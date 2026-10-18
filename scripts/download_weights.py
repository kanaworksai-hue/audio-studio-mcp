"""Download Stable Audio 3 MLX weights from Hugging Face.

Usage: .venv/bin/python scripts/download_weights.py [medium|sm-music|sm-sfx|all ...]

Files go to the Hugging Face cache (HF_HOME / HF_HUB_CACHE) and are symlinked
into vendor/stable-audio-3/optimized/mlx/models/mlx by the upstream helper.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
MLX_DIR = Path(os.environ.get("AUDIO_STUDIO_SA3_DIR") or REPO_DIR / "vendor" / "stable-audio-3"
               ).expanduser().resolve() / "optimized" / "mlx"


def main(argv: list[str]) -> int:
    if not (MLX_DIR / "scripts" / "weights.py").is_file():
        print(f"error: upstream runtime not found at {MLX_DIR}; run ./install.sh first", file=sys.stderr)
        return 1
    sys.path.insert(0, str(MLX_DIR / "scripts"))
    from weights import BUNDLE_SIZES, DIT_BUNDLES, SHARED, ensure_local

    names = argv or ["medium"]
    if "all" in names:
        names = list(DIT_BUNDLES)
    unknown = [n for n in names if n not in DIT_BUNDLES]
    if unknown:
        print(f"error: unknown model(s) {', '.join(unknown)}; choose from {', '.join(DIT_BUNDLES)} or all",
              file=sys.stderr)
        return 1

    seen: set[str] = set()
    for name in names:
        print(f"[{name}] {BUNDLE_SIZES[name]}")
        for rel, _ in DIT_BUNDLES[name] + SHARED:
            if rel not in seen:
                seen.add(rel)
                path = ensure_local(rel)
                print(f"  ✓ {Path(rel).name}" + ("" if path.exists() else "  (missing!)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
