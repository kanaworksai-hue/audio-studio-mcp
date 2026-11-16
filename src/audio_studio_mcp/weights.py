"""Stable Audio 3 model weights: what is downloaded, and downloading it.

Apple Silicon (sa3-mlx): the MLX weights from stabilityai/stable-audio-3-optimized
(not gated), symlinked into <runtime>/optimized/mlx/models/mlx by upstream's helper.

NVIDIA / PyTorch (sa3-torch): the official checkpoints from
stabilityai/stable-audio-3-{medium,small-music,small-sfx}. These repos are gated:
accept the license on each model page, then log in once with `hf auth login`.

Files go to the Hugging Face cache (HF_HOME / HF_HUB_CACHE).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

MODELS = ("medium", "sm-music", "sm-sfx")
# Download sizes shown before downloading (MLX: upstream's numbers incl. the
# shared T5Gemma text encoder).
MLX_SIZES = {"medium": "~6.9 GB", "sm-music": "~1.9 GB", "sm-sfx": "~1.9 GB"}
# sa3-torch: name → (Hugging Face repo, approximate download size)
TORCH_REPOS = {
    "medium": ("stabilityai/stable-audio-3-medium", "~10.4 GB"),
    "sm-music": ("stabilityai/stable-audio-3-small-music", "~3.5 GB"),
    "sm-sfx": ("stabilityai/stable-audio-3-small-sfx", "~3.5 GB"),
}
TORCH_FILES = ["model_config.json", "model.safetensors", "t5gemma-b-b-ul2/*"]
LICENSE_NOTE = "The weights are licensed under the Stability AI Community License."


def _mlx_manifest(upstream: Path):
    """Upstream's weights module (optimized/mlx/scripts/weights.py)."""
    scripts = str(upstream / "optimized" / "mlx" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("weights")


def missing(backend: str, model: str, upstream: Path) -> list[str]:
    """Weight files (MLX) or repos (PyTorch) of `model` that are not downloaded yet."""
    if backend == "sa3-mlx":
        w = _mlx_manifest(upstream)
        mlx_dir = upstream / "optimized" / "mlx"
        rels = dict.fromkeys(rel for rel, _ in w.DIT_BUNDLES[model] + w.SHARED)
        # exists() follows the symlink into the HF cache, so a cleared cache counts as missing
        return [rel for rel in rels if not (mlx_dir / rel).exists()]
    from huggingface_hub import try_to_load_from_cache
    repo = TORCH_REPOS[model][0]
    for name in ("model_config.json", "model.safetensors"):
        if not isinstance(try_to_load_from_cache(repo, name), str):
            return [repo]
    return []


def in_hf_cache(model: str, upstream: Path) -> bool:
    """MLX: every file of `model` is already in the Hugging Face cache (so setup
    only has to link them, e.g. after AUDIO_STUDIO_HOME moved)."""
    from huggingface_hub import try_to_load_from_cache
    w = _mlx_manifest(upstream)
    return all(isinstance(try_to_load_from_cache(w.REPO_ID, hf), str)
               for _, hf in w.DIT_BUNDLES[model] + w.SHARED)


def file_count(model: str, upstream: Path) -> int:
    """Number of MLX weight files `model` needs."""
    w = _mlx_manifest(upstream)
    return len(dict.fromkeys(rel for rel, _ in w.DIT_BUNDLES[model] + w.SHARED))


def download_size(backend: str, names: list[str]) -> str:
    if backend == "sa3-mlx":
        return " + ".join(MLX_SIZES[n] for n in names)
    return " + ".join(TORCH_REPOS[n][1] for n in names)


def download_mlx(names: list[str], upstream: Path) -> int:
    w = _mlx_manifest(upstream)
    seen: set[str] = set()
    for name in names:
        print(f"[{name}] {w.BUNDLE_SIZES[name]}")
        for rel, _ in w.DIT_BUNDLES[name] + w.SHARED:
            if rel not in seen:
                seen.add(rel)
                path = w.ensure_local(rel)
                print(f"  ✓ {Path(rel).name}" + ("" if path.exists() else "  (missing!)"))
    return 0


def hf_account() -> str | None:
    """The Hugging Face user this machine is logged in as (like `hf auth whoami`)."""
    from huggingface_hub import whoami
    try:
        return whoami()["name"]
    except Exception:
        return None


def hf_login_command() -> str:
    """The `hf auth login` line for this install: the venv's hf for a checkout,
    a one-off uvx run for package installs (the token is saved per user)."""
    from .runtime import source_checkout
    if source_checkout() is not None:
        hf = Path(sys.executable).with_name("hf.exe" if os.name == "nt" else "hf")
        return f'"{hf}" auth login'
    return "uvx --from huggingface_hub hf auth login"


def gated_help(repos: list[str], account: str | None, retry: str = "Run the installer again") -> str:
    """Why a gated download failed and what the user has to do, worded so an AI
    agent running the installer can pass it on as is."""
    login = hf_login_command()
    pages = "".join(f"\n       https://huggingface.co/{r}" for r in repos)
    if account:
        head = (f"error: the Hugging Face account '{account}' (hf auth whoami) has no access to\n"
                "these gated model repos yet. Either the license was not accepted on them, or it\n"
                "was accepted with a different account.\n\n"
                f"  1. Log in on huggingface.co as '{account}', open each page and accept the\n"
                f"     Stability AI Community License (fill in the short form and submit):{pages}\n"
                "     To use another account instead, log in with it in your own terminal:\n"
                f"       {login}\n")
    else:
        head = ("error: not logged in to Hugging Face, so these gated model repos cannot be\n"
                "downloaded.\n\n"
                "  1. Create a free account (https://huggingface.co/join), log in on huggingface.co,\n"
                "     open each page and accept the Stability AI Community License:"
                f"{pages}\n"
                "     Then create a Read token (https://huggingface.co/settings/tokens) and, in your\n"
                "     own terminal (not in a chat), run this and paste the token:\n"
                f"       {login}\n")
    return (head +
            f"  2. {retry}; finished downloads are kept.\n\n"
            "AI agents: pass these steps on to the user and wait until they are done. Do not ask\n"
            "for the token in the chat, and do not skip the weights to get past this.")


def gated_repos(names: list[str]) -> list[str]:
    """The PyTorch repos of `names` this machine cannot download (license / login)."""
    from huggingface_hub import auth_check
    from huggingface_hub.errors import GatedRepoError

    gated = []
    for name in names:
        repo = TORCH_REPOS[name][0]
        try:
            auth_check(repo)
        except GatedRepoError:
            gated.append(repo)
        except Exception:
            pass  # offline etc.: let snapshot_download decide
    return gated


def download_torch(names: list[str], retry: str = "Run the installer again") -> int:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import GatedRepoError

    # Check every repo first, so the user can accept all the licenses in one go.
    gated = gated_repos(names)
    if gated:
        print(gated_help(gated, hf_account(), retry), file=sys.stderr)
        return 2

    for name in names:
        repo, size = TORCH_REPOS[name]
        print(f"[{name}] {repo} ({size})")
        try:
            path = snapshot_download(repo, allow_patterns=TORCH_FILES)
        except GatedRepoError:
            print(gated_help([repo], hf_account(), retry), file=sys.stderr)
            return 2
        print(f"  ✓ {path}")
    return 0


def expand(names: list[str]) -> list[str]:
    names = names or ["medium"]
    if "all" in names:
        return list(MODELS)
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        raise ValueError(f"unknown model(s) {', '.join(unknown)}; choose from {', '.join(MODELS)} or all")
    return list(dict.fromkeys(names))


def download(backend: str, names: list[str], upstream: Path, retry: str = "Run the installer again") -> int:
    if backend == "sa3-mlx":
        return download_mlx(names, upstream)
    return download_torch(names, retry)
