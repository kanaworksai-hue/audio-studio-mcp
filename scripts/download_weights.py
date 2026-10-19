"""Download Stable Audio 3 weights from Hugging Face.

Usage: <venv python> scripts/download_weights.py [medium|sm-music|sm-sfx|all ...]

Apple Silicon (sa3-mlx): the MLX weights from stabilityai/stable-audio-3-optimized,
symlinked into vendor/stable-audio-3/optimized/mlx/models/mlx by the upstream helper.

NVIDIA / PyTorch (sa3-torch): the official checkpoints from
stabilityai/stable-audio-3-{medium,small-music,small-sfx}. These repos are gated:
accept the license on each model page, then log in once with `hf auth login`.

Files go to the Hugging Face cache (HF_HOME / HF_HUB_CACHE).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
UPSTREAM_DIR = Path(os.environ.get("AUDIO_STUDIO_SA3_DIR") or REPO_DIR / "vendor" / "stable-audio-3"
                    ).expanduser().resolve()
MLX_DIR = UPSTREAM_DIR / "optimized" / "mlx"

sys.path.insert(0, str(REPO_DIR / "src"))
from audio_studio_mcp.backends import resolve_backend_name  # noqa: E402

MODELS = ("medium", "sm-music", "sm-sfx")
# sa3-torch: name → (Hugging Face repo, approximate download size)
TORCH_REPOS = {
    "medium": ("stabilityai/stable-audio-3-medium", "~10.4 GB"),
    "sm-music": ("stabilityai/stable-audio-3-small-music", "~3.5 GB"),
    "sm-sfx": ("stabilityai/stable-audio-3-small-sfx", "~3.5 GB"),
}
TORCH_FILES = ["model_config.json", "model.safetensors", "t5gemma-b-b-ul2/*"]


def download_mlx(names: list[str]) -> int:
    if not (MLX_DIR / "scripts" / "weights.py").is_file():
        print(f"error: upstream runtime not found at {MLX_DIR}; run ./install.sh first", file=sys.stderr)
        return 1
    sys.path.insert(0, str(MLX_DIR / "scripts"))
    from weights import BUNDLE_SIZES, DIT_BUNDLES, SHARED, ensure_local

    seen: set[str] = set()
    for name in names:
        print(f"[{name}] {BUNDLE_SIZES[name]}")
        for rel, _ in DIT_BUNDLES[name] + SHARED:
            if rel not in seen:
                seen.add(rel)
                path = ensure_local(rel)
                print(f"  ✓ {Path(rel).name}" + ("" if path.exists() else "  (missing!)"))
    return 0


def download_torch(names: list[str]) -> int:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import GatedRepoError

    for name in names:
        repo, size = TORCH_REPOS[name]
        print(f"[{name}] {repo} ({size})")
        try:
            path = snapshot_download(repo, allow_patterns=TORCH_FILES)
        except GatedRepoError:
            print(f"\nerror: {repo} is gated. Open this page, log in and accept the\n"
                  f"Stability AI Community License:\n  https://huggingface.co/{repo}\n"
                  "then log in here once (hf auth login, with a read token) and re-run.",
                  file=sys.stderr)
            return 2
        print(f"  ✓ {path}")
    return 0


def main(argv: list[str]) -> int:
    # Windows consoles and pipes may use a legacy code page; never crash on ✓ / →.
    sys.stdout.reconfigure(errors="replace")
    names = argv or ["medium"]
    if "all" in names:
        names = list(MODELS)
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        print(f"error: unknown model(s) {', '.join(unknown)}; choose from {', '.join(MODELS)} or all",
              file=sys.stderr)
        return 1
    if resolve_backend_name() == "sa3-mlx":
        return download_mlx(names)
    return download_torch(names)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
