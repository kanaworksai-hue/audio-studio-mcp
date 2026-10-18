"""Backend selection.

AUDIO_STUDIO_BACKEND = auto (default) | sa3-mlx
  auto picks sa3-mlx on Apple Silicon macOS. Other platforms (Windows/CUDA)
  are not supported yet.
"""
from __future__ import annotations

import os
import platform

from .base import Backend, GenerationRequest, GenerationResult

__all__ = ["Backend", "GenerationRequest", "GenerationResult", "create_backend", "resolve_backend_name"]

BACKENDS = ("sa3-mlx",)


def resolve_backend_name(name: str | None = None) -> str:
    name = (name or os.environ.get("AUDIO_STUDIO_BACKEND") or "auto").strip().lower()
    if name == "auto":
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            return "sa3-mlx"
        raise RuntimeError(
            f"No backend for {platform.system()}/{platform.machine()} yet. "
            "Currently supported: Apple Silicon Mac (sa3-mlx).")
    if name not in BACKENDS:
        raise RuntimeError(f"unknown AUDIO_STUDIO_BACKEND {name!r}; choose auto or one of {', '.join(BACKENDS)}")
    return name


def create_backend(name: str | None = None) -> Backend:
    name = resolve_backend_name(name)
    if name == "sa3-mlx":
        from .sa3_mlx import SA3MLXBackend
        return SA3MLXBackend()
    raise AssertionError(name)
