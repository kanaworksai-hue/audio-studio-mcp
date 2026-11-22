"""Install or diagnose the optional YuE2 weights and native audio.cpp runtime."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.error import URLError
from urllib.request import urlopen
from http.client import HTTPException
from pathlib import Path

from . import firstrun, gpu, runtime, song_cuda, songs


def _matches(path: Path, size: int, digest: str) -> bool:
    if not songs.has_file(path, size):
        return False
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 << 20), b""):
            sha256.update(chunk)
    return sha256.hexdigest() == digest


def _verify(path: Path, name: str) -> bool:
    return _matches(path, *songs.WEIGHT_FILES[name])


def _install_weights(root: Path, files: list[str], source: Path | None) -> None:
    from huggingface_hub import hf_hub_download

    root.mkdir(parents=True, exist_ok=True)
    remaining = [name for name in files if not _verify(root / name, name)]
    if shutil.disk_usage(root).free < sum(songs.WEIGHT_FILES[name][0] for name in remaining):
        raise RuntimeError("Insufficient free disk space for the selected song weights.")
    stage = root / ".downloads"
    for name in remaining:
        print(f"Installing {name}…", flush=True)
        dest = root / name
        if source is None:
            staged = Path(hf_hub_download(songs.WEIGHTS_REPO, name, revision=songs.WEIGHTS_REV,
                                          local_dir=stage))
        else:
            staged = stage / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, staged)
        if not _verify(staged, name):
            staged.unlink(missing_ok=True)
            raise RuntimeError(f"Size or SHA-256 mismatch for {name}.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(dest)


def _install_metal_runtime(binary: Path) -> None:
    if binary.is_file() and os.access(binary, os.X_OK):
        return
    if not all(shutil.which(name) for name in ("git", "cmake", "xcrun")):
        raise RuntimeError("Metal song setup needs Git, CMake, and Xcode to build the native runtime.")
    env = os.environ.copy()
    xcode = Path("/Applications/Xcode.app/Contents/Developer")
    if "DEVELOPER_DIR" not in env and xcode.is_dir():
        env["DEVELOPER_DIR"] = str(xcode)
    if subprocess.run(["xcrun", "--find", "metal"], env=env, stdout=subprocess.DEVNULL).returncode:
        raise RuntimeError("Xcode's Metal compiler is required. Select full Xcode with DEVELOPER_DIR.")
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=binary.parent) as folder:
        source = Path(folder)
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "fetch", "--depth=1", songs.RUNTIME_REPO, songs.RUNTIME_REV],
                       cwd=source, check=True)
        subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=source, check=True)
        if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() != songs.RUNTIME_REV:
            raise RuntimeError("Unexpected audio.cpp source revision.")
        subprocess.run(["scripts/build_metal.sh", "--deployment-build", "--build-type", "Release",
                        "--archs", "arm64", "--embed-metal", "ON", "--model-set", "custom",
                        "--models", "yue2", "--target", "audiocpp_cli", "-j", "8"],
                       cwd=source, env=env, check=True)
        built = source / "build" / "macos-metal-release" / "bin" / "audiocpp_cli"
        songs.probe_metal(built)
        built.replace(binary)


def _download_asset(name: str, path: Path, size: int) -> None:
    url = f"{song_cuda.RELEASE_URL}/{name}"
    for attempt in range(3):
        try:
            written = 0
            with urlopen(url, timeout=30) as response, path.open("wb") as dest:
                while chunk := response.read(1 << 20):
                    written += len(chunk)
                    if written > size:
                        raise RuntimeError(f"Download exceeds the expected size for {name}.")
                    dest.write(chunk)
            if written == size:
                return
            raise URLError(f"received {written:,} of {size:,} bytes")
        except (URLError, TimeoutError, HTTPException) as error:
            if attempt == 2:
                raise RuntimeError(f"Could not download {name}: {error}. "
                                   "Retry or supply the pinned release ZIPs with --runtime-from.") from error
            time.sleep(attempt + 1)


def _install_cuda_runtime(binary: Path, source: Path | None) -> None:
    if source is None and song_cuda.runtime_problem(binary) is None:
        return
    target = binary.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cuda-", dir=target.parent) as folder:
        stage = Path(folder)
        unpacked = stage / "runtime"
        unpacked.mkdir()
        if source is None:
            needed = sum(size for size, _ in song_cuda.ASSETS.values())
            if shutil.disk_usage(stage).free < needed:
                raise RuntimeError("Insufficient free disk space to download the CUDA runtime ZIPs.")
        archives = []
        for name, (size, digest) in song_cuda.ASSETS.items():
            archive = (stage if source is None else source) / name
            if source is None:
                print(f"Downloading {name} ({size / 1e6:.0f} MB)...", flush=True)
                _download_asset(name, archive, size)
            if not _matches(archive, size, digest):
                raise RuntimeError(f"Missing file or size/SHA-256 mismatch for CUDA release asset {name}.")
            archives.append(archive)
        for archive in archives:
            with zipfile.ZipFile(archive) as package:
                if shutil.disk_usage(stage).free < sum(item.file_size for item in package.infolist()):
                    raise RuntimeError("Insufficient free disk space to unpack the CUDA runtime.")
                package.extractall(unpacked)
            if source is None:
                archive.unlink()
        songs.probe_cuda(unpacked / binary.name)
        previous = stage / "previous"
        if target.exists():
            target.rename(previous)
        try:
            unpacked.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise


def _print_resources(resources: songs.Resources) -> None:
    total = "unknown" if resources.total_gib is None else f"{resources.total_gib:.1f} GiB"
    available = "unknown" if resources.available_gib is None else f"{resources.available_gib:.1f} GiB"
    print(f"{resources.device}: {total} RAM, {available} available.")
    if resources.driver is not None:
        print(f"NVIDIA driver {resources.driver}, compute {resources.compute_capability}, "
              f"{resources.gpu_available_gib:.1f}/{resources.gpu_total_gib:.1f} GiB free VRAM.")


def _diagnose(binary: Path, backend: songs.Backend, config: songs.Policy) -> None:
    if problem := songs.runtime_problem(binary, backend):
        raise songs.SongRuntimeError(f"{problem} Install or repair YuE2 with: {songs.setup_command()}")
    resources = songs.probe_resources(binary, backend)
    _print_resources(resources)
    root = songs.model_dir()
    candidates = songs.profiles(config, backend)
    files = {*songs.SHARED_FILES, *(profile.model_file for profile in candidates)}
    verified = {name for name in sorted(files) if _verify(root / name, name)}
    installed = {profile.id for profile in candidates
                 if profile.model_file in verified and set(songs.SHARED_FILES) <= verified}
    print(f"Verified profiles: {', '.join(sorted(installed)) or 'none'}.")
    for name in sorted(files - verified):
        print(f"Missing or failed size/SHA-256 verification: {name}")
    selection = songs.select(config, resources, installed, songs.SongRequest(seed=0), backend=backend)
    print(f"Auto at {songs.DEFAULT_DURATION_SECONDS} s: {selection.profile.quantization} "
          f"(estimated {selection.estimate_seconds}s).")
    if selection.speed_factor != 1.0:
        print(f"Estimate scaled ×{selection.speed_factor:g} from earlier runs on {resources.device}.")
    for reason in selection.reasons:
        print(reason)


def _auto_profile(backend: songs.Backend, config: songs.Policy,
                  high: songs.Profile, fast: songs.Profile) -> songs.Profile:
    if backend == "metal":
        return fast
    devices = song_cuda.nvidia_devices()
    memory = songs.read_memory(song_cuda.memory_status)
    if memory is None:
        return fast
    total, available = memory
    # After installation the CUDA probe will check the actual visible device.
    free_vram = max(device.free_bytes for device in devices.values()) / songs.GIB
    if (free_vram >= high.tuning.min_gpu_gib
            and total / songs.GIB >= config.min_total_gib
            and available / songs.GIB >= high.tuning.min_available_gib
            and high.estimate(songs.DEFAULT_DURATION_SECONDS) <= config.wait_budget_seconds):
        return high
    return fast


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="audio-studio-mcp song-setup", description=__doc__)
    parser.add_argument("--profile", choices=("auto", "fast", "quality", "both"), default="auto",
                        help="Auto recommends a profile; Fast Q4 (~2.93 GB), Quality Q8 (~4.53 GB), both ~7.20 GB.")
    parser.add_argument("--weights-from", type=Path, help="Import the selected files from a local GGUF snapshot.")
    parser.add_argument("--runtime-from", type=Path, help="Import both pinned Windows CUDA release ZIPs from DIR.")
    parser.add_argument("--check", action="store_true", help="Verify the existing runtime and weights without downloading.")
    parser.add_argument("--accept-license", action="store_true",
                        help="Confirm you have reviewed the YuE2 model weight terms.")
    args = parser.parse_args(argv)
    backend = songs.song_backend()
    if backend is None:
        print(songs.UNSUPPORTED, file=sys.stderr)
        return 1
    if args.runtime_from is not None and backend != "cuda":
        parser.error("--runtime-from applies to Windows CUDA setup.")
    try:
        config = songs.policy(backend)
        binary = songs.binary_path(backend)
        if args.check:
            _diagnose(binary, backend, config)
            return 0
        high, fast = songs.profiles(config, backend)
        # Check the Windows driver before asking to download the runtime.
        recommended = _auto_profile(backend, config, high, fast)
        choices = {"auto": [recommended], "fast": [fast], "quality": [high], "both": [fast, high]}
        selected = choices[args.profile]
        files = [p.model_file for p in selected] + list(songs.SHARED_FILES)
        gb = sum(songs.WEIGHT_FILES[name][0] for name in files) / 1e9
        label = "CUDA 13.3" if backend == "cuda" else "Metal"
        print(f"YuE2 3B for {label}: {', '.join(p.quantization for p in selected)} ({gb:.2f} GB of weights).")
        if args.profile == "auto":
            print(f"Auto recommends {recommended.quantization} for this installation. "
                  "Generation uses current memory readings for recommendations.")
        if backend == "cuda" and (song_cuda.runtime_problem(binary) or args.runtime_from):
            print("The pinned CUDA runtime ZIPs total 0.86 GB and unpack to about 1.12 GB.")
        print("Weights: CC BY-NC 4.0. Review the additional terms for individual creators and companies.")
        print(songs.LICENSE_URL)
        if not args.accept_license:
            if (not firstrun.stdin_is_terminal()
                    or input("Continue with installation? [y/N] ").strip().lower() not in ("y", "yes")):
                print("Review the terms and rerun with --accept-license to install.", file=sys.stderr)
                return 2
        with gpu.file_lease(runtime.data_home() / "songs" / ".install.lock"):
            if backend == "cuda":
                _install_cuda_runtime(binary, args.runtime_from)
            else:
                _install_metal_runtime(binary)
            available = songs.probe_resources(binary, backend)
            _print_resources(available)
            if advisory := songs.ram_advisory(config, available.total_gib):
                print(f"Memory advisory: {advisory}")
            _install_weights(songs.model_dir(), files, args.weights_from)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"audio-studio-mcp song-setup: {error}", file=sys.stderr)
        return 1
    print("Song setup complete. Use generate_song in MCP or open Songs in the Web UI.")
    return 0
