"""Song requests, resource policy and the pinned audio.cpp runtime."""
from __future__ import annotations

import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import gpu, runtime, song_cuda

RUNTIME_REPO = "https://github.com/0xShug0/audio.cpp.git"
RUNTIME_REV = "cf4929424ac27e487d09fb64702c72681cc18ba8"
WEIGHTS_REPO = "audio-cpp/Yue2-3B-GGUF"
WEIGHTS_REV = "cca74d8980c00f30ab8d52248a367c01ad633ec2"
LICENSE_URL = "https://github.com/multimodal-art-projection/YuE/blob/1116bea76032bee5668e5f097294d082fa0b84f0/MODEL_LICENSE"
WEIGHT_FILES = {
    "yue2-3b-q4_0.gguf": (2665641504, "b80215b59a7fe41a4c76b6163c0ae1031672f892b5951d66fefaa2053a815029"),
    "yue2-3b-q8_0.gguf": (4264195616, "52be345b5155bf9ef0b2411e7df8d1d2b8e13b6d977a2f70c35612ec69b1f591"),
    "yue2-vae-f16.gguf": (265218656, "d4f4a05d8f291ae820cd1e43609da3fa91b56465810091a2b08c3350b751719d"),
    "sidecars/yue2-model-config.json": (959, "ad3477bbef890bf98ae196c1e4b44779494a6231c4ab66f32708eabadf265329"),
    "sidecars/yue2-generation-config.json": (466, "203830cebde6e3644eb291925362990d198e66c3b6006cd11f1aaa21904bcc61"),
    "sidecars/yue2-qwen.tiktoken": (2561218, "b2b1b8dfb5cc5f024bafc373121c6aba3f66f9a5a0269e243470a1de16a33186"),
    "sidecars/yue2-vae-config.json": (1378, "f0191bb9694009956de44e0c361a6f1334760be4c8f848e599bde242a54a0970"),
}
VAE = "yue2-vae-f16.gguf"
SHARED_FILES = (VAE, *(name for name in WEIGHT_FILES if name.startswith("sidecars/")))
GIB = 1 << 30
SPEED_FACTOR_LIMITS = (0.25, 8.0)
DEFAULT_DURATION_SECONDS = 180
MAX_DURATION_SECONDS = 240
DURATION_LIMIT_FADE_MS = 2000
Mode = Literal["auto", "fast", "quality"]
Backend = Literal["metal", "cuda"]
UNSUPPORTED = "Song generation requires native Apple Silicon or Windows x64 with an NVIDIA CUDA GPU."
Lyrics = Annotated[str, StringConstraints(max_length=16000), Field(
    description="Original lyrics in their language, with section tags such as [Verse], [Chorus] and [Bridge].")]
Style = Annotated[str, StringConstraints(max_length=1200), Field(
    description="Genre, mood, tempo, instruments and vocal character, e.g. 'Japanese vocals, warm city pop, 100 BPM'.")]
Duration = Annotated[int, Field(
    ge=8, le=MAX_DURATION_SECONDS,
    description="Maximum song length in seconds. For 30 seconds or less, use a single short chorus "
                "and ask for the lead vocal on the first beat in the style prompt.",
)]
Budget = Annotated[int | None, Field(ge=1, le=3600, description=(
    "Auto only: the longest acceptable wait in seconds. Auto skips profiles whose estimate exceeds it."))]
Replay = Annotated[str | None, Field(max_length=4096, description=(
    "Path of a song WAV in the output folder. Replays its saved lyrics, style, seed, profile and ending; "
    "other arguments are ignored."))]
Title = Annotated[str | None, Field(max_length=200, description="Short name used for the file name and metadata.")]
ModeArg = Annotated[Mode, Field(description=(
    "auto = recommends an installed profile using memory estimates and the wait budget; fast = Q4_0; quality = Q8_0."))]


class SongRequest(BaseModel):
    lyrics: Lyrics = ""
    style: Style = ""
    title: Title = None
    duration_seconds: Duration = DEFAULT_DURATION_SECONDS
    seed: Annotated[int, Field(ge=0, le=2**31 - 5)]
    mode: Mode = "auto"
    wait_budget_seconds: Budget = None


class ProfileTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_available_gib: Annotated[float, Field(gt=0)]
    min_gpu_gib: Annotated[float, Field(gt=0)]
    setup_seconds: Annotated[float, Field(ge=0)]
    seconds_per_second: Annotated[float, Field(gt=0)]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_total_gib: Annotated[float, Field(gt=0)] = 32
    wait_budget_seconds: Annotated[int, Field(ge=1, le=3600)] = 240
    q4: ProfileTuning = ProfileTuning(min_available_gib=10, min_gpu_gib=5,
                                     setup_seconds=20, seconds_per_second=1.17)
    q8: ProfileTuning = ProfileTuning(min_available_gib=14.5, min_gpu_gib=7,
                                     setup_seconds=22, seconds_per_second=1.45)


class CudaPolicy(Policy):
    # Windows may report slightly less usable physical memory than installed.
    min_total_gib: Annotated[float, Field(gt=0)] = 31
    wait_budget_seconds: Annotated[int, Field(ge=1, le=3600)] = 180
    q4: ProfileTuning = ProfileTuning(min_available_gib=8, min_gpu_gib=6,
                                     setup_seconds=20, seconds_per_second=.80)
    q8: ProfileTuning = ProfileTuning(min_available_gib=10, min_gpu_gib=8.2,
                                     setup_seconds=47, seconds_per_second=.72)


@dataclass(frozen=True)
class Profile:
    id: str
    quantization: str
    tuning: ProfileTuning

    @property
    def model_file(self) -> str:
        return f"yue2-3b-{self.quantization.lower()}.gguf"

    def estimate(self, seconds: int, speed: float = 1.0) -> int:
        """Policy estimate, scaled by this machine's measured speed factor when one is known."""
        return math.ceil((self.tuning.setup_seconds + self.tuning.seconds_per_second * seconds) * speed)

    def parameters(self, duration_seconds: int) -> dict:
        return {"cot": "full", "guidance_scale": 1.0, "num_inference_steps": 8,
                "semantic_min_tokens": 200, "semantic_max_tokens": 25 * duration_seconds}


@dataclass(frozen=True)
class Resources:
    total_gib: float | None
    available_gib: float | None
    gpu_available_gib: float
    pressure: int | None
    device: str
    device_index: int | None = None
    gpu_total_gib: float | None = None
    driver: str | None = None
    compute_capability: str | None = None


@dataclass(frozen=True)
class Selection:
    profile: Profile
    estimate_seconds: int
    reasons: tuple[str, ...]
    speed_factor: float = 1.0

    def view(self) -> dict:
        view = {"profile": self.profile.id, "model": "YuE2 3B", "quantization": self.profile.quantization,
                "eta_seconds": self.estimate_seconds, "reasons": list(self.reasons)}
        if self.speed_factor != 1.0:
            view["speed_factor"] = self.speed_factor
        return view


class SongRuntimeError(RuntimeError):
    pass


class SongMemoryError(SongRuntimeError):
    pass


def policy(backend: Backend = "metal") -> Policy:
    model = CudaPolicy if backend == "cuda" else Policy
    config = os.environ.get("AUDIO_STUDIO_SONG_POLICY")
    if not config:
        return model()
    path = Path(config).expanduser()
    if not path.is_absolute():
        raise ValueError("AUDIO_STUDIO_SONG_POLICY must be an absolute path.")
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def profiles(config: Policy, backend: Backend = "metal") -> tuple[Profile, Profile]:
    return (Profile(f"{backend}-q8", "Q8_0", config.q8), Profile(f"{backend}-q4", "Q4_0", config.q4))


def setup_command(mode: Mode = "auto") -> str:
    option = f" --profile {mode}" if mode != "auto" else ""
    return f"{runtime.run_command()} song-setup{option}"


def model_dir() -> Path:
    return runtime.data_home() / "songs" / f"yue2-{WEIGHTS_REV[:12]}"


def song_backend() -> Backend | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine == "arm64":
        return "metal"
    if sys.platform == "win32" and machine in ("amd64", "x86_64") and sys.maxsize > 2**32:
        return "cuda"
    return None


def runtime_revision(backend: Backend) -> str:
    return song_cuda.REVISION if backend == "cuda" else RUNTIME_REV


def binary_path(backend: Backend) -> Path:
    folder = f"{backend}-{runtime_revision(backend)[:12]}"
    filename = "audiocpp_cli.exe" if backend == "cuda" else "audiocpp_cli"
    return runtime.data_home() / "songs" / folder / filename


def runtime_problem(binary: Path, backend: Backend) -> str | None:
    if backend == "cuda":
        return song_cuda.runtime_problem(binary)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return f"YuE2 Metal runtime is not installed: {binary.name} is missing or not executable."
    return None


def supported() -> bool:
    return song_backend() is not None


def has_file(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def installed_profiles(root: Path, candidates: tuple[Profile, ...]) -> set[str]:
    if not all(has_file(root / name, WEIGHT_FILES[name][0]) for name in SHARED_FILES):
        return set()
    return {p.id for p in candidates if has_file(root / p.model_file, WEIGHT_FILES[p.model_file][0])}


def ram_advisory(config: Policy, total_gib: float | None) -> str | None:
    if total_gib is not None and total_gib < config.min_total_gib:
        return (f"{total_gib:.1f} GiB installed RAM is below the song recommendation "
                f"of {config.min_total_gib:g} GiB.")
    return None


def _mac_pressure_advisory(level: int | None, during_run: bool = False) -> str | None:
    if level is not None and level > 1:
        severity = "critical" if level >= 4 else "elevated"
        action = "You can cancel this song." if during_run else "Generation may be slow or fail."
        return f"macOS memory pressure is {severity}. {action}"
    return None


def _profile_advisories(profile: Profile, resources: Resources, backend: Backend) -> list[str]:
    tune = profile.tuning
    name = profile.quantization
    notes = []
    if resources.available_gib is not None and resources.available_gib < tune.min_available_gib:
        notes.append(f"{name} recommends {tune.min_available_gib:g} GiB available RAM "
                     f"({resources.available_gib:.1f} GiB now).")
    if resources.gpu_available_gib < tune.min_gpu_gib:
        memory = "free NVIDIA VRAM" if backend == "cuda" else "Metal working-set headroom"
        notes.append(f"{name} recommends {tune.min_gpu_gib:g} GiB {memory} "
                     f"({resources.gpu_available_gib:.1f} GiB now).")
    return notes


def select(config: Policy, resources: Resources, installed: set[str],
           request: SongRequest, *, pinned: str | None = None,
           excluded: set[str] | None = None, backend: Backend = "metal") -> Selection:
    """Rank installed profiles within the Auto time budget, using memory as advice."""
    high, fast = profiles(config, backend)
    auto = pinned is None and request.mode == "auto"
    candidates = [p for p in (high, fast) if p.id == pinned] if pinned else {
        "auto": [high, fast], "fast": [fast], "quality": [high],
    }[request.mode]
    if not candidates:
        raise ValueError("The original song profile is unavailable on this platform.")
    budget = request.wait_budget_seconds or config.wait_budget_seconds
    speed = speed_factors(candidates, resources.device, request.duration_seconds)
    common = tuple(filter(None, (ram_advisory(config, resources.total_gib),
                                 _mac_pressure_advisory(resources.pressure))))
    reasons: list[str] = []
    fallback = None
    for p in candidates:
        factor = speed.get(p.id, 1.0)
        estimate = p.estimate(request.duration_seconds, factor)
        if p.id in (excluded or ()):
            reasons.append(f"{p.quantization} ran out of memory.")
        elif p.id not in installed:
            reasons.append(f"{p.quantization} weights are not installed.")
        elif auto and estimate > budget:
            reasons.append(f"{p.quantization} estimate {estimate}s exceeds the {budget}s Auto budget.")
        else:
            advisories = _profile_advisories(p, resources, backend)
            selection = Selection(p, estimate, (*reasons, *common, *advisories), factor)
            if not auto or not (common or advisories) or p == fast:
                return selection
            fallback = selection
            reasons.extend(advisories)
    if fallback is not None:
        return fallback
    detail = (" ".join(reasons) + " Check the duration and installed profiles.")
    if any(p.id not in installed for p in candidates):
        missing = next(p for p in candidates if p.id not in installed)
        mode = "quality" if missing.id == high.id else "fast"
        detail += f" Install missing weights with: {setup_command(mode)}"
    raise SongRuntimeError(detail)


def _speed_path() -> Path:
    return runtime.data_home() / "songs" / "speed.json"


def _speed_key(profile_id: str, device: str, duration_seconds: float) -> str:
    if duration_seconds <= 60:
        band = "short"
    elif duration_seconds < 150:
        band = "medium"
    else:
        band = "long"
    revision = runtime_revision(profile_id.split("-")[0])[:12]
    # Legacy factors lack a sample duration, so they cannot seed a band safely.
    return f"{profile_id}|{device}|{revision}|{band}"


def _read_speed() -> dict:
    try:
        saved = json.loads(_speed_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _bounded_factor(value) -> float | None:
    low, high = SPEED_FACTOR_LIMITS
    if isinstance(value, (int, float)) and not isinstance(value, bool) and -math.inf < value < math.inf:
        return float(min(max(value, low), high))
    return None


def speed_factors(candidates, device: str, duration_seconds: int) -> dict[str, float]:
    """Measured synthesis-time / policy-estimate ratios for this device and length band."""
    saved = _read_speed()
    factors: dict[str, float] = {}
    for p in candidates:
        value = _bounded_factor(saved.get(_speed_key(p.id, device, duration_seconds)))
        if value is not None:
            factors[p.id] = value
    return factors


def record_speed(profile: Profile, device: str, duration_seconds: float, seconds: float) -> float | None:
    """Blend one finished run into the stored speed factor. Returns the new factor."""
    if not (0 < duration_seconds < math.inf and 0 < seconds < math.inf):
        return None
    baseline = profile.tuning.setup_seconds + profile.tuning.seconds_per_second * duration_seconds
    if baseline <= 0:
        return None
    path = _speed_path()
    saved = _read_speed()
    low, high = SPEED_FACTOR_LIMITS
    observed = min(max(seconds / baseline, low), high)
    key = _speed_key(profile.id, device, duration_seconds)
    previous = _bounded_factor(saved.get(key))
    if previous is not None:
        observed = (previous + observed) / 2
    saved[key] = round(observed, 4)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_suffix(".json.tmp")
        staged.write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")
        staged.replace(path)
    except OSError:
        return None
    return saved[key]


def _read_command(*args) -> str:
    return subprocess.check_output(args, text=True, timeout=10).strip()


def memory_pressure() -> int:
    return int(_read_command("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"))


def metal_installed_ram() -> float:
    try:
        total = int(_read_command("/usr/sbin/sysctl", "-n", "hw.memsize"))
        if total <= 0:
            raise ValueError("hw.memsize must be positive")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SongRuntimeError(f"Could not read Apple Silicon installed RAM (hw.memsize): {error}") from error
    return total / GIB


def installed_ram(backend: Backend) -> float | None:
    if backend == "metal":
        return read_memory(metal_installed_ram)
    memory = read_memory(song_cuda.memory_status)
    return memory[0] / GIB if memory is not None else None


def _metal_available_ram() -> float:
    vm = _read_command("/usr/bin/vm_stat")
    size = re.search(r"page size of (\d+) bytes", vm)
    pages = {k.lower(): int(v) for k, v in re.findall(r"^Pages (\w+):\s+(\d+)\.", vm, re.M)}
    needed = ("free", "inactive", "speculative")
    if size is None or not all(page in pages for page in needed):
        raise ValueError("vm_stat did not report available memory pages.")
    return sum(pages[page] for page in needed) * int(size[1]) / GIB


def read_memory(read):
    """Memory telemetry is advisory even when a system reading is unavailable."""
    try:
        return read()
    except (OSError, ValueError, SongRuntimeError, subprocess.SubprocessError):
        return None


def probe_metal(binary: Path) -> Resources:
    try:
        result = subprocess.run([str(binary), "--list-devices", "--json"], text=True,
                                capture_output=True, timeout=60, check=True)
        devices = json.loads(result.stdout)["devices"]
        metal = next(item for item in devices if item["backend"] == "metal")
        return Resources(
            total_gib=read_memory(metal_installed_ram),
            available_gib=read_memory(_metal_available_ram),
            pressure=read_memory(memory_pressure),
            gpu_available_gib=metal["memory"]["free_bytes"] / GIB,
            device=metal["description"],
        )
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, StopIteration) as error:
        raise SongRuntimeError("Could not read Apple Silicon or Metal resources. "
                               f"{str(error) or type(error).__name__}. "
                               f"Check the song runtime with: {setup_command()}") from error


def probe_cuda(binary: Path) -> Resources:
    try:
        device = song_cuda.probe(binary)
    except (OSError, RuntimeError) as error:
        raise SongRuntimeError(f"{error} Check the song runtime with: {setup_command()}") from error
    memory = read_memory(song_cuda.memory_status)
    total, available = (value / GIB for value in memory) if memory is not None else (None, None)
    return Resources(total, available, device.free_bytes / GIB, 1,
                     device.name, device.index, device.total_bytes / GIB, device.driver, device.compute)


def probe_resources(binary: Path, backend: Backend) -> Resources:
    return probe_cuda(binary) if backend == "cuda" else probe_metal(binary)


def check_request(request: SongRequest) -> SongRequest:
    if not request.lyrics.strip() or not request.style.strip():
        raise ValueError("Song lyrics and style are required.")
    return request


def replay_request(meta: dict) -> tuple[SongRequest, str, int]:
    if meta.get("kind") != "song":
        raise ValueError("Choose a WAV generated by generate_song.")
    backend = song_backend()
    if (backend is None or meta.get("runtime_revision") != runtime_revision(backend)
            or meta.get("weights_revision") != WEIGHTS_REV):
        raise ValueError("This song used a runtime or weights revision that is unavailable in this build.")
    try:
        request = check_request(SongRequest.model_validate(meta["request"]))
        pinned = meta["profile"]
        profile = next(p for p in profiles(policy(backend), backend) if p.id == pinned)
        if profile.parameters(request.duration_seconds) != meta["parameters"]:
            raise ValueError("The saved synthesis parameters are unavailable in this build.")
        # Sidecars written before the fade existed replay with the original ending.
        fade_ms = meta.get("duration_limit_fade_ms", 0)
        if type(fade_ms) is not int or fade_ms not in (0, DURATION_LIMIT_FADE_MS):
            raise ValueError("The saved song fade is unavailable in this build.")
        return request, pinned, fade_ms
    except (KeyError, StopIteration) as error:
        raise ValueError("The saved song metadata is incomplete.") from error


_STAGE = {
    "yue2.ar.weights.buffer_name": "loading",
    "yue2.ar.init_ms": "planning",
    "yue2.semantic.abc_generated_tokens": "lyrics",
    "yue2.nar.weights.buffer_name": "synthesis",
    "yue2.vae.init_ms": "decoding",
}
_MEMORY_FAILURE = re.compile(
    r"out of memory|cannot allocate memory|failed to allocate|insufficient memory|"
    r"cudaErrorMemoryAllocation|CUBLAS_STATUS_ALLOC_FAILED|paging file is too small", re.I,
)


def runtime_options(backend: Backend, device: int | None) -> dict:
    options = {"backend": backend, "threads": 8, "perf_mode": "off", "vae": VAE}
    if backend == "cuda":
        options.update(device=device, mem_saver=True, vae_tile_frames=512, attention="auto")
    return options


def _running_memory_advisory(backend: Backend) -> str | None:
    if backend == "cuda":
        memory = read_memory(song_cuda.memory_status)
        if memory is not None and memory[1] < GIB:
            return "Available Windows RAM or commit headroom is below 1 GiB. You can cancel this song."
        return None
    return _mac_pressure_advisory(read_memory(memory_pressure), during_run=True)


def _run_cli(binary, weights, request, profile, work, cancel, update, *,
             backend: Backend = "metal", device: int | None = None,
             selection: Selection | None = None) -> tuple[float, bool]:
    path = work / "request.json"
    output = work / "result"
    output.mkdir()
    path.write_text(json.dumps([{
        "id": "song", "lyrics": request.lyrics, "seed": request.seed,
        "options": {**profile.parameters(request.duration_seconds), "style": request.style},
    }], ensure_ascii=False), encoding="utf-8")
    options = runtime_options(backend, device)
    command = [
        str(binary), "--family", "yue2", "--task", "gen", "--backend", backend,
        "--threads", str(options["threads"]), "--model", str(weights),
        "--session-option", f"yue2.model_gguf={profile.model_file}",
        "--session-option", f"yue2.vae_gguf={options['vae']}",
        "--session-option", f"yue2.perf_mode={options['perf_mode']}",
        "--request-sequence", str(path), "--out-dir", str(output),
        "--log", "--load-progress", "1", "--metrics",
    ]
    process_options = {}
    if backend == "cuda":
        command.extend(["--device", str(device), "--session-option",
                        f"yue2.mem_saver={str(options['mem_saver']).lower()}", "--session-option",
                        f"yue2.vae_tile_frames={options['vae_tile_frames']}", "--session-option",
                        f"yue2.attention={options['attention']}"])
        process_options = song_cuda.process_options()
    start = last_pressure_check = time.monotonic()
    phase, advisory = "loading", None
    log = work / "runtime.log"
    with log.open("wb") as log_out, log.open("r", encoding="utf-8", errors="replace") as log_in:
        with subprocess.Popen(command, cwd=work, stdin=subprocess.DEVNULL, stdout=log_out,
                              stderr=subprocess.STDOUT, **process_options) as process:
            try:
                while process.poll() is None:
                    if cancel.wait(0.2):
                        raise gpu.Cancelled()
                    for line in log_in:
                        for event, next_phase in _STAGE.items():
                            if event in line:
                                phase = next_phase
                                update(phase)
                    now = time.monotonic()
                    if now - last_pressure_check >= 2:
                        last_pressure_check = now
                        if selection is not None:
                            current = _running_memory_advisory(backend)
                            if current != advisory:
                                advisory = current
                                view = selection.view()
                                if advisory is not None:
                                    view["reasons"].append(advisory)
                                update(phase, view)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    if cancel.is_set():
        raise gpu.Cancelled()
    log_text = log.read_text(encoding="utf-8", errors="replace")
    if process.returncode != 0:
        windows_memory_error = (backend == "cuda"
                                and (process.returncode & 0xFFFFFFFF) in (0xC0000017, 0xC000009A))
        if windows_memory_error or _MEMORY_FAILURE.search(log_text):
            raise SongMemoryError("audio.cpp reported an allocation failure. Free GPU or system memory and retry.")
        if backend == "metal" and process.returncode == -signal.SIGKILL:
            # Not our own cancel (handled above): macOS kills processes this way under memory pressure.
            raise SongMemoryError("macOS stopped audio.cpp (SIGKILL), usually because memory ran out. "
                                  "Close other apps and retry.")
        hint = song_cuda.launch_problem(process.returncode) if backend == "cuda" else ""
        detail = next((line.strip()[:300] for line in reversed(log_text.splitlines())
                       if "error" in line.lower()), "")
        raise SongRuntimeError(f"audio.cpp exited with code {process.returncode}. {hint or detail} "
                               f"Verify the runtime and weights with: {setup_command()}")
    ended = re.search(r"yue2\.semantic\.truncated ([01])", log_text)
    if ended is None:
        raise SongRuntimeError("audio.cpp did not report how the song ended.")
    return time.monotonic() - start, ended[1] == "1"


def _fade_song_tail(audio: Path, fade_ms: int) -> None:
    """Fade the staged PCM WAV in place, leaving the prefix and frame count intact."""
    try:
        with sf.SoundFile(audio, "r+") as wav:
            frames = min(wav.frames, wav.samplerate * fade_ms // 1000)
            start = wav.frames - frames
            wav.seek(start)
            tail = wav.read(frames, dtype="float32", always_2d=True)
            envelope = np.linspace(1, 0, frames, dtype=np.float32)
            envelope[-1] = 0
            wav.seek(start)
            wav.write(tail * envelope[:, None])
    except (OSError, RuntimeError) as error:
        raise SongRuntimeError("Could not apply the song's duration-limit fade.") from error


def generate(request: SongRequest, work: Path, cancel, update, *, pinned: str | None = None,
             duration_limit_fade_ms: int = DURATION_LIMIT_FADE_MS) -> tuple[Path, dict]:
    """Called on a worker with the GPU lease. The caller publishes its validated WAV."""
    backend = song_backend()
    if backend is None:
        raise SongRuntimeError(UNSUPPORTED)
    config = policy(backend)
    binary, weights = binary_path(backend), model_dir()
    if problem := runtime_problem(binary, backend):
        raise runtime.SetupRequired(f"{problem} Install or repair YuE2 with: {setup_command(request.mode)}")
    installed = installed_profiles(weights, profiles(config, backend))
    if not installed:
        raise runtime.SetupRequired(f"Install the optional YuE2 song weights with: {setup_command(request.mode)}")
    excluded: set[str] = set()
    options = request
    start = time.monotonic()
    while True:
        resources = probe_resources(binary, backend)
        selection = select(config, resources, installed, options, pinned=pinned, excluded=excluded, backend=backend)
        p = selection.profile
        update("loading", selection.view())
        if cancel.is_set():
            raise gpu.Cancelled()
        attempt = work / p.id
        attempt.mkdir()
        try:
            generation_time, truncated = _run_cli(binary, weights, request, p, attempt, cancel, update,
                                                  backend=backend, device=resources.device_index, selection=selection)
            break
        except SongMemoryError:
            if pinned is not None or request.mode != "auto" or p.quantization == "Q4_0":
                raise
            remaining = (request.wait_budget_seconds or config.wait_budget_seconds) - math.ceil(time.monotonic() - start)
            if remaining <= 0:
                raise SongMemoryError("Q8 ran out of memory and exhausted the Auto time budget.") from None
            options = request.model_copy(update={"wait_budget_seconds": remaining})
            excluded.add(p.id)
            update("checking")
    audio = attempt / "result" / "song.wav"
    try:
        info = sf.info(str(audio))
    except (OSError, RuntimeError) as error:
        raise SongRuntimeError("audio.cpp did not produce a readable WAV.") from error
    if (info.format, info.subtype, info.samplerate, info.channels) != ("WAV", "PCM_16", 48000, 2) or not info.frames:
        raise SongRuntimeError("audio.cpp produced an invalid song WAV (expected 48 kHz stereo PCM16).")
    if truncated and duration_limit_fade_ms:
        _fade_song_tail(audio, duration_limit_fade_ms)
    record_speed(p, resources.device, min(info.duration, request.duration_seconds), generation_time)
    metadata = {
        "kind": "song", "audio_type": "song", "title": request.title, "seed": request.seed,
        "prompt": request.style, "prompt_used": request.style, "model": "YuE2 3B", "backend": f"audiocpp-{backend}",
        "weights_repo": WEIGHTS_REPO, "weights_revision": WEIGHTS_REV,
        "runtime_revision": runtime_revision(backend),
        "runtime_options": runtime_options(backend, resources.device_index),
        "quantization": p.quantization, "profile": p.id, "parameters": p.parameters(request.duration_seconds),
        "request": request.model_dump(), "device": resources.device,
        "resources": {key: value for key, value in asdict(resources).items() if value is not None},
        "sample_rate": 48000, "channels": 2, "bit_depth": 16, "seconds": info.duration,
        "wall_seconds": round(time.monotonic() - start, 3), "synthesis_seconds": round(generation_time, 3),
        "end_reason": "duration_limit" if truncated else "eos",
        "duration_limit_fade_ms": duration_limit_fade_ms,
        "selection": selection.view(),
    }
    return audio, metadata
