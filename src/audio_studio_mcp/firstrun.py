"""`audio-studio-mcp setup`: the first-run check, run once in a terminal.

Checks the platform (arm64 Python + MLX on a Mac, a CUDA build of PyTorch on
NVIDIA machines), downloads the upstream Stable Audio 3 runtime (package
installs) and the model weights after saying how big they are, and prints how
to register the server with an MCP client. Safe to re-run: finished downloads
are kept.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys

from . import runtime, weights
from .backends import resolve_backend_name

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
B, C, R, Y, G, N = (("\033[1m", "\033[1;36m", "\033[1;31m", "\033[1;33m", "\033[1;32m", "\033[0m")
                    if _TTY else ("",) * 6)


def step(msg: str) -> None:
    print(f"\n{C}→ {msg}{N}", flush=True)


def ok(msg: str) -> None:
    print(f"  {G}✓{N} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  {Y}warning{N}: {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"\n{R}error{N}: {msg}", file=sys.stderr, flush=True)


def check_mlx() -> bool:
    try:
        import mlx.core as mx
    except ImportError:
        fail("MLX is not installed in this Python environment. Start Audio Studio with a native "
             f"arm64 Python, e.g.\n  {runtime.UVX_MAC_ARM64} setup")
        return False
    ok(f"Apple Silicon, MLX {getattr(mx, '__version__', '')} ({sys.executable})")
    return True


def check_torch() -> bool:
    try:
        import torch
    except ImportError:
        fail("PyTorch is not installed in this Python environment. On Linux use the cuda extra:\n"
             f"  {runtime.UVX_LINUX} setup")
        return False
    if torch.cuda.is_available():
        ok(f"NVIDIA GPU: {torch.cuda.get_device_name(0)} (PyTorch {torch.__version__})")
        return True
    if torch.version.cuda is None and runtime.nvidia_gpu_present():
        fail(runtime.cpu_torch_message(torch.__version__))
        return False
    warn("no NVIDIA GPU found, so generation runs on the CPU and is very slow (minutes per clip "
         "with medium). The small models (--model sm-sfx / sm-music) are lighter.")
    return True


def confirm(question: str) -> bool:
    try:
        reply = input(f"  {question} [Y/n] ")
    except EOFError:
        return False
    return reply.strip().lower() in ("", "y", "yes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="audio-studio-mcp setup",
        description="First-run setup: check the platform, download the Stable Audio 3 runtime "
                    "and model weights, and show how to register the MCP server.")
    ap.add_argument("--model", default=os.environ.get("AUDIO_STUDIO_SA3_MODEL", "medium"),
                    help="weights to download: medium (default), sm-music, sm-sfx or all "
                         "(comma-separated for several)")
    ap.add_argument("-y", "--yes", action="store_true", help="download without asking")
    ap.add_argument("--skip-weights", action="store_true",
                    help="only check the platform and fetch the runtime")
    args = ap.parse_args(argv)
    for name in ("httpx", "httpx2", "huggingface_hub"):  # no per-request log lines
        logging.getLogger(name).setLevel(logging.WARNING)
    # Windows consoles and pipes may use a legacy code page; never crash on ✓ / →.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    try:
        names = weights.expand([n.strip() for n in args.model.split(",") if n.strip()])
    except ValueError as e:
        fail(str(e))
        return 1

    print(f"{B}Audio Studio MCP setup{N}")
    step("Checking the platform")
    try:
        backend = resolve_backend_name()
    except RuntimeError as e:
        fail(str(e))
        return 1
    if not (check_mlx() if backend == "sa3-mlx" else check_torch()):
        return 1
    if shutil.which("ffmpeg"):
        ok("ffmpeg found")
    else:
        hint = "brew install ffmpeg" if sys.platform == "darwin" else (
            "winget install Gyan.FFmpeg" if os.name == "nt" else "your package manager")
        warn(f"ffmpeg not found. Generation works, but process_audio needs it for resampling and "
             f"loudness normalization. Install it with: {hint}")

    step("Stable Audio 3 runtime")
    try:
        upstream = runtime.ensure_runtime(log=lambda msg: print(f"  {msg}", flush=True))
    except runtime.SetupRequired as e:
        fail(str(e))
        return 1
    ok(str(upstream))

    step(f"Model weights: {', '.join(names)}")
    todo = [n for n in names if weights.missing(backend, n, upstream)]
    if not todo:
        ok("already downloaded")
    elif args.skip_weights:
        warn(f"skipped ({weights.download_size(backend, todo)}). Generating needs them: run "
             f"{runtime.setup_command()} again before the first generation.")
    else:
        from huggingface_hub import constants
        retry = f"Run this again: {runtime.setup_command()}"
        if backend == "sa3-torch":
            gated = weights.gated_repos(todo)
            if gated:
                print(weights.gated_help(gated, weights.hf_account(), retry), file=sys.stderr)
                return 2
        source = ("stabilityai/stable-audio-3-optimized" if backend == "sa3-mlx"
                  else ", ".join(weights.TORCH_REPOS[n][0] for n in todo))
        if backend == "sa3-mlx" and all(weights.in_hf_cache(n, upstream) for n in todo):
            print(f"  Already in the Hugging Face cache ({constants.HF_HUB_CACHE}): linking, no download.")
        else:
            partial = backend == "sa3-mlx" and any(
                len(weights.missing(backend, n, upstream)) < weights.file_count(n, upstream) for n in todo)
            size = weights.download_size(backend, todo)
            size = f"the rest of {size}" if partial else size
            print(f"  Download size: {B}{size}{N} from Hugging Face ({source}).\n"
                  "  This can take from a few minutes to an hour depending on the connection; if it\n"
                  "  is interrupted, run setup again and finished files are kept.\n"
                  f"  Saved to the Hugging Face cache: {constants.HF_HUB_CACHE}\n"
                  f"  {weights.LICENSE_NOTE}", flush=True)
            if not args.yes and sys.stdin.isatty() and not confirm("Download now?"):
                print(f"\nNothing downloaded. Run setup again when you are ready: {runtime.setup_command()}")
                return 1
        rc = weights.download(backend, todo, upstream, retry)
        if rc:
            return rc
        ok("weights ready")

    cmd = runtime.run_command()
    head = f"{B}{G}✓ Ready.{N}" if not (todo and args.skip_weights) else f"{B}{Y}Set up, but without the weights.{N}"
    print(f"\n{head} Register the server with your MCP client, e.g. Claude Code:\n\n"
          f"  claude mcp add --scope user audio-studio -- {cmd}\n\n"
          "Claude Desktop, Codex, VS Code: see https://github.com/kanaworksai-hue/audio-studio-mcp#-quick-start\n"
          f"Web UI:  {cmd} --web 8765\n"
          f"Output folder: {runtime.output_dir()}")
    return 0
