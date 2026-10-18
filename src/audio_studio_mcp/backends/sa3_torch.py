"""Stable Audio 3 on Windows / Linux with an NVIDIA GPU via the official
PyTorch runtime (Stability-AI/stable-audio-3, cloned into vendor/).

This is the Windows counterpart of sa3_mlx.py. It wraps the upstream
``StableAudioModel`` (the same class the official ``stable-audio`` CLI uses),
so results are reproducible per seed and match upstream for identical inputs.

Weights come from a local mirror directory first
(``<repo>/models/stable-audio-3-<model>/``) because the Hugging Face repos are
gated; only if that is absent does it fall back to ``hf_hub_download``.

Model ids differ from the MLX backend: upstream names them small-music /
small-sfx / medium, while the studio UI calls the two small ones sm-music and
sm-sfx. MODELS below is the only place that mapping lives.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .base import Backend, GenerationRequest, GenerationResult

# studio id -> (upstream model id, max seconds)
MODELS = {
    "medium": ("medium", 380.0),
    "sm-music": ("small-music", 120.0),
    "sm-sfx": ("small-sfx", 120.0),
}


def default_upstream_dir() -> Path:
    # <repo>/src/audio_studio_mcp/backends/sa3_torch.py -> <repo>/vendor/stable-audio-3
    return Path(__file__).resolve().parents[3] / "vendor" / "stable-audio-3"


def default_weights_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "models"


def _load_audio(path: str):
    """Read any format soundfile supports and return (sample_rate, tensor[ch, n])."""
    import soundfile as sf
    import torch

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return int(sr), torch.from_numpy(np.ascontiguousarray(data.T))


class SA3TorchBackend(Backend):
    name = "sa3-torch"

    def __init__(self, upstream_dir: Path | None = None, model: str | None = None,
                 device: str | None = None, weights_dir: Path | None = None) -> None:
        super().__init__()
        self.upstream_dir = Path(
            upstream_dir or os.environ.get("AUDIO_STUDIO_SA3_DIR") or default_upstream_dir()
        ).expanduser().resolve()

        if not (self.upstream_dir / "stable_audio_3" / "model.py").is_file():
            raise RuntimeError(
                f"Stable Audio 3 PyTorch runtime not found at {self.upstream_dir}. "
                "Set AUDIO_STUDIO_SA3_DIR to a stable-audio-3 checkout."
            )

        self.weights_root = Path(
            weights_dir or os.environ.get("AUDIO_STUDIO_WEIGHTS_DIR") or default_weights_dir()
        ).expanduser().resolve()

        self.dit = model or os.environ.get("AUDIO_STUDIO_SA3_MODEL", "sm-sfx")
        if self.dit not in MODELS:
            raise RuntimeError(
                f"unknown AUDIO_STUDIO_SA3_MODEL {self.dit!r}; "
                f"choose one of {', '.join(MODELS)}"
            )
        self.upstream_model, self.max_seconds = MODELS[self.dit]
        self.model_label = f"{self.dit} (torch)"

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sa3-torch")
        self._model = None
        self._torch = None

        # Upstream prints progress to stdout; under stdio MCP that would corrupt
        # the JSON-RPC stream. Keep everything off stdout even for direct CLI use.
        if str(self.upstream_dir) not in sys.path:
            sys.path.insert(0, str(self.upstream_dir))

        with contextlib.redirect_stdout(sys.stderr):
            import torch

        self._torch = torch
        if device is None:
            device = os.environ.get("AUDIO_STUDIO_DEVICE") or (
                "cuda" if torch.cuda.is_available() else "cpu")
        self.device = device
        self.model_half = torch.cuda.is_available() and device.startswith("cuda")

    # ── backend contract ─────────────────────────────────────────────────────
    @property
    def executor(self) -> Executor:
        return self._executor

    def loaded(self) -> bool:
        return self._model is not None

    def _local_dir(self) -> Path:
        return self.weights_root / f"stable-audio-3-{self.upstream_model}"

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        from stable_audio_3 import StableAudioModel

        local = self._local_dir()
        cfg_path, ckpt_path = local / "model_config.json", local / "model.safetensors"

        t0 = time.time()
        with contextlib.redirect_stdout(sys.stderr):
            if cfg_path.is_file() and ckpt_path.is_file():
                self._model = self._load_local(StableAudioModel, cfg_path, ckpt_path, local)
                src = f"local mirror {local}"
            else:
                self._model = StableAudioModel.from_pretrained(
                    self.upstream_model, device=self.device, model_half=self.model_half)
                src = f"huggingface {self.upstream_model}"
        print(f"[audio-studio] loaded {src} on {self.device} in {time.time() - t0:.1f}s",
              file=sys.stderr)
        return self._model

    def _load_local(self, cls, cfg_path: Path, ckpt_path: Path, local: Path):
        """Build the model from a mirrored checkpoint, bypassing huggingface_hub.

        The T5Gemma text encoder resolves its weights from the conditioning
        config, so point it at the mirror root (the ``subfolder`` stays intact).
        """
        from stable_audio_3.loading_utils import load_diffusion_cond

        with open(cfg_path, encoding="utf-8") as f:
            model_config = json.load(f)

        conds = model_config.get("model", {}).get("conditioning", {}).get("configs", [])
        for c in conds:
            if c.get("type") == "t5gemma":
                c.setdefault("config", {})["model_path"] = str(local)

        model = load_diffusion_cond(model_config, str(ckpt_path),
                                    device=self.device, model_half=self.model_half)
        model.use_lora = False
        model.lora_names = []
        return cls(model, model_config, self.device, self.model_half)

    def _preload(self) -> None:
        self._ensure_model()

    def _unload(self) -> bool:
        was = self._model is not None
        self._model = None
        if self._torch is not None:
            import gc

            gc.collect()
            if self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        return was

    def _generate(self, seeds: list[int], req: GenerationRequest) -> GenerationResult:
        model = self._ensure_model()
        res = GenerationResult()

        kw: dict = dict(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt or None,
            duration=float(req.seconds),
            steps=int(req.steps),
            cfg_scale=float(req.cfg),
            apg_scale=1.0,
        )
        if req.init_audio:
            kw.update(init_audio=_load_audio(req.init_audio),
                      init_noise_level=float(req.init_strength))
        if req.inpaint_audio:
            sr, wav = _load_audio(req.inpaint_audio)
            start, end = req.inpaint_range or (0.0, float(req.seconds))
            kw.update(inpaint_audio=(sr, wav),
                      inpaint_mask_start_seconds=float(start),
                      inpaint_mask_end_seconds=float(end))

        with contextlib.redirect_stdout(sys.stderr):
            t0 = time.time()
            for seed in seeds:
                audio = model.generate(seed=int(seed), **kw)
                # upstream returns [batch, channels, samples] float32 in [-1, 1]
                arr = audio[0].detach().to("cpu").float().numpy()
                res.audios.append(np.ascontiguousarray(arr, dtype=np.float32))
            res.wall_seconds = time.time() - t0
        return res
