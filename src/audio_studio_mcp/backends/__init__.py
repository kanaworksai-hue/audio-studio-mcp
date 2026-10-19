"""Backend selection.

AUDIO_STUDIO_BACKEND = auto (default) | sa3-mlx | sa3-torch
  auto picks sa3-mlx on Apple Silicon macOS and sa3-torch (PyTorch, NVIDIA
  CUDA) everywhere else.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys

from .base import Backend, GenerationRequest, GenerationResult

__all__ = ["Backend", "GenerationRequest", "GenerationResult", "create_backend", "resolve_backend_name"]

BACKENDS = ("sa3-mlx", "sa3-torch")


def _running_under_rosetta() -> bool:
    """True when this (x86_64) process is translated by Rosetta 2 on an Apple Silicon Mac."""
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "1"


def resolve_backend_name(name: str | None = None) -> str:
    name = (name or os.environ.get("AUDIO_STUDIO_BACKEND") or "auto").strip().lower()
    if name == "auto":
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            return "sa3-mlx"
        if platform.system() == "Darwin" and _running_under_rosetta():
            raise RuntimeError(
                f"this Python ({sys.executable}) is an x86_64 build running under Rosetta on an "
                "Apple Silicon Mac, so MLX is not available. Recreate the environment with an arm64 "
                "Python: re-run ./install.sh (it picks one; unset UV_PYTHON if you set it to an x86_64 "
                "Python), or run: uv sync --extra mlx --python cpython-3.11-macos-aarch64")
        if platform.system() == "Darwin":
            raise RuntimeError("Intel Macs are not supported; Audio Studio MCP needs Apple Silicon "
                               "(sa3-mlx) or an NVIDIA GPU (sa3-torch).")
        return "sa3-torch"
    if name not in BACKENDS:
        raise RuntimeError(f"unknown AUDIO_STUDIO_BACKEND {name!r}; choose auto or one of {', '.join(BACKENDS)}")
    return name


def create_backend(name: str | None = None) -> Backend:
    name = resolve_backend_name(name)
    if name == "sa3-mlx":
        from .sa3_mlx import SA3MLXBackend
        return SA3MLXBackend()
    if name == "sa3-torch":
        from .sa3_torch import SA3TorchBackend
        return SA3TorchBackend()
    raise AssertionError(name)
