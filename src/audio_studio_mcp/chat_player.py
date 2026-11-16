"""In-chat player (MCP Apps, `io.modelcontextprotocol/ui`).

Clients that support MCP Apps (Claude Desktop, VS Code, Cursor, Goose, …)
render the results of generate_audio / edit_audio / process_audio as a player
inside the chat: waveform, play / seek, A/B between candidates, loop, and
buttons to pick, export or make more takes. The UI is one self-contained HTML
document (static/player.html with static/wave.js inlined), served as the
`ui://` resource the three tools point at.

How the audio gets into the sandboxed iframe: the host's default CSP allows
no network access and only `data:` media, and a 60 s 24-bit WAV is ~15 MB.
So the player calls the app-only tool `audio_preview`, which encodes a
compressed preview (Ogg Vorbis, ~1 MB a minute; MP3 as a fallback for
browsers without Vorbis) once per file and returns it in base64 chunks, plus
waveform peaks computed on the server (the web UI's /api/peaks summary) and
the sidecar's metadata. The player decodes it with Web Audio, which also gives
sample-accurate loops for listening to a seam.

Clients without MCP Apps (Claude Code, Codex, …) see exactly what they saw
before: `AppsGate` hides the app-only tool, the `ui://` resource and the
`_meta.ui` tool fields from them, and every tool result is unchanged text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID, Apps, client_supports_apps
from mcp_types import CallToolResult, ListResourcesResult, ListToolsResult, TextContent

STATIC_DIR = Path(__file__).with_name("static")
RESOURCE_URI = "ui://audio-studio/player.html"
# _meta for the tools whose results the player shows
UI_META = {"ui": {"resourceUri": RESOURCE_URI}}
# _meta for tools only the player may call (hidden from the model)
APP_ONLY_META = {"ui": {"visibility": ["app"]}}
APP_ONLY_TOOLS = {"audio_preview"}

PREVIEW_FORMATS = {"ogg": "audio/ogg", "mp3": "audio/mpeg"}
PREVIEW_OGG_QUALITY = 0.45   # Vorbis q≈4.5: ~130 kbps stereo, transparent enough to judge a take
CHUNK_BYTES = 512 * 1024     # raw bytes per audio_preview call (~700 KB of base64)
PEAK_COLUMNS = 720           # waveform columns; the player scales them to its width
_CACHE_BYTES = 256 << 20     # encoded previews kept in memory (~2 h of audio; a long chat's worth)

# Export buttons: the same presets as the web UI's editor (static/editor.js).
EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "game_sfx": {"trim_silence": True, "mono": True, "normalize": "peak", "peak_ceiling_db": -1.0,
                 "format": "ogg", "sample_rate": 48000},
    "game_loop": {"loop": True, "loop_crossfade_ms": 200.0, "normalize": "loudness", "target_lufs": -16.0,
                  "peak_ceiling_db": -1.0, "format": "ogg", "sample_rate": 48000},
    "video_bgm": {"normalize": "loudness", "target_lufs": -14.0, "peak_ceiling_db": -1.0,
                  "fade_in_ms": 500.0, "fade_out_ms": 2000.0, "format": "wav", "sample_rate": 48000},
    "original": {"normalize": "none", "peak_ceiling_db": -1.0, "format": "wav", "sample_rate": 44100},
}

# Sidecar fields the player shows or needs to repeat a call ("more takes").
_META_KEYS = ("kind", "title", "audio_type", "prompt", "prompt_used", "sound_fields", "bpm", "seconds",
              "negative_prompt", "cfg", "steps", "seed", "candidate", "variations", "strength", "region",
              "mode", "steps_done", "format", "sample_rate", "channels", "integrated_lufs", "peak_dbfs",
              "input_audio")


# ── the UI resource ──────────────────────────────────────────────────────────
def player_html() -> str:
    """static/player.html with the shared waveform code and the export presets inlined
    (the iframe cannot load anything else)."""
    html = (STATIC_DIR / "player.html").read_text(encoding="utf-8")
    wave = (STATIC_DIR / "wave.js").read_text(encoding="utf-8")
    presets = json.dumps(EXPORT_PRESETS)
    for name, value in (("/*@WAVE_JS@*/", wave), ("/*@PRESETS@*/null", presets)):
        if name not in html:
            raise RuntimeError(f"player.html lost its {name} placeholder")
        html = html.replace(name, value)
    return html


def create_apps() -> Apps:
    apps = Apps()
    apps.add_html_resource(
        RESOURCE_URI, player_html(), name="audio-studio-player", title="Audio Studio player",
        description="Plays generated audio in the chat: waveform, A/B between candidates, loop, export.",
        prefers_border=True)
    return apps


# ── audio_preview ────────────────────────────────────────────────────────────
# (path, mtime, size, format) -> (encoded bytes, etag), most recent last. Re-encoding
# Ogg gives different bytes (random stream serial), so every chunk carries the
# etag and the player starts over if an entry was evicted mid-download.
_previews: dict[tuple, tuple[bytes, str]] = {}
_cache_lock = threading.Lock()
_encode_locks: dict[tuple, threading.Lock] = {}  # one encode per file at a time, others run in parallel


def resolve(path: str) -> Path:
    """A result file the player may read: an audio file directly in the output
    folder (the same rule as the web UI), never anything else on the disk."""
    from . import web
    found = web.resolve_input(path)
    if found is None:
        raise ValueError("audio_preview only reads audio files in the Audio Studio output folder")
    return found


def _encode(path: Path, fmt: str) -> bytes:
    import soundfile as sf

    from . import post
    audio, sr = post.read(path)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"preview.{fmt}"
        if fmt == "ogg":
            post.write(out, audio, sr, "ogg", PREVIEW_OGG_QUALITY)
        else:  # MP3 (libsndfile ≥ 1.1): browsers without Vorbis (Safari's WebKit)
            if sr not in (32000, 44100, 48000):
                raise ValueError(f"MP3 preview needs 32/44.1/48 kHz audio, not {sr} Hz")
            data = audio.clip(-1, 1).T
            # in blocks, like post.write's OGG (libsndfile encoders and the 1 MB Windows thread stack)
            with sf.SoundFile(str(out), "w", sr, data.shape[1], format="MP3", subtype="MPEG_LAYER_III",
                              compression_level=0.3) as f:
                for i in range(0, len(data), post.OGG_WRITE_BLOCK):
                    f.write(data[i:i + post.OGG_WRITE_BLOCK])
        return out.read_bytes()


def encoded(path: Path, fmt: str) -> tuple[bytes, str]:
    """The file's preview and its etag, encoded once and cached."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, fmt)
    with _cache_lock:
        hit = _previews.pop(key, None)
        if hit is not None:
            _previews[key] = hit  # most recent last
            return hit
        lock = _encode_locks.setdefault(key, threading.Lock())
    with lock:  # parallel calls for the same file wait for one encode
        with _cache_lock:
            hit = _previews.get(key)
        if hit is None:
            data = _encode(path, fmt)
            hit = (data, hashlib.sha1(data).hexdigest()[:16])
        with _cache_lock:
            _previews.pop(key, None)
            _previews[key] = hit
            _encode_locks.pop(key, None)
            while len(_previews) > 1 and sum(len(d) for d, _ in _previews.values()) > _CACHE_BYTES:
                del _previews[next(iter(_previews))]
    return hit


def _sidecar(src: Path) -> dict:
    """The file's .json sidecar, if it is a regular file in the same folder (a
    symlink out of the output folder is ignored, like for the audio)."""
    from . import web
    side = src.with_name(src.name + ".json")
    try:
        real = side.resolve(strict=True)
    except (OSError, RuntimeError):
        return {}
    if real.parent != src.parent or not real.is_file():
        return {}
    return web._read_meta(real) or {}


def preview(path: str, offset: int, fmt: str) -> dict:
    """One chunk of a file's preview; the first one (offset 0) also carries the
    waveform, the length and the sidecar metadata."""
    from . import web
    src = resolve(path)
    data, etag = encoded(src, fmt)
    if not 0 <= offset <= len(data):
        raise ValueError(f"offset {offset} is outside the preview (size {len(data)})")
    chunk = data[offset:offset + CHUNK_BYTES]
    out: dict[str, Any] = {"file": src.name, "path": str(src), "mime": PREVIEW_FORMATS[fmt],
                           "size": len(data), "etag": etag, "offset": offset, "next": offset + len(chunk),
                           "done": offset + len(chunk) >= len(data),
                           "data": base64.b64encode(chunk).decode("ascii")}
    if offset == 0:
        pk = web.peaks(src, PEAK_COLUMNS)
        out.update(seconds=pk["seconds"], sample_rate=pk["sample_rate"], channels=pk["channels"],
                   peaks={"min": pk["min"], "max": pk["max"]})
        meta = _sidecar(src)
        if isinstance(meta.get("steps"), list):  # process_audio: the steps it ran, not sampling steps
            meta = dict(meta)
            meta["steps_done"] = meta.pop("steps")
        out["meta"] = {k: meta[k] for k in _META_KEYS if meta.get(k) not in (None, "", {})}
    return out


# ── clients without MCP Apps see the server as before ─────────────────────────
_announced: set[int] = set()


def mode() -> str:
    """AUDIO_STUDIO_CHAT_PLAYER: auto (default) = the player for clients that
    advertise MCP Apps at initialize; on = for every client (hosts that render
    apps without advertising them, e.g. the ext-apps reference host; clients
    without MCP Apps would then list the player's helper tool); off = never."""
    value = os.environ.get("AUDIO_STUDIO_CHAT_PLAYER", "auto").strip().lower()
    return value if value in ("auto", "on", "off") else "auto"


def _supports(ctx: Any) -> bool:
    m = mode()
    if m != "auto":
        return m == "on"
    try:
        return client_supports_apps(ctx)
    except Exception:  # no session / capabilities yet
        return False


class AppsGate:
    """Server middleware: for clients that did not negotiate MCP Apps, hide the
    app-only tools, the `ui://` resource and the `_meta.ui` tool fields, so their
    tool list (what the model reads) stays exactly what it was before the player."""

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        method = ctx.method
        if method == "tools/list":  # once per client session: say whether the player can show
            session = getattr(ctx, "session", None)
            if session is not None and id(session) not in _announced:
                _announced.add(id(session))
                print(f"[audio-studio] client supports the in-chat player (MCP Apps): "
                      f"{'yes' if _supports(ctx) else 'no, text results only'}", file=sys.stderr)
        if method not in ("tools/list", "tools/call", "resources/list") or _supports(ctx):
            return await call_next(ctx)
        if method == "tools/call":
            name = (ctx.params or {}).get("name")
            if name in APP_ONLY_TOOLS:
                return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                                      is_error=True)
            return await call_next(ctx)
        result = await call_next(ctx)
        if isinstance(result, ListToolsResult | ListResourcesResult):
            result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not isinstance(result, dict):
            return result
        if method == "tools/list":
            tools = []
            for tool in result.get("tools", []):
                if tool.get("name") in APP_ONLY_TOOLS:
                    continue
                tool = dict(tool)
                meta = {k: v for k, v in (tool.pop("_meta", None) or {}).items() if k != "ui"}
                if meta:
                    tool["_meta"] = meta
                tools.append(tool)
            result = {**result, "tools": tools}
        else:
            result = {**result, "resources": [r for r in result.get("resources", [])
                                              if not str(r.get("uri", "")).startswith("ui://")]}
        return result


__all__ = ["APP_MIME_TYPE", "EXTENSION_ID", "RESOURCE_URI", "UI_META", "APP_ONLY_META", "AppsGate",
           "create_apps", "preview", "player_html"]
