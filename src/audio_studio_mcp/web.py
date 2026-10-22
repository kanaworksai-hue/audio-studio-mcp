"""Local web UI: `audio-studio-mcp --web PORT`.

One process serves, on 127.0.0.1:PORT,
  /mcp     the MCP server (streamable HTTP), for agents
  /        the web UI (static/, plain HTML/JS/CSS, no build step)
  /api/*   a small JSON API the page uses
so the page and MCP clients share one loaded model.

The API runs tools in-process through `server.server.call_tool()`, so argument
validation and error messages are exactly what an MCP client gets. Generation
requests become jobs in a FIFO queue that one worker runs in order; the page
polls a job for its queue position, elapsed time, result or error.

Local only: the server binds 127.0.0.1 and /api rejects requests whose Host is
not this loopback address (DNS rebinding) and writes from other origins (CSRF).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, post, server
from .prompts import DEFAULT_SECONDS

STATIC_DIR = Path(__file__).with_name("static")
AUDIO_EXTS = {".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac", ".mp3": "audio/mpeg"}
# Tools the page may queue. edit/process read `input_audio`; from the page that
# must be an audio file directly inside OUTPUT_DIR (see resolve_input).
JOB_TOOLS = {"generate_audio", "edit_audio", "process_audio"}
INPUT_TOOLS = {"edit_audio", "process_audio"}
MAX_FINISHED_JOBS = 200
MAX_PEAKS = 20000


# ── jobs ─────────────────────────────────────────────────────────────────────
@dataclass
class Job:
    id: str
    tool: str
    arguments: dict
    seq: int
    status: str = "queued"  # queued | running | done | error | cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: Any = None
    error: str | None = None

    def view(self, position: int | None) -> dict:
        end = self.finished or time.time()
        return {
            "id": self.id, "tool": self.tool, "arguments": self.arguments,
            "status": self.status, "position": position,
            "created": self.created, "started": self.started, "finished": self.finished,
            "elapsed": round(end - self.started, 1) if self.started else 0.0,
            "waited": round((self.started or end) - self.created, 1),
            "result": self.result, "error": self.error,
        }


def _tool_result(r) -> Any:
    """CallToolResult → the tool's return value (raises ToolError on an error result)."""
    text = "".join(getattr(c, "text", "") for c in r.content)
    if r.is_error:
        raise ToolError(text)
    if r.structured_content is not None:
        return r.structured_content
    try:
        return json.loads(text)
    except ValueError:
        return text


class JobQueue:
    """FIFO of tool calls run one at a time on the server's event loop.

    The backend serializes model calls anyway; running web jobs one by one on
    top of that keeps the queue position the page shows meaningful."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._seq = itertools.count(1)
        self._queue: asyncio.Queue[Job] | None = None
        self._worker: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def submit(self, tool: str, arguments: dict) -> Job:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # first call, or the app was restarted on a new loop
            self._loop, self._queue = loop, asyncio.Queue()
            for j in self.jobs.values():
                if j.status in ("queued", "running"):  # their worker died with the old loop
                    j.status, j.error, j.finished = "error", "interrupted (server restarted)", time.time()
            self._worker = None
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run())
        job = Job(id=secrets.token_hex(6), tool=tool, arguments=arguments, seq=next(self._seq))
        self.jobs[job.id] = job
        self._queue.put_nowait(job)
        self._prune()
        return job

    def positions(self) -> dict[str, int]:
        """1-based place of each queued job (started jobs have none)."""
        queued = sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: j.seq)
        return {j.id: i for i, j in enumerate(queued, 1)}

    def view(self, job: Job, positions: dict[str, int] | None = None) -> dict:
        return job.view((positions if positions is not None else self.positions()).get(job.id))

    def cancel(self, job: Job) -> bool:
        """Drop a job that has not started yet (a running model call cannot stop)."""
        if job.status != "queued":
            return False
        job.status, job.finished = "cancelled", time.time()
        return True

    def counts(self) -> dict:
        st = [j.status for j in self.jobs.values()]
        return {"queued": st.count("queued"), "running": st.count("running")}

    def _prune(self) -> None:
        done = [j for j in self.jobs.values() if j.status in ("done", "error", "cancelled")]
        for j in sorted(done, key=lambda j: j.seq)[:max(0, len(done) - MAX_FINISHED_JOBS)]:
            del self.jobs[j.id]

    async def _run(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            job = await queue.get()
            if job.status == "cancelled":
                queue.task_done()
                continue
            job.status, job.started = "running", time.time()
            try:
                job.result = _tool_result(await server.server.call_tool(job.tool, job.arguments))
                job.status = "done"
            except Exception as e:  # ToolError carries the same text MCP clients see
                job.error, job.status = str(e), "error"
            except asyncio.CancelledError:  # server shutting down
                job.error, job.status = "cancelled", "error"
                raise
            finally:
                job.finished = time.time()
                queue.task_done()


jobs = JobQueue()


def validate_arguments(tool: str, arguments: dict) -> list[dict]:
    """Check arguments against the tool's schema before queueing, so a typo is
    reported now instead of after the jobs ahead of it. Returns field errors."""
    t = server.server._tool_manager.get_tool(tool)
    try:
        t.fn_metadata.validate_arguments(arguments)
    except ValidationError as e:
        return [{"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
                for err in e.errors()]
    except AttributeError:  # SDK internals moved; the tool call validates anyway
        pass
    return []


# ── library ──────────────────────────────────────────────────────────────────
def _output_dir() -> Path:
    return server.OUTPUT_DIR  # read at call time (tests and env may change it)


def _safe_audio_path(name: str) -> Path | None:
    """An audio file directly inside OUTPUT_DIR, or None (no traversal, no symlink escape)."""
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\0" in name:
        return None
    root = _output_dir().resolve()
    path = (root / name)
    if path.suffix.lower() not in AUDIO_EXTS:
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # a symlink must stay in the folder and point at audio as well
    if resolved.parent != root or resolved.suffix.lower() not in AUDIO_EXTS or not resolved.is_file():
        return None
    return resolved


def resolve_input(value: Any) -> Path | None:
    """`input_audio` from the page: a file name in OUTPUT_DIR, or an absolute
    path whose folder is OUTPUT_DIR (what the library and results show).
    Anything else (other folders, traversal, symlinks out) is None."""
    if not isinstance(value, str) or not value:
        return None
    name = value
    if "/" in value or "\\" in value:
        p = Path(value)
        if not p.is_absolute():
            return None
        try:
            if p.parent.resolve(strict=True) != _output_dir().resolve():
                return None
        except (OSError, RuntimeError):
            return None
        name = p.name
    path = _safe_audio_path(name)
    try:
        return path if path is not None and path.stat().st_size > 0 else None
    except OSError:
        return None


def _read_meta(side: Path) -> dict | None:
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


# file name → (stat key, entry); a library request re-reads only changed files
_entry_cache: dict[str, tuple[tuple, dict]] = {}


def _entry(path: Path, st: os.stat_result) -> dict:
    side = path.with_suffix(path.suffix + ".json")
    try:
        side_mtime = side.stat().st_mtime_ns
    except OSError:
        side_mtime = None
    key = (str(path), st.st_mtime_ns, st.st_size, side_mtime)
    hit = _entry_cache.get(path.name)
    if hit and hit[0] == key:
        return hit[1]
    meta = _read_meta(side) if side_mtime is not None else None
    seconds = (meta or {}).get("seconds")
    if not isinstance(seconds, (int, float)):
        try:
            import soundfile as sf
            seconds = round(float(sf.info(str(path)).duration), 3)
        except Exception:
            seconds = None
    entry = {"file": path.name, "path": str(path), "size": st.st_size, "mtime": st.st_mtime,
             "seconds": seconds, "format": path.suffix.lower().lstrip("."), "meta": meta}
    _entry_cache[path.name] = (key, entry)
    return entry


def _search_text(e: dict) -> str:
    m = e["meta"] or {}
    sf = m.get("sound_fields")
    parts = [e["file"], m.get("title"), m.get("prompt"), m.get("prompt_used"),
             m.get("original_idea"), m.get("audio_type"), m.get("kind"), m.get("seed"),
             *(sf.values() if isinstance(sf, dict) else [])]
    return " ".join(str(p) for p in parts if p is not None and p != "").lower()


LIBRARY_KINDS = ("music", "instrument", "sfx", "edit", "process")


def _kind_matches(e: dict, kind: str) -> bool:
    m = e["meta"] or {}
    k = str(m.get("kind") or "")
    if kind == "edit":
        return k.startswith("edit")
    if kind == "process":
        return k == "process"
    return m.get("audio_type") == kind and k != "process"


def list_library(query: str = "", offset: int = 0, limit: int = 100, kind: str = "") -> dict:
    root = _output_dir()
    files: list[tuple[Path, os.stat_result]] = []
    try:
        names = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        names = []
    for p in names:
        if p.suffix.lower() not in AUDIO_EXTS or p.name.startswith("."):
            continue
        try:
            if p.is_symlink() and _safe_audio_path(p.name) is None:
                continue  # points outside the folder or at something that is not audio
            st = p.stat()
        except OSError:  # deleted while we scan
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size > 0:  # empty = name reserved by a running generation
            files.append((p, st))
    files.sort(key=lambda f: (f[1].st_mtime, f[0].name), reverse=True)
    if len(_entry_cache) > 2 * len(files) + 100:
        _entry_cache.clear()
    words = query.lower().split()
    if not words and not kind:  # build entries for the requested page only
        return {"output_dir": str(root), "total": len(files),
                "items": [_entry(p, st) for p, st in files[offset:offset + limit]]}
    entries = [e for e in (_entry(p, st) for p, st in files)
               if all(w in _search_text(e) for w in words) and (not kind or _kind_matches(e, kind))]
    return {"output_dir": str(root), "total": len(entries), "items": entries[offset:offset + limit]}


# ── waveform peaks ───────────────────────────────────────────────────────────
# Read on the server so every format the tools read draws in every browser
# (Safari cannot decode OGG Vorbis). Each file is summarized once, streaming,
# into min/max per PEAK_BLOCK frames (380 s stereo → ~0.5 MB instead of a
# 127 MB decode); windows finer than that read just their frames from the file.
PEAK_BLOCK = 256
_summaries: dict[tuple, tuple] = {}   # (path, mtime, size) -> (sr, channels, frames, mins, maxs)
_summary_lock = threading.Lock()      # one summary at a time; also dedupes parallel requests
_SUMMARY_KEEP = 64


def _summary(path: Path) -> tuple:
    import soundfile as sf
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _summary_lock:
        hit = _summaries.pop(key, None)
        if hit is None:
            mins, maxs = [], []
            with sf.SoundFile(str(path)) as f:
                sr, channels = f.samplerate, f.channels
                for block in f.blocks(blocksize=PEAK_BLOCK * 4096, dtype="float32", always_2d=True):
                    lo, hi = block.min(axis=1), block.max(axis=1)
                    idx = np.arange(0, len(lo), PEAK_BLOCK)
                    mins.append(np.minimum.reduceat(lo, idx))
                    maxs.append(np.maximum.reduceat(hi, idx))
                frames = f.frames if f.frames > 0 else sum(len(m) for m in mins) * PEAK_BLOCK
            empty = np.zeros(0, dtype=np.float32)
            hit = (sr, channels, frames, np.concatenate(mins) if mins else empty,
                   np.concatenate(maxs) if maxs else empty)
        _summaries[key] = hit  # most recent last
        while len(_summaries) > _SUMMARY_KEEP:
            del _summaries[next(iter(_summaries))]
    return hit


def _buckets(lo: np.ndarray, hi: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce per-frame (or per-block) min/max to n buckets. A bucket narrower
    than one input value (zoomed past sample level) repeats the value it starts on."""
    idx = (np.arange(n) * len(lo)) // n
    return np.minimum.reduceat(lo, idx), np.maximum.reduceat(hi, idx)


def peaks(path: Path, n: int, start: float | None = None, end: float | None = None) -> dict:
    """Min/max of all channels in n equal buckets over [start, end] seconds."""
    sr, channels, total, bmin, bmax = _summary(path)
    s = 0 if start is None else min(total, max(0, int(start * sr)))
    e = total if end is None else min(total, max(s, int(end * sr)))
    lo = hi = np.zeros(0, dtype=np.float32)
    if e > s and n:
        if (e - s) >= n * PEAK_BLOCK:  # coarse enough for the summary
            b0, b1 = s // PEAK_BLOCK, min(len(bmin), -(-e // PEAK_BLOCK))
            lo, hi = _buckets(bmin[b0:b1], bmax[b0:b1], n)
        else:
            import soundfile as sf
            seg = sf.read(str(path), start=s, stop=e, dtype="float32", always_2d=True)[0]
            if len(seg):
                lo, hi = _buckets(seg.min(axis=1), seg.max(axis=1), n)
    return {"seconds": round(total / sr, 4), "sample_rate": sr, "channels": channels,
            "start": round(s / sr, 4), "end": round(e / sr, 4),
            "min": [round(float(v), 4) for v in lo], "max": [round(float(v), 4) for v in hi]}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class LocalOnly:
    """ASGI middleware. /api: loopback Host only (DNS rebinding), no requests
    from other sites (Sec-Fetch-Site), and writes need a same-origin Origin and
    a JSON body (cross-site form posts). Every response: no framing (clickjacking)."""

    LOOPBACK = ("127.0.0.1", "localhost", "::1")
    FRAME_HEADERS = [(b"x-frame-options", b"DENY"),
                     (b"content-security-policy", b"frame-ancestors 'none'")]

    def __init__(self, app, port: int | None) -> None:
        self.app, self.port = app, port

    def _host_ok(self, host: str) -> bool:
        try:
            u = urlsplit("//" + host)
            port = u.port
        except ValueError:
            return False
        if u.hostname not in self.LOOPBACK or u.username or u.path:
            return False
        return self.port is None or port == self.port

    def _problem(self, scope) -> str | None:
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if not self._host_ok(headers.get("host", "")):
            return "host not allowed"
        if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return "cross-site request refused"
        if scope["method"] not in ("GET", "HEAD"):
            origin = headers.get("origin")
            if origin is None or not self._host_ok(origin.split("://", 1)[-1]):
                return "cross-origin request refused"
            if not headers.get("content-type", "").startswith("application/json"):
                return "expected application/json"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self.FRAME_HEADERS]}
            await send(message)

        if scope["path"].startswith("/api/"):
            problem = self._problem(scope)
            if problem:
                return await _err(403, problem)(scope, receive, send_with_headers)
        return await self.app(scope, receive, send_with_headers)


async def api_status(_: Request) -> Response:
    b = server.get_backend()
    return JSONResponse({
        "version": __version__, "backend": b.name, "model": b.model_label,
        "loaded": bool(b.loaded()), "max_seconds": b.max_seconds, "sample_rate": b.sample_rate,
        "output_dir": str(_output_dir()), "default_seconds": DEFAULT_SECONDS,
        "jobs": jobs.counts(),
    })


async def api_unload(_: Request) -> Response:
    try:
        r = _tool_result(await server.server.call_tool("unload_models", {}))
    except ToolError as e:
        return _err(500, str(e))
    return JSONResponse(r)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise ValueError("invalid JSON body")
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


async def api_jobs_create(request: Request) -> Response:
    try:
        body = await _json_body(request)
    except ValueError as e:
        return _err(400, str(e))
    tool, arguments = body.get("tool", "generate_audio"), body.get("arguments", {})
    if tool not in JOB_TOOLS:
        return _err(400, f"tool not available from the web UI: {tool!r}")
    if not isinstance(arguments, dict):
        return _err(400, "arguments must be an object")
    if tool in INPUT_TOOLS:
        src = resolve_input(arguments.get("input_audio"))
        if src is None:
            return _err(422, "input_audio must be an audio file in the output folder",
                        fields=[{"field": "input_audio", "message": "not a file in the output folder"}])
        arguments = {**arguments, "input_audio": str(src)}
    errors = validate_arguments(tool, arguments)
    if errors:
        msg = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
        return _err(422, f"invalid arguments: {msg}", fields=errors)
    job = jobs.submit(tool, arguments)
    return JSONResponse(jobs.view(job), status_code=202)


async def api_jobs_list(request: Request) -> Response:
    items = sorted(jobs.jobs.values(), key=lambda j: j.seq, reverse=True)
    ids = request.query_params.get("ids")
    if ids is not None:  # the page polls only the jobs it is waiting for
        wanted = set(ids.split(","))
        items = [j for j in items if j.id in wanted]
    pos = jobs.positions()
    # `loaded` lets the page tell "loading the model" from "generating"
    return JSONResponse({"items": [jobs.view(j, pos) for j in items], **jobs.counts(),
                         "loaded": bool(server.get_backend().loaded())})


async def api_job_cancel(request: Request) -> Response:
    job = jobs.jobs.get(request.path_params["job_id"])
    if job is None:
        return _err(404, "no such job")
    if not jobs.cancel(job):
        return _err(409, f"job is {job.status}, only waiting jobs can be cancelled")
    return JSONResponse(jobs.view(job))


async def api_job(request: Request) -> Response:
    job = jobs.jobs.get(request.path_params["job_id"])
    if job is None:
        return _err(404, "no such job")
    return JSONResponse(jobs.view(job))


async def api_library(request: Request) -> Response:
    q = request.query_params
    try:
        offset = max(0, int(q.get("offset", 0)))
        limit = min(500, max(1, int(q.get("limit", 100))))
    except ValueError:
        return _err(400, "offset/limit must be integers")
    kind = q.get("kind", "")
    if kind and kind not in LIBRARY_KINDS:
        return _err(400, f"kind must be one of {', '.join(LIBRARY_KINDS)}")
    return JSONResponse(await asyncio.to_thread(list_library, q.get("q", ""), offset, limit, kind))


async def api_library_item(request: Request) -> Response:
    path = _safe_audio_path(request.path_params["name"])
    if path is None:
        return _err(404, "not found")
    return JSONResponse(await asyncio.to_thread(lambda: _entry(path, path.stat())))


async def api_audio(request: Request) -> Response:
    path = _safe_audio_path(request.path_params["name"])
    if path is None:
        return _err(404, "not found")
    download = request.query_params.get("download") in ("1", "true")
    return FileResponse(path, media_type=AUDIO_EXTS[path.suffix.lower()],
                        filename=path.name if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "no-cache"})


async def api_peaks(request: Request) -> Response:
    path = _safe_audio_path(request.path_params["name"])
    if path is None:
        return _err(404, "not found")
    q = request.query_params
    try:
        n = min(MAX_PEAKS, max(1, int(q.get("n", 2000))))
        start = float(q["start"]) if q.get("start") else None
        end = float(q["end"]) if q.get("end") else None
        if not all(math.isfinite(v) for v in (start, end) if v is not None):
            raise ValueError
    except ValueError:
        return _err(400, "n/start/end must be finite numbers")
    try:
        return JSONResponse(await asyncio.to_thread(peaks, path, n, start, end))
    except Exception as e:  # unreadable or truncated file
        return _err(422, f"cannot read audio: {e}")


async def index(_: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


API_ROUTES = [
    Route("/api/status", api_status, methods=["GET"]),
    Route("/api/unload", api_unload, methods=["POST"]),
    Route("/api/jobs", api_jobs_create, methods=["POST"]),
    Route("/api/jobs", api_jobs_list, methods=["GET"]),
    Route("/api/jobs/{job_id}", api_job, methods=["GET"]),
    Route("/api/jobs/{job_id}/cancel", api_job_cancel, methods=["POST"]),
    Route("/api/library", api_library, methods=["GET"]),
    Route("/api/library/{name:path}", api_library_item, methods=["GET"]),
    Route("/api/audio/{name:path}", api_audio, methods=["GET", "HEAD"]),
    Route("/api/peaks/{name:path}", api_peaks, methods=["GET"]),
]


def create_app(port: int | None = None) -> Starlette:
    """The MCP streamable-HTTP app plus the web UI and API on the same routes table.

    `port` pins the Host / Origin check to 127.0.0.1:port (None = any port, tests)."""
    app = server.server.streamable_http_app(host="127.0.0.1")
    app.router.routes.extend([
        *API_ROUTES,
        Route("/", index, methods=["GET"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ])
    app.add_middleware(LocalOnly, port=port)
    return app


def serve(port: int, open_browser: bool = True) -> None:
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    config = uvicorn.Config(create_app(port), host="127.0.0.1", port=port, log_level="info",
                            access_log=False)  # the page polls; keep the console readable

    class Server(uvicorn.Server):
        # Announce from the startup itself: a watcher thread can be killed
        # before it prints when the server is stopped right away (uvicorn
        # re-raises SIGTERM after shutting down).
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                print(f"[audio-studio] web UI: {url}   MCP: {url}mcp", flush=True)
                if open_browser:
                    threading.Thread(target=webbrowser.open, args=(url,), daemon=True, name="open-browser").start()

    Server(config).run()
