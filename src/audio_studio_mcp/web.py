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
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, server
from .prompts import DEFAULT_SECONDS

STATIC_DIR = Path(__file__).with_name("static")
AUDIO_EXTS = {".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac", ".mp3": "audio/mpeg"}
# Tools the page may queue. edit/process take arbitrary input paths; they come
# with the editing page (W2) and an input check limited to OUTPUT_DIR.
JOB_TOOLS = {"generate_audio"}
MAX_FINISHED_JOBS = 200


# ── jobs ─────────────────────────────────────────────────────────────────────
@dataclass
class Job:
    id: str
    tool: str
    arguments: dict
    seq: int
    status: str = "queued"  # queued | running | done | error
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

    def submit(self, tool: str, arguments: dict) -> Job:
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())
        job = Job(id=secrets.token_hex(6), tool=tool, arguments=arguments, seq=next(self._seq))
        self.jobs[job.id] = job
        self._queue.put_nowait(job)
        self._prune()
        return job

    def position(self, job: Job) -> int | None:
        """1-based place among queued jobs, None once it has started."""
        if job.status != "queued":
            return None
        return 1 + sum(1 for j in self.jobs.values() if j.status == "queued" and j.seq < job.seq)

    def view(self, job: Job) -> dict:
        return job.view(self.position(job))

    def counts(self) -> dict:
        st = [j.status for j in self.jobs.values()]
        return {"queued": st.count("queued"), "running": st.count("running")}

    def _prune(self) -> None:
        done = [j for j in self.jobs.values() if j.status in ("done", "error")]
        for j in sorted(done, key=lambda j: j.seq)[:max(0, len(done) - MAX_FINISHED_JOBS)]:
            del self.jobs[j.id]

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            job.status, job.started = "running", time.time()
            try:
                job.result = _tool_result(await server.server.call_tool(job.tool, job.arguments))
                job.status = "done"
            except Exception as e:  # ToolError carries the same text MCP clients see
                job.error, job.status = str(e), "error"
            finally:
                job.finished = time.time()
                self._queue.task_done()


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
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        return None
    root = _output_dir().resolve()
    path = (root / name)
    if path.suffix.lower() not in AUDIO_EXTS:
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.parent != root or not resolved.is_file():
        return None
    return resolved


def _entry(path: Path) -> dict:
    st = path.stat()
    meta = None
    side = path.with_suffix(path.suffix + ".json")
    if side.is_file():
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
    seconds = (meta or {}).get("seconds")
    if not isinstance(seconds, (int, float)):
        try:
            import soundfile as sf
            seconds = round(float(sf.info(str(path)).duration), 3)
        except Exception:
            seconds = None
    return {"file": path.name, "path": str(path), "size": st.st_size, "mtime": st.st_mtime,
            "seconds": seconds, "format": path.suffix.lower().lstrip("."), "meta": meta}


def _search_text(e: dict) -> str:
    m = e["meta"] or {}
    sf = m.get("sound_fields") or {}
    parts = [e["file"], m.get("title"), m.get("prompt"), m.get("prompt_used"),
             m.get("original_idea"), m.get("audio_type"), m.get("kind"), str(m.get("seed", "")),
             *(sf.values() if isinstance(sf, dict) else [])]
    return " ".join(str(p) for p in parts if p).lower()


def list_library(query: str = "", offset: int = 0, limit: int = 100) -> dict:
    root = _output_dir()
    files = []
    if root.is_dir():
        files = [p for p in root.iterdir()
                 if p.suffix.lower() in AUDIO_EXTS and p.is_file() and not p.name.startswith(".")
                 and p.stat().st_size > 0]  # skip names reserved by a running generation
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    entries = [_entry(p) for p in files]
    words = query.lower().split()
    if words:
        entries = [e for e in entries if all(w in _search_text(e) for w in words)]
    return {"output_dir": str(root), "total": len(entries),
            "items": entries[offset:offset + limit]}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class LocalOnly:
    """ASGI middleware for /api: loopback Host only; writes need a same-origin
    Origin and a JSON body (blocks DNS rebinding and cross-site form posts)."""

    LOOPBACK = ("127.0.0.1", "localhost", "::1")

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

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        problem = None
        if not self._host_ok(headers.get("host", "")):
            problem = "host not allowed"
        elif scope["method"] not in ("GET", "HEAD"):
            origin = headers.get("origin")
            if origin is None or not self._host_ok(origin.split("://", 1)[-1]):
                problem = "cross-origin request refused"
            elif not headers.get("content-type", "").startswith("application/json"):
                problem = "expected application/json"
        if problem:
            return await _err(403, problem)(scope, receive, send)
        return await self.app(scope, receive, send)


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
    errors = validate_arguments(tool, arguments)
    if errors:
        msg = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
        return _err(422, f"invalid arguments: {msg}", fields=errors)
    job = jobs.submit(tool, arguments)
    return JSONResponse(jobs.view(job), status_code=202)


async def api_jobs_list(_: Request) -> Response:
    items = sorted(jobs.jobs.values(), key=lambda j: j.seq, reverse=True)
    return JSONResponse({"items": [jobs.view(j) for j in items], **jobs.counts()})


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
    return JSONResponse(await asyncio.to_thread(list_library, q.get("q", ""), offset, limit))


async def api_audio(request: Request) -> Response:
    path = _safe_audio_path(request.path_params["name"])
    if path is None:
        return _err(404, "not found")
    download = request.query_params.get("download") in ("1", "true")
    return FileResponse(path, media_type=AUDIO_EXTS[path.suffix.lower()],
                        filename=path.name if download else None,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "no-cache"})


async def index(_: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


API_ROUTES = [
    Route("/api/status", api_status, methods=["GET"]),
    Route("/api/unload", api_unload, methods=["POST"]),
    Route("/api/jobs", api_jobs_create, methods=["POST"]),
    Route("/api/jobs", api_jobs_list, methods=["GET"]),
    Route("/api/jobs/{job_id}", api_job, methods=["GET"]),
    Route("/api/library", api_library, methods=["GET"]),
    Route("/api/audio/{name:path}", api_audio, methods=["GET", "HEAD"]),
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
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    config = uvicorn.Config(create_app(port), host="127.0.0.1", port=port, log_level="info")
    srv = uvicorn.Server(config)

    def announce():
        while not srv.started:
            if srv.should_exit:
                return
            time.sleep(0.1)
        print(f"[audio-studio] web UI: {url}   MCP: {url}mcp", flush=True)
        if open_browser:
            webbrowser.open(url)

    threading.Thread(target=announce, daemon=True, name="open-browser").start()
    srv.run()
