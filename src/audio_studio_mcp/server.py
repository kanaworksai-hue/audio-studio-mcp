"""Audio Studio MCP server: local BGM and sound-effect generation.

Tools: generate_audio, edit_audio (variation / inpaint / continue),
process_audio (trim, loop, fades, normalize, 48 kHz WAV/OGG/FLAC export; see
post.py) and unload_models. Every output gets a .json metadata sidecar.

The model runtime is a pluggable backend (backends/): Stable Audio 3 on Apple
Silicon via MLX, or on NVIDIA GPUs (Windows / Linux) via PyTorch. Models load lazily on the first call and are
freed after AUDIO_STUDIO_IDLE_UNLOAD_MIN minutes without a request (each MCP
client session spawns its own stdio server, so keeping ~5 GB resident per
process forever is not an option on a 16 GB Mac).

Run:  audio-studio-mcp              (stdio, what MCP clients launch)
      audio-studio-mcp --http 8765  (one shared streamable-HTTP server on 127.0.0.1)
      audio-studio-mcp --web 8765   (the same /mcp plus the web UI at http://127.0.0.1:8765/; see web.py)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AliasChoices, Field

from . import post
from .backends import Backend, GenerationRequest, create_backend
from .prompts import DEFAULT_SECONDS, INSTRUCTIONS, prepare_prompt, strip_prefix

REPO_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = Path(os.environ.get("AUDIO_STUDIO_OUTPUT_DIR") or REPO_DIR / "outputs").expanduser().resolve()
IDLE_UNLOAD_MIN = float(os.environ.get("AUDIO_STUDIO_IDLE_UNLOAD_MIN", "10"))
# The MLX backend chdirs into its runtime; resolve relative inputs against the
# directory the server was started from instead.
_START_CWD = Path.cwd()
MAX_STEM_BYTES = 160  # UTF-8; Japanese names take 3 bytes a character

_backend: Backend | None = None


def get_backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = create_backend()
    return _backend


def set_backend(backend: Backend | None) -> None:
    """Swap the backend (tests)."""
    global _backend
    _backend = backend


# ── helpers ──────────────────────────────────────────────────────────────────
def _slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^\w\-]+", "_", text)[:n].strip("_")


def _out_path(stem: str, ext: str = ".wav") -> Path:
    """Reserve a new file name in OUTPUT_DIR (stem.ext, stem_2.ext, …).

    The empty file is created atomically, so parallel tool calls and other
    server processes sharing the folder never pick the same name."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[\\/:*?"<>|\s]+', "_", stem).strip("_") or "audio"
    # chained edits append _inpaint, _proc, … to the input's name; keep room for
    # _vN_N.ext.json under the 255-byte file name limit (and Windows paths)
    base, suffix = re.fullmatch(r"(.*?)(_v\d+)?", stem).groups(default="")  # keep the candidate number
    base = base.encode()[:MAX_STEM_BYTES - len(suffix)].decode(errors="ignore").strip("_") or "audio"
    stem = base + suffix
    n = 1
    while True:
        path = OUTPUT_DIR / (f"{stem}{ext}" if n == 1 else f"{stem}_{n}{ext}")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            n += 1


def _auto_stem(title: str | None, prompt: str, seed: int) -> str:
    if title:
        return _slug(title, 80)
    return f"{datetime.now():%Y%m%d-%H%M%S}_{_slug(strip_prefix(prompt)) or 'audio'}_{seed}"


def _input_path(p: str) -> Path:
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = _START_CWD / path
    path = path.resolve()
    if not path.is_file():
        raise ToolError(f"input_audio not found: {path}")
    return path


def _save_generated(backend: Backend, audio: np.ndarray, path: Path,
                    ceiling_db: float | None, meta: dict) -> dict:
    """Write a 24-bit WAV at the model rate (+ .json sidecar) after the safety ceiling."""
    raw = post.stats(audio)
    gain = 0.0
    if ceiling_db is not None:
        audio, gain = post.peak_ceiling(audio, ceiling_db)
    post.write(path, audio, backend.sample_rate, "wav")
    info = {"path": str(path), "seed": meta["seed"],
            "peak_dbfs": post.stats(audio)["peak_dbfs"],
            "raw_peak_dbfs": raw["peak_dbfs"], "ceiling_gain_db": round(gain, 2)}
    meta = {**meta, "file": path.name, "sample_rate": backend.sample_rate, "bit_depth": 24,
            "created": datetime.now().isoformat(timespec="seconds"),
            "backend": backend.name, "model": backend.model_label,
            **{k: v for k, v in info.items() if k != "path"}}
    info["metadata"] = str(post.write_sidecar(path, meta))
    return info


def _audio_seconds(path: Path) -> float:
    import soundfile as sf
    return float(sf.info(str(path)).duration)


def _load_sidecar(path: Path) -> dict | None:
    p = path.with_suffix(path.suffix + ".json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _summary(backend: Backend, prompt: str, seconds: float, n: int,
             wall: float, included_load: bool) -> dict:
    return {
        "prompt_used": prompt,
        "seconds": round(seconds, 2),
        "model": backend.model_label,
        "wall_seconds": round(wall, 2),
        "realtime_factor": round(seconds * n / max(wall, 1e-9), 2),
        "included_model_load": included_load,
    }


def _seeds(seed: int | None, n: int) -> list[int]:
    base = int(seed) if seed is not None and seed >= 0 else int(np.random.randint(0, 2**31 - 5))
    return [base + i for i in range(n)]


# ── MCP server ───────────────────────────────────────────────────────────────
_preload_when_ready = False


@contextlib.asynccontextmanager
async def _lifespan(_app):
    # The stdio transport claims fd 1 when it starts, but only if sys.stdout is
    # still the real stdout. Model loading swaps sys.stdout for stderr on its
    # worker thread, so a preload racing the transport start would send every
    # JSON-RPC reply to stderr. The lifespan runs once the transport is up.
    global _preload_when_ready
    if _preload_when_ready and _backend is not None:
        _preload_when_ready = False
        _backend.preload()
    yield {}


server = MCPServer(name="audio-studio", instructions=INSTRUCTIONS, lifespan=_lifespan)


def _tool(fn):
    """Register an MCP tool whose failures reach the client as readable errors.

    The MCP SDK replaces any exception other than ToolError with a generic
    "Error executing tool" message, which would hide e.g. a missing ffmpeg or
    a failed weight download from the model and the user."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except ValueError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return server.tool()(wrapper)


AudioType = Literal["music", "instrument", "sfx", "raw"]
_Seed = Annotated[int | None, Field(ge=-1, le=2**31 - 5, description="Base seed for reproducibility; omit or -1 for random.")]
_Title = Annotated[str | None, Field(description="Short name; used as the file name and stored in metadata. Auto-named if omitted.")]
_Idea = Annotated[str, Field(description="The user's original request, verbatim and in their language. Stored in metadata only.")]
_Ceiling = Annotated[float | None, Field(ge=-24, le=0, description="Safety ceiling in dBFS: a louder peak is turned down to this, quieter audio is untouched. null disables.")]
# Clients sometimes send "seconds" (the name used in results and sidecars); accept it as well.
_SECONDS_ALIASES = AliasChoices("duration_seconds", "seconds")


@_tool
async def generate_audio(
    prompt: Annotated[str, Field(description="English description (see server instructions). Optional for sfx when the sound_* fields are given.")] = "",
    audio_type: Annotated[AudioType, Field(description="music = BGM / full instrumental track; instrument = solo instrument, stem or loop; sfx = sound effect, ambience or one-shot; raw = no prefix (prompt used verbatim)")] = "music",
    duration_seconds: Annotated[float | None, Field(ge=0.5, le=380, validation_alias=_SECONDS_ALIASES, description="Length in seconds. Default: music 60, instrument 12, sfx 5, raw 30.")] = None,
    variations: Annotated[int, Field(ge=1, le=4, description="Number of candidates (different seeds) to generate in one call.")] = 1,
    bpm: Annotated[int | None, Field(ge=30, le=300, description="Tempo for music/instrument; appended as 'BPM: N.'")] = None,
    sound_source: Annotated[str, Field(description="SFX: what makes the sound (object, instrument, creature, machine).")] = "",
    sound_action: Annotated[str, Field(description="SFX: how it is triggered and evolves over time (attack, decay, movement).")] = "",
    sound_material: Annotated[str, Field(description="SFX: material, texture, timbre.")] = "",
    sound_space: Annotated[str, Field(description="SFX: space and recording (indoor/outdoor, distance, dry/reverberant, mic).")] = "",
    seed: _Seed = None,
    negative_prompt: Annotated[str | None, Field(description="Qualities to avoid. Only takes effect when cfg != 1.")] = None,
    cfg: Annotated[float, Field(ge=0, le=15, description="Guidance. 1.0 = official default; 3–7 = stricter prompt adherence (≈2× slower).")] = 1.0,
    steps: Annotated[int, Field(ge=1, le=50, description="Sampling steps; 8 is the official default.")] = 8,
    title: _Title = None,
    original_idea: _Idea = "",
    peak_ceiling_db: _Ceiling = -1.0,
) -> dict:
    """Generate BGM, instrument loops/stems or sound effects from text with Stable Audio 3 (runs locally). Returns one WAV path per candidate."""
    backend = get_backend()
    sfx_fields = (sound_source, sound_action, sound_material, sound_space)
    text, seconds = prepare_prompt(prompt, audio_type, duration_seconds, bpm, sfx_fields)
    seconds = float(min(seconds or DEFAULT_SECONDS[audio_type], backend.max_seconds))
    seeds = _seeds(seed, variations)
    res = await backend.generate(seeds, GenerationRequest(
        prompt=text, negative_prompt=negative_prompt or "", seconds=seconds,
        steps=steps, cfg=cfg))

    meta = {"kind": "generate", "title": title, "original_idea": original_idea,
            "audio_type": audio_type, "prompt": prompt, "prompt_used": text,
            "sound_fields": dict(zip(("source", "action", "material", "space"), sfx_fields)),
            "bpm": bpm, "seconds": seconds, "negative_prompt": negative_prompt,
            "cfg": cfg, "steps": steps, "variations": variations}
    stem = _auto_stem(title, text, seeds[0])
    candidates = []
    for i, (sd, audio) in enumerate(zip(seeds, res.audios)):
        path = _out_path(stem if variations == 1 else f"{stem}_v{i + 1}")
        candidates.append(_save_generated(backend, audio, path, peak_ceiling_db,
                                          {**meta, "seed": sd, "candidate": i + 1}))
    return {**_summary(backend, text, seconds, variations, res.wall_seconds,
                       res.included_model_load), "candidates": candidates}


@_tool
async def edit_audio(
    input_audio: Annotated[str, Field(description="Absolute path to an existing audio file (wav/mp3/flac/ogg).")],
    mode: Annotated[Literal["variation", "inpaint", "continue"], Field(description="variation = audio-to-audio remix/style or timbre transfer of the whole clip; inpaint = regenerate only [start,end] and keep the rest; continue = extend the clip to duration_seconds, regenerating from just before the end of its audible content (see overlap_seconds)")],
    prompt: Annotated[str, Field(description="What the result (or the regenerated region) should sound like. May be empty for continue.")] = "",
    audio_type: Annotated[AudioType, Field(description="Same prefixes as generate_audio.")] = "music",
    strength: Annotated[float, Field(ge=0.05, le=1.0, description="variation only: init noise level. ~0.4 keeps melody/rhythm (timbre transfer), ~0.6 style transfer, 0.8+ loose.")] = 0.6,
    start_seconds: Annotated[float | None, Field(ge=0, description="inpaint only: region start.")] = None,
    end_seconds: Annotated[float | None, Field(ge=0, description="inpaint only: region end.")] = None,
    duration_seconds: Annotated[float | None, Field(ge=0.5, le=380, validation_alias=_SECONDS_ALIASES, description="Output length. variation/inpaint default = input length; continue = required total length (> input length).")] = None,
    overlap_seconds: Annotated[float, Field(ge=0, le=10, description="continue only: start regenerating this many seconds before the end of the audible content, so a trailing fade or silence is replaced and the join is smooth.")] = 1.0,
    variations: Annotated[int, Field(ge=1, le=4, description="Number of candidates (different seeds).")] = 1,
    bpm: Annotated[int | None, Field(ge=30, le=300)] = None,
    seed: _Seed = None,
    negative_prompt: Annotated[str | None, Field(description="Only used when cfg != 1.")] = None,
    cfg: Annotated[float, Field(ge=0, le=15)] = 1.0,
    steps: Annotated[int, Field(ge=1, le=50)] = 8,
    title: _Title = None,
    original_idea: _Idea = "",
    peak_ceiling_db: _Ceiling = -1.0,
) -> dict:
    """Edit existing audio with Stable Audio 3: variation (audio-to-audio), inpainting a time range, or continuation/extension. Returns one WAV path per candidate.

    Official tip: surrounding context outweighs the prompt — inpaint a large region first and shrink it, and keep prompts plausible for the context."""
    src = _input_path(input_audio)
    src_len = _audio_seconds(src)
    text = ""
    if prompt.strip():
        text, _ = prepare_prompt(prompt, audio_type, None, bpm)

    req = GenerationRequest(prompt=text, negative_prompt=negative_prompt or "",
                            seconds=0.0, steps=steps, cfg=cfg)
    if mode == "variation":
        seconds = duration_seconds or src_len
        req.init_audio, req.init_strength = str(src), strength
        extra = {"strength": strength}
    elif mode == "inpaint":
        if start_seconds is None or end_seconds is None or end_seconds <= start_seconds:
            raise ToolError("inpaint needs start_seconds < end_seconds")
        seconds = duration_seconds or max(src_len, end_seconds)
        req.inpaint_audio, req.inpaint_range = str(src), (start_seconds, end_seconds)
        extra = {"region": [start_seconds, end_seconds]}
    else:  # continue
        if not duration_seconds or duration_seconds <= src_len:
            raise ToolError(f"continue needs duration_seconds > input length ({src_len:.1f}s)")
        seconds = duration_seconds
        audio, sr = post.read(src)
        start = max(0.0, post.audible_end(audio, sr) - overlap_seconds)
        req.inpaint_audio, req.inpaint_range = str(src), (start, seconds)
        extra = {"region": [round(start, 2), seconds]}

    backend = get_backend()
    req.seconds = seconds = float(min(seconds, backend.max_seconds))
    if req.inpaint_range is not None:
        start, end = req.inpaint_range
        if start >= seconds:
            raise ToolError(f"nothing to regenerate: the region starts at {start:g}s but the output is "
                            f"{seconds:g}s long (model max {backend.max_seconds:g}s)")
        req.inpaint_range = (start, min(end, seconds))
        extra = {"region": [round(req.inpaint_range[0], 2), round(req.inpaint_range[1], 2)]}
    seeds = _seeds(seed, variations)
    res = await backend.generate(seeds, req)

    meta = {"kind": f"edit:{mode}", "title": title, "original_idea": original_idea,
            "audio_type": audio_type, "prompt": prompt, "prompt_used": text,
            "bpm": bpm, "seconds": seconds, "negative_prompt": negative_prompt,
            "cfg": cfg, "steps": steps, "variations": variations,
            "input_audio": str(src), "source": _load_sidecar(src), **extra}
    stem = _slug(title, 80) if title else f"{src.stem}_{mode}"
    candidates = []
    for i, (sd, audio) in enumerate(zip(seeds, res.audios)):
        path = _out_path(stem if variations == 1 else f"{stem}_v{i + 1}")
        candidates.append(_save_generated(backend, audio, path, peak_ceiling_db,
                                          {**meta, "seed": sd, "candidate": i + 1}))
    return {**_summary(backend, text, seconds, variations, res.wall_seconds,
                       res.included_model_load),
            "mode": mode, **extra, "candidates": candidates}


@_tool
async def process_audio(
    input_audio: Annotated[str, Field(description="Absolute path to an audio file (any format soundfile/ffmpeg reads).")],
    start_seconds: Annotated[float, Field(ge=0, description="Trim: keep from here.")] = 0.0,
    end_seconds: Annotated[float | None, Field(ge=0, description="Trim: keep until here (default: end).")] = None,
    trim_silence: Annotated[bool, Field(description="Cut leading/trailing silence (useful for one-shot SFX).")] = False,
    silence_threshold_db: Annotated[float, Field(ge=-90, le=-10)] = -50.0,
    mono: Annotated[bool, Field(description="Downmix to mono (common for positional game SFX).")] = False,
    normalize: Annotated[Literal["none", "peak", "loudness"], Field(description="none = only the safety ceiling; peak = scale so the peak hits peak_ceiling_db; loudness = EBU R128 to target_lufs, two-pass, linear gain when the true-peak limit allows it, otherwise ffmpeg's dynamic mode (reported as loudnorm_mode); the peak ceiling is applied afterwards.")] = "none",
    target_lufs: Annotated[float, Field(ge=-40, le=-5, description="loudness mode: -14 streaming/video, -16 game BGM, -23 broadcast. SFX are usually peak-normalized instead.")] = -16.0,
    peak_ceiling_db: Annotated[float, Field(ge=-24, le=0, description="Peak ceiling / target in dBFS.")] = -1.0,
    loop: Annotated[bool, Field(description="Make a seamless loop by crossfading the tail into the head (output gets shorter by loop_crossfade_ms).")] = False,
    loop_crossfade_ms: Annotated[float, Field(ge=1, le=5000, description="Loop crossfade: ~50–200 ms for rhythmic BGM cut on a bar, 500–2000 ms for ambience.")] = 200.0,
    fade_in_ms: Annotated[float, Field(ge=0, le=10000)] = 0.0,
    fade_out_ms: Annotated[float, Field(ge=0, le=10000)] = 0.0,
    format: Annotated[Literal["wav", "ogg", "flac"], Field(description="wav = 24-bit PCM; ogg = Ogg Vorbis (Unity/Godot); flac = 24-bit lossless.")] = "wav",
    sample_rate: Annotated[Literal[44100, 48000], Field(description="48000 for video; 44100 keeps the model's native rate (no resampling).")] = 48000,
    ogg_quality: Annotated[float, Field(ge=0.1, le=1.0, description="Vorbis quality 0.1–1.0 (0.6 ≈ 190 kbps stereo).")] = 0.6,
    title: Annotated[str | None, Field(description="Output file name stem. Default: <input>_proc.")] = None,
) -> dict:
    """Post-process and export a clip: trim, trim silence, mono, normalize (peak or LUFS), seamless loop, fades, then write 48 kHz/44.1 kHz WAV, OGG Vorbis or FLAC (+ .json sidecar). The input file is not modified. Steps run in that order. Does not load any model."""
    src = _input_path(input_audio)

    def job():
        audio, sr = post.read(src)
        steps_done, info = [], {}
        if start_seconds > 0 or end_seconds is not None:
            audio = post.trim(audio, sr, start_seconds, end_seconds)
            steps_done.append(f"trim {start_seconds}–{end_seconds}")
        if trim_silence:
            audio = post.trim_silence(audio, sr, silence_threshold_db)
            steps_done.append(f"trim_silence {silence_threshold_db} dB")
        if mono and audio.shape[0] > 1:
            audio = post.to_mono(audio)
            steps_done.append("mono")
        lufs = target_lufs if normalize == "loudness" else None
        if sr != sample_rate or lufs is not None:
            audio, info = post.ffmpeg_process(audio, sr, sample_rate, lufs, peak_ceiling_db)
            if sr != sample_rate:
                steps_done.append(f"resample {sr}→{sample_rate}")
            if lufs is not None:
                steps_done.append(f"loudnorm {lufs} LUFS")
            sr = sample_rate
        if loop:
            audio = post.make_loop(audio, sr, loop_crossfade_ms)
            steps_done.append(f"loop xfade {loop_crossfade_ms} ms")
        if fade_in_ms or fade_out_ms:
            audio = post.fade(audio, sr, fade_in_ms, fade_out_ms)
            steps_done.append(f"fade {fade_in_ms}/{fade_out_ms} ms")
        if normalize == "peak":
            audio, gain = post.peak_normalize(audio, peak_ceiling_db)
            steps_done.append(f"peak normalize {peak_ceiling_db} dBFS ({gain:+.2f} dB)")
        else:
            audio, gain = post.peak_ceiling(audio, peak_ceiling_db)
            if gain:
                steps_done.append(f"peak ceiling {peak_ceiling_db} dBFS ({gain:+.2f} dB)")

        ext = {"wav": ".wav", "ogg": ".ogg", "flac": ".flac"}[format]
        path = _out_path(_slug(title, 80) if title else f"{src.stem}_proc", ext)
        post.write(path, audio, sr, format, ogg_quality)
        st = post.stats(audio)
        result = {"path": str(path), "format": format, "sample_rate": sr,
                  "channels": int(audio.shape[0]),
                  "seconds": round(audio.shape[-1] / sr, 3),
                  "steps": steps_done, **st, **info}
        meta = {"kind": "process", "input_audio": str(src), "source": _load_sidecar(src),
                "created": datetime.now().isoformat(timespec="seconds"),
                **{k: v for k, v in result.items() if k != "path"}, "file": path.name}
        result["metadata"] = str(post.write_sidecar(path, meta))
        return result

    return await asyncio.to_thread(job)


@_tool
async def unload_models() -> dict:
    """Free the ~5–7 GB of memory held by the loaded models now (they reload on the next call)."""
    if _backend is None:
        return {"unloaded": False}
    return {"unloaded": await _backend.unload()}


def _port(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, got {port}")
    return port


def main(argv: list[str] | None = None) -> None:
    global _preload_when_ready
    ap = argparse.ArgumentParser(prog="audio-studio-mcp", description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--http", type=int, metavar="PORT",
                      help="serve streamable HTTP on 127.0.0.1:PORT instead of stdio (local only)")
    mode.add_argument("--web", type=_port, metavar="PORT",
                      help="serve the web UI at http://127.0.0.1:PORT/ and MCP at /mcp on the same "
                           "port, sharing one loaded model (local only)")
    ap.add_argument("--no-browser", action="store_true", help="with --web: do not open a browser")
    ap.add_argument("--preload", action="store_true", help="load models at startup")
    args = ap.parse_args(argv)

    try:
        backend = get_backend()
    except RuntimeError as e:
        print(f"audio-studio-mcp: {e}", file=sys.stderr)
        sys.exit(1)
    backend.start_idle_reaper(IDLE_UNLOAD_MIN)
    if args.preload:
        if args.http or args.web is not None:
            backend.preload()
        else:
            _preload_when_ready = True

    if args.web is not None:
        from . import web
        web.serve(args.web, open_browser=not args.no_browser)
    elif args.http:
        server.run("streamable-http", host="127.0.0.1", port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
