"""Stable Audio 3 on Apple Silicon via the official MLX runtime.

Wraps upstream optimized/mlx (Stability-AI/stable-audio-3, cloned by
install.sh into vendor/). Generation goes through sa3_gradio.run_generation,
the official cached-model path, so results match upstream ./sa3 and
./sa3-gradio bit-for-bit for the same seed.

Upstream internals used here (pinned commit, see install.sh): run_generation,
load_dit, get_t5/get_conditioner/get_decoder, _reconcile_lora, the module
caches and _MLX_EXECUTOR.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from concurrent.futures import Executor
from pathlib import Path

from .base import Backend, GenerationRequest, GenerationResult

MODELS = {  # DiT → (decoder, max seconds)
    "medium": ("same-l", 380.0),
    "sm-music": ("same-s", 120.0),
    "sm-sfx": ("same-s", 120.0),
}


def default_upstream_dir() -> Path:
    # <repo>/src/audio_studio_mcp/backends/sa3_mlx.py → <repo>/vendor/stable-audio-3
    return Path(__file__).resolve().parents[3] / "vendor" / "stable-audio-3"


class SA3MLXBackend(Backend):
    name = "sa3-mlx"

    def __init__(self, upstream_dir: Path | None = None, model: str | None = None,
                 cache_limit_gb: float | None = None) -> None:
        super().__init__()
        upstream_dir = Path(upstream_dir or os.environ.get("AUDIO_STUDIO_SA3_DIR")
                            or default_upstream_dir()).expanduser().resolve()
        self.mlx_dir = upstream_dir / "optimized" / "mlx"
        if not (self.mlx_dir / "scripts" / "sa3_gradio.py").is_file():
            raise RuntimeError(
                f"Stable Audio 3 MLX runtime not found at {self.mlx_dir}. "
                "Run ./install.sh, or set AUDIO_STUDIO_SA3_DIR to a stable-audio-3 checkout.")

        self.dit = model or os.environ.get("AUDIO_STUDIO_SA3_MODEL", "medium")
        if self.dit not in MODELS:
            raise RuntimeError(f"unknown AUDIO_STUDIO_SA3_MODEL {self.dit!r}; "
                               f"choose one of {', '.join(MODELS)}")
        self.decoder, self.max_seconds = MODELS[self.dit]
        self.model_label = f"{self.dit} + {self.decoder}"

        # Upstream resolves weights relative to the MLX dir and prints progress
        # to stdout, which would corrupt the stdio JSON-RPC stream. Import with
        # stdout pointed at stderr; every model call is wrapped the same way.
        os.chdir(self.mlx_dir)
        sys.path.insert(0, str(self.mlx_dir / "scripts"))
        with contextlib.redirect_stdout(sys.stderr):
            import mlx.core as mx
            import sa3_gradio as g
        self._mx, self._g = mx, g
        self.sample_rate = int(g.SAMPLE_RATE)

        # Upstream never trims MLX's buffer cache between generations: after a
        # 120 s track the process keeps ~6 GB of cache on top of ~4.8 GB of
        # weights. A 1 GB cap costs ~4% speed.
        if cache_limit_gb is None:
            cache_limit_gb = float(os.environ.get("AUDIO_STUDIO_MLX_CACHE_GB", "1"))
        if cache_limit_gb > 0:
            mx.set_cache_limit(int(cache_limit_gb * 2**30))

        # The medium/small DiTs zero-pad local conditioning at the *input*
        # length, so one loaded instance serves every duration. Upstream keys
        # its cache on the latent length (and reloads 2.7 GB for each new
        # length); key on the model name only instead.
        self._dit_single: dict[str, object] = {}
        g.get_dit = self._get_dit_any_length

    @property
    def executor(self) -> Executor:
        return self._g._MLX_EXECUTOR

    def _get_dit_any_length(self, dit_name, T_lat, dtype, lora_specs=None, num_steps=8):
        g = self._g
        load_ms = 0.0
        if dit_name not in self._dit_single:
            t0 = time.time()
            self._dit_single[dit_name], _ = g.load_dit(dit_name, T_lat=T_lat, dtype=dtype)
            load_ms = (time.time() - t0) * 1000
        model = self._dit_single[dit_name]
        g._reconcile_lora(model, dit_name, lora_specs, num_steps)
        return model, load_ms

    def loaded(self) -> bool:
        g = self._g
        return bool(self._dit_single or g._dec_cache or g._enc_cache or g._t5 is not None)

    def _unload(self) -> bool:
        g = self._g
        was = self.loaded()
        self._dit_single.clear()
        g._dit_cache.clear()
        g._dit_lru.clear()
        g._dec_cache.clear()
        g._enc_cache.clear()
        g._cond_cache.clear()
        g._latent_cache.clear()
        g._t5 = None
        g._free_to_pool()
        return was

    def _preload(self) -> None:
        g = self._g
        with contextlib.redirect_stdout(sys.stderr):
            g.get_t5()
            g.get_conditioner(self.dit)
            self._get_dit_any_length(self.dit, 64, self._mx.float16)
            g.get_decoder(self.decoder)

    def _generate(self, seeds: list[int], req: GenerationRequest) -> GenerationResult:
        kw = dict(prompt=req.prompt, negative_prompt=req.negative_prompt,
                  seconds=req.seconds, steps=req.steps, cfg=req.cfg, apg=1.0,
                  sigma_max=1.0)
        if req.init_audio:
            kw.update(a2a_audio_path=req.init_audio, sigma_max=req.init_strength)
        if req.inpaint_audio:
            kw.update(inpaint_audio_path=req.inpaint_audio,
                      inpaint_range_sec=req.inpaint_range)
        res = GenerationResult()
        with contextlib.redirect_stdout(sys.stderr):
            t0 = time.time()
            for i, seed in enumerate(seeds):
                audio, timings = self._g.run_generation(
                    dit_name=self.dit, decoder_name=self.decoder, seed=seed, **kw)
                res.audios.append(audio)
                if i == 0:
                    res.included_model_load = timings.get("dit_load_ms", 0) > 0
            res.wall_seconds = time.time() - t0
        return res
