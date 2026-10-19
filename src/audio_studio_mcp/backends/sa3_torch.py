"""Stable Audio 3 on NVIDIA GPUs (Windows / Linux) via the official PyTorch API.

Wraps upstream stable_audio_3 (Stability-AI/stable-audio-3, cloned by
install.ps1 / install.sh into vendor/): StableAudioModel.from_pretrained and
StableAudioModel.generate, the documented Python API. Weights come from the
Hugging Face repos stabilityai/stable-audio-3-{medium,small-music,small-sfx},
which are gated: accept the license on the model page and log in once
(`hf auth login`).

Flash Attention 2 is optional. Without it upstream falls back to SDPA; we also
turn off its flex_attention path when Triton is missing (Windows), because
torch.compile cannot build it there and the eager fallback is ~15x slower.
"""
from __future__ import annotations

import contextlib
import gc
import importlib.util
import os
import sys
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

from .base import Backend, GenerationRequest, GenerationResult

MODELS = {  # AUDIO_STUDIO_SA3_MODEL → (upstream model name, autoencoder, max seconds)
    "medium": ("medium", "same-l", 380.0),
    "sm-music": ("small-music", "same-s", 120.0),
    "sm-sfx": ("small-sfx", "same-s", 120.0),
}


def default_upstream_dir() -> Path:
    # <repo>/src/audio_studio_mcp/backends/sa3_torch.py → <repo>/vendor/stable-audio-3
    return Path(__file__).resolve().parents[3] / "vendor" / "stable-audio-3"


class SA3TorchBackend(Backend):
    name = "sa3-torch"

    def __init__(self, upstream_dir: Path | None = None, model: str | None = None,
                 device: str | None = None) -> None:
        super().__init__()
        upstream_dir = Path(upstream_dir or os.environ.get("AUDIO_STUDIO_SA3_DIR")
                            or default_upstream_dir()).expanduser().resolve()
        if not (upstream_dir / "stable_audio_3" / "model.py").is_file():
            raise RuntimeError(
                f"Stable Audio 3 runtime not found at {upstream_dir}. Run the installer "
                "(install.ps1 on Windows, install.sh on Linux), or set AUDIO_STUDIO_SA3_DIR "
                "to a stable-audio-3 checkout.")

        self.dit = model or os.environ.get("AUDIO_STUDIO_SA3_MODEL", "medium")
        if self.dit not in MODELS:
            raise RuntimeError(f"unknown AUDIO_STUDIO_SA3_MODEL {self.dit!r}; "
                               f"choose one of {', '.join(MODELS)}")
        self.upstream_model, self.decoder, self.max_seconds = MODELS[self.dit]
        self.model_label = f"{self.dit} + {self.decoder}"

        # Upstream prints to stdout (e.g. "flash_attn not installed"), which
        # would corrupt the stdio JSON-RPC stream: import and call it with
        # stdout pointed at stderr.
        if str(upstream_dir) not in sys.path:
            sys.path.insert(0, str(upstream_dir))
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            import stable_audio_3
            from stable_audio_3.models import transformer
        if transformer.flex_attention_compiled is not None and importlib.util.find_spec("triton") is None:
            transformer.flex_attention_compiled = None
        self._torch, self._sa3 = torch, stable_audio_3
        self.device = device or os.environ.get("AUDIO_STUDIO_DEVICE") or (
            "cuda" if torch.cuda.is_available() else "cpu")

        self._model = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sa3-torch")

    @property
    def executor(self) -> Executor:
        return self._pool

    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> bool:
        """Load the model if needed; returns whether this call loaded it."""
        if self._model is not None:
            return False
        if self.device == "cpu" and self.dit == "medium":
            print("[audio-studio] warning: no CUDA GPU found; medium on CPU is very slow "
                  "(AUDIO_STUDIO_SA3_MODEL=sm-music / sm-sfx run on CPU)", file=sys.stderr)
        with contextlib.redirect_stdout(sys.stderr):
            self._model = self._sa3.StableAudioModel.from_pretrained(
                self.upstream_model, device=self.device)
        self.sample_rate = int(self._model.model.sample_rate)
        return True

    def _unload(self) -> bool:
        if self._model is None:
            return False
        self._model = None
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
        return True

    def _preload(self) -> None:
        self._load()

    def _read(self, path: str):
        from .. import post
        audio, sr = post.read(path)
        return sr, self._torch.from_numpy(audio)

    def _generate(self, seeds: list[int], req: GenerationRequest) -> GenerationResult:
        res = GenerationResult()
        with contextlib.redirect_stdout(sys.stderr):
            t0 = time.time()
            res.included_model_load = self._load()
            # generate() defaults sample_size to 120 s of audio and clamps to it;
            # use the model's own maximum (380 s for medium).
            kw = dict(prompt=req.prompt, duration=req.seconds, steps=req.steps,
                      cfg_scale=req.cfg, apg_scale=1.0,
                      sample_size=int(self._model.model_config["sample_size"]))
            # Like the MLX backend: the negative prompt only matters with CFG.
            if req.cfg != 1.0 and req.negative_prompt.strip():
                kw["negative_prompt"] = req.negative_prompt.strip()
            if req.init_audio:
                kw.update(init_audio=self._read(req.init_audio), init_noise_level=req.init_strength)
            if req.inpaint_audio:
                start, end = req.inpaint_range
                kw.update(inpaint_audio=self._read(req.inpaint_audio),
                          inpaint_mask_start_seconds=float(start), inpaint_mask_end_seconds=float(end))
            for seed in seeds:
                audio = self._model.generate(seed=seed, **kw)
                res.audios.append(audio[0].float().cpu().numpy())
            res.wall_seconds = time.time() - t0
        return res
