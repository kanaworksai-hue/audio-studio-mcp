"""Download Stable Audio 3 weights from Hugging Face (used by install.sh / install.ps1).

Usage: <venv python> scripts/download_weights.py [medium|sm-music|sm-sfx|all ...]

Apple Silicon (sa3-mlx): the MLX weights from stabilityai/stable-audio-3-optimized,
symlinked into vendor/stable-audio-3/optimized/mlx/models/mlx by the upstream helper.

NVIDIA / PyTorch (sa3-torch): the official checkpoints from
stabilityai/stable-audio-3-{medium,small-music,small-sfx}. These repos are gated:
accept the license on each model page, then log in once with `hf auth login`.

Files go to the Hugging Face cache (HF_HOME / HF_HUB_CACHE). The logic lives in
the package (audio_studio_mcp.weights), shared with `audio-studio-mcp setup`.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "src"))
from audio_studio_mcp import weights  # noqa: E402
from audio_studio_mcp.backends import resolve_backend_name  # noqa: E402
from audio_studio_mcp.runtime import MLX_MARKER, upstream_dir  # noqa: E402


def main(argv: list[str]) -> int:
    # Windows consoles and pipes may use a legacy code page; never crash on ✓ / →.
    sys.stdout.reconfigure(errors="replace")
    try:
        names = weights.expand(argv)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    backend = resolve_backend_name()
    upstream = upstream_dir()
    if backend == "sa3-mlx" and not (upstream / MLX_MARKER).is_file():
        print(f"error: upstream runtime not found at {upstream}; run ./install.sh first", file=sys.stderr)
        return 1
    return weights.download(backend, names, upstream)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
