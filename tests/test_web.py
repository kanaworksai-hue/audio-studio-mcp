"""Web UI API tests (`--web`), with the fake backend."""
from __future__ import annotations

import functools
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from mcp import Client
from starlette.testclient import TestClient

from audio_studio_mcp import post, server, web
from conftest import sine

TESTS = Path(__file__).parent


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr(web, "jobs", web.JobQueue())
    with TestClient(web.create_app(), base_url="http://127.0.0.1") as c:
        c.headers.update({"Origin": "http://127.0.0.1"})
        yield c


def wait_job(client, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_index_and_static_are_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Audio Studio</title>" in r.text
    for f in ("app.js", "app.css", "i18n.js"):
        assert client.get(f"/static/{f}").status_code == 200


def test_static_files_ship_in_the_package():
    for f in ("index.html", "app.js", "app.css", "i18n.js"):
        assert (web.STATIC_DIR / f).is_file()


def test_status(client, fake):
    s = client.get("/api/status").json()
    assert s["backend"] == "fake" and s["loaded"] is False
    assert s["max_seconds"] == fake.max_seconds
    assert s["default_seconds"]["sfx"] == 5.0
    assert s["output_dir"] == str(server.OUTPUT_DIR)
    assert s["jobs"] == {"queued": 0, "running": 0}


def test_generate_job_matches_the_mcp_tool(client, fake):
    r = client.post("/api/jobs", json={"tool": "generate_audio", "arguments": {
        "audio_type": "sfx", "sound_source": "door", "sound_action": "slam",
        "duration_seconds": 1, "variations": 2, "seed": 7}})
    assert r.status_code == 202
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["prompt_used"] == "TrackType: SFX, door, slam."
    assert [c["seed"] for c in res["candidates"]] == [7, 8]
    for c in res["candidates"]:
        assert Path(c["path"]).parent == server.OUTPUT_DIR and Path(c["metadata"]).is_file()
    assert job["elapsed"] >= 0 and job["position"] is None
    assert client.get("/api/status").json()["loaded"] is True


def test_invalid_arguments_rejected_before_queueing(client, fake):
    r = client.post("/api/jobs", json={"arguments": {"prompt": "x", "variations": 9, "steps": 0}})
    assert r.status_code == 422
    fields = {f["field"] for f in r.json()["fields"]}
    assert fields == {"variations", "steps"}
    assert client.get("/api/jobs").json()["items"] == []
    assert fake.requests == []


def test_tool_error_reaches_the_job_with_the_mcp_message(client, fake):
    # passes schema validation, fails inside the tool: same text an MCP client gets
    r = client.post("/api/jobs", json={"arguments": {"audio_type": "sfx"}})
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "error"
    assert "empty prompt" in job["error"]


async def _mcp_error_text(args):
    async with Client(server.server) as c:
        r = await c.call_tool("generate_audio", args)
    assert r.is_error
    return r.content[0].text


def test_error_text_identical_to_mcp(client, fake):
    r = client.post("/api/jobs", json={"arguments": {"audio_type": "sfx"}})
    job = wait_job(client, r.json()["id"])
    assert job["error"] == anyio.run(_mcp_error_text, {"audio_type": "sfx"})


def test_only_whitelisted_tools(client, tmp_path):
    for tool in ("unload_models", "nope"):
        r = client.post("/api/jobs", json={"tool": tool, "arguments": {}})
        assert r.status_code == 400, tool
    assert client.post("/api/jobs", json={"arguments": []}).status_code == 400
    assert client.post("/api/jobs", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400


def _input_cases(out: Path) -> list:
    """input_audio values the page must not be able to use, plus setup."""
    secret = out.parent / "secret.wav"
    post.write(secret, sine(0.1), 44100, "wav")
    (out / "sub").mkdir()
    post.write(out / "sub" / "x.wav", sine(0.1), 44100, "wav")
    cases = [str(secret), "../secret.wav", "..\\secret.wav", "sub/x.wav", str(out / "sub" / "x.wav"),
             str(out / ".." / "secret.wav"), "notes.txt", str(out / "notes.txt"), "a.wav.json",
             "missing.wav", "reserved.wav", "", ".", "..", "secret.wav", "/etc/passwd", None, 3, ["a.wav"]]
    try:
        (out / "link.wav").symlink_to(secret)
        (out.parent / "linkdir").symlink_to(out.parent, target_is_directory=True)
        cases += ["link.wav", str(out.parent / "linkdir" / "secret.wav")]
    except OSError:  # Windows without symlink privilege
        pass
    return cases


@pytest.mark.parametrize("tool", ["edit_audio", "process_audio"])
def test_input_audio_must_be_in_the_output_folder(client, fake, tool):
    out = _make_library(fake)
    for value in _input_cases(out):
        args = {"input_audio": value, "mode": "variation"}
        r = client.post("/api/jobs", json={"tool": tool, "arguments": args})
        assert r.status_code == 422, (value, r.status_code)
        assert r.json()["fields"][0]["field"] == "input_audio"
    assert client.get("/api/jobs").json()["items"] == []
    assert fake.requests == []


def test_edit_job_from_the_library(client, fake):
    out = _make_library(fake)
    for value in ("a.wav", str(out / "a.wav")):  # name or the path the library shows
        r = client.post("/api/jobs", json={"tool": "edit_audio", "arguments": {
            "input_audio": value, "mode": "inpaint", "prompt": "thunder", "audio_type": "sfx",
            "start_seconds": 0.25, "end_seconds": 0.5, "variations": 2, "seed": 5}})
        assert r.status_code == 202, r.json()
        assert r.json()["arguments"]["input_audio"] == str(out / "a.wav")
        job = wait_job(client, r.json()["id"])
        assert job["status"] == "done", job
        assert job["result"]["mode"] == "inpaint" and job["result"]["region"] == [0.25, 0.5]
        assert [c["seed"] for c in job["result"]["candidates"]] == [5, 6]
    req = fake.requests[-1][1]
    assert req.inpaint_audio == str(out / "a.wav") and req.inpaint_range == (0.25, 0.5)
    names = {i["file"] for i in client.get("/api/library?q=inpaint").json()["items"]}
    assert len(names) == 4 and all(n.startswith("a_inpaint") for n in names)


def test_process_job_exports_into_the_library(client, fake):
    _make_library(fake)
    r = client.post("/api/jobs", json={"tool": "process_audio", "arguments": {
        "input_audio": "a.wav", "start_seconds": 0.2, "end_seconds": 0.8, "mono": True,
        "fade_out_ms": 50, "format": "flac", "sample_rate": 44100, "title": "cut"}})
    assert r.status_code == 202, r.json()
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["format"] == "flac" and res["channels"] == 1 and res["seconds"] == pytest.approx(0.6, abs=0.01)
    item = client.get("/api/library?q=cut").json()["items"][0]
    assert item["file"] == "cut.flac" and item["meta"]["kind"] == "process"
    assert fake.requests == []  # no model involved


def test_edit_arguments_validated_before_queueing(client, fake):
    _make_library(fake)
    r = client.post("/api/jobs", json={"tool": "edit_audio", "arguments": {
        "input_audio": "a.wav", "mode": "remix", "strength": 2}})
    assert r.status_code == 422
    assert {f["field"] for f in r.json()["fields"]} == {"mode", "strength"}


def test_peaks(client, fake):
    out = _make_library(fake)
    audio = sine(1.0, freq=5.0, amp=0.5)
    audio[1] *= -0.5  # channels differ: peaks cover both
    post.write(out / "p.wav", audio, 44100, "wav")
    r = client.get("/api/peaks/p.wav?n=100").json()
    assert r["channels"] == 2 and r["sample_rate"] == 44100
    assert r["seconds"] == pytest.approx(1.0) and (r["start"], r["end"]) == (0, 1.0)
    assert len(r["min"]) == len(r["max"]) == 100
    assert max(r["max"]) == pytest.approx(0.5, abs=1e-3) and min(r["min"]) == pytest.approx(-0.5, abs=1e-3)
    assert all(lo <= hi for lo, hi in zip(r["min"], r["max"]))
    # a zoomed window narrower than n samples still returns n buckets
    z = client.get("/api/peaks/p.wav?n=500&start=0.5&end=0.501").json()
    assert len(z["min"]) == 500 and z["start"] == 0.5 and z["end"] == pytest.approx(0.501, abs=1e-4)
    # out-of-range windows are clamped, not errors
    e = client.get("/api/peaks/p.wav?n=10&start=5&end=9").json()
    assert e["min"] == [] and e["start"] == e["end"] == 1.0
    assert client.get("/api/peaks/p.wav?n=x").status_code == 400
    for bad in ("nan", "inf", "-inf"):
        assert client.get(f"/api/peaks/p.wav?n=10&start={bad}&end=1").status_code == 400
    # summary path (coarse) and exact path (fine) agree on a window
    coarse = client.get("/api/peaks/p.wav?n=4&start=0&end=1").json()
    fine = client.get("/api/peaks/p.wav?n=400&start=0&end=0.01").json()
    assert len(coarse["max"]) == 4 and len(fine["max"]) == 400
    assert max(coarse["max"]) == pytest.approx(0.5, abs=1e-3)
    assert client.get("/api/peaks/b.flac?n=10").json()["seconds"] == pytest.approx(0.5, abs=0.01)
    for bad in ("../secret.wav", "notes.txt", "missing.wav"):
        assert client.get(f"/api/peaks/{bad}").status_code == 404
    (out / "broken.wav").write_bytes(b"RIFF....not audio")
    assert client.get("/api/peaks/broken.wav").status_code == 422


def test_queue_runs_in_order_with_positions(client, fake):
    gate = threading.Event()
    orig = fake._generate

    def slow(seeds, req):
        gate.wait(10)
        return orig(seeds, req)

    fake._generate = slow
    ids = [client.post("/api/jobs", json={"arguments": {"prompt": f"p{i}", "duration_seconds": 1, "seed": i}}).json()["id"]
           for i in range(3)]
    end = time.time() + 5
    while client.get(f"/api/jobs/{ids[0]}").json()["status"] != "running" and time.time() < end:
        time.sleep(0.02)
    views = [client.get(f"/api/jobs/{i}").json() for i in ids]
    assert [v["status"] for v in views] == ["running", "queued", "queued"]
    assert [v["position"] for v in views] == [None, 1, 2]
    assert client.get("/api/status").json()["jobs"] == {"queued": 2, "running": 1}
    gate.set()
    done = [wait_job(client, i) for i in ids]
    assert all(d["status"] == "done" for d in done)
    # FIFO: each job started after the previous one finished
    for a, b in zip(done, done[1:]):
        assert b["started"] >= a["finished"]
    assert [r[1].prompt for r in fake.requests] == [f"TrackType: Music, VocalType: Instrumental, p{i}." for i in range(3)]
    listed = client.get("/api/jobs").json()["items"]
    assert [j["id"] for j in listed] == ids[::-1]


def test_jobs_list_filtered_by_ids(client, fake):
    ids = [client.post("/api/jobs", json={"arguments": {"prompt": f"p{i}", "duration_seconds": 1}}).json()["id"]
           for i in range(3)]
    for i in ids:
        wait_job(client, i)
    r = client.get(f"/api/jobs?ids={ids[0]},{ids[2]},nope").json()
    assert [j["id"] for j in r["items"]] == [ids[2], ids[0]]
    assert len(client.get("/api/jobs").json()["items"]) == 3


def test_cancel_waiting_job(client, fake):
    gate = threading.Event()
    orig = fake._generate
    fake._generate = lambda seeds, req: (gate.wait(10), orig(seeds, req))[1]
    a, b, c = (client.post("/api/jobs", json={"arguments": {"prompt": f"p{i}", "duration_seconds": 1}}).json()["id"]
               for i in range(3))
    end = time.time() + 5
    while client.get(f"/api/jobs/{a}").json()["status"] != "running" and time.time() < end:
        time.sleep(0.02)
    assert client.post(f"/api/jobs/{a}/cancel", json={}).status_code == 409  # running
    r = client.post(f"/api/jobs/{b}/cancel", json={})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.get(f"/api/jobs/{c}").json()["position"] == 1
    assert client.post("/api/jobs/nope/cancel", json={}).status_code == 404
    gate.set()
    assert wait_job(client, c)["status"] == "done"
    assert client.get(f"/api/jobs/{b}").json()["status"] == "cancelled"
    assert [r[1].prompt for r in fake.requests] == [f"TrackType: Music, VocalType: Instrumental, p{i}." for i in (0, 2)]
    assert client.get("/api/jobs").json()["loaded"] is True


def test_library_item(client, fake):
    _make_library(fake)
    item = client.get("/api/library/a.wav").json()
    assert item["file"] == "a.wav" and item["meta"]["seed"] == 42
    for bad in ("../secret.wav", "notes.txt", "missing.wav"):
        assert client.get(f"/api/library/{bad}").status_code == 404


def test_unload(client, fake):
    assert client.post("/api/unload", json={}).json() == {"unloaded": False}
    fake._loaded = True
    assert client.post("/api/unload", json={}).json() == {"unloaded": True}
    assert fake.loaded() is False


def _make_library(fake):
    out = server.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    post.write(out / "a.wav", sine(1.0), 44100, "wav")
    post.write_sidecar(out / "a.wav", {"kind": "generate", "title": "Rain loop", "prompt": "soft rain",
                                       "audio_type": "sfx", "seed": 42, "seconds": 1.0})
    post.write(out / "b.flac", sine(0.5), 44100, "flac")  # no sidecar
    os.utime(out / "a.wav", (1_700_000_000, 1_700_000_000))
    os.utime(out / "b.flac", (1_700_000_100, 1_700_000_100))
    (out / "notes.txt").write_text("x")
    (out / "reserved.wav").touch()  # name reserved by a running generation
    return out


def test_library_lists_audio_with_sidecars(client, fake):
    out = _make_library(fake)
    lib = client.get("/api/library").json()
    assert lib["output_dir"] == str(out) and lib["total"] == 2
    files = [i["file"] for i in lib["items"]]
    assert files == ["b.flac", "a.wav"]  # newest first
    a = lib["items"][1]
    assert a["meta"]["seed"] == 42 and a["seconds"] == 1.0 and a["path"] == str(out / "a.wav")
    b = lib["items"][0]
    assert b["meta"] is None and b["seconds"] == pytest.approx(0.5, abs=0.01)


def test_library_search_and_paging(client, fake):
    _make_library(fake)
    assert [i["file"] for i in client.get("/api/library?q=RAIN").json()["items"]] == ["a.wav"]
    assert client.get("/api/library?q=rain 42").json()["total"] == 1
    assert client.get("/api/library?q=rain 43").json()["total"] == 0
    page = client.get("/api/library?limit=1&offset=1").json()
    assert page["total"] == 2 and [i["file"] for i in page["items"]] == ["a.wav"]
    assert client.get("/api/library?limit=x").status_code == 400


def test_library_kind_filter(client, fake):
    out = _make_library(fake)  # a.wav = generated sfx, b.flac = no sidecar
    post.write(out / "c.wav", sine(0.2), 44100, "wav")
    post.write_sidecar(out / "c.wav", {"kind": "edit:inpaint", "audio_type": "music"})
    post.write(out / "d.ogg", sine(0.2), 44100, "ogg")
    post.write_sidecar(out / "d.ogg", {"kind": "process", "source": {"audio_type": "sfx"}})
    files = lambda k, q="": [i["file"] for i in client.get(f"/api/library?kind={k}&q={q}").json()["items"]]
    assert files("sfx") == ["a.wav"]
    assert files("music") == ["c.wav"]  # an edit of music is still music
    assert files("edit") == ["c.wav"] and files("process") == ["d.ogg"]
    assert files("instrument") == [] and files("sfx", "rain") == ["a.wav"] and files("sfx", "nope") == []
    assert client.get("/api/library?kind=video").status_code == 400


def test_symlinks_and_hidden_files(client, fake):
    out = _make_library(fake)
    secret = out.parent / "secret.wav"
    post.write(secret, sine(0.1), 44100, "wav")
    (out / ".hidden.wav").write_bytes((out / "a.wav").read_bytes())
    try:
        (out / "outside.wav").symlink_to(secret)
        (out / "alias.wav").symlink_to(out / "a.wav.json")  # audio name, JSON target
        (out / "inside.wav").symlink_to(out / "a.wav")
    except OSError:
        pytest.skip("symlinks not available")
    listed = {i["file"] for i in client.get("/api/library").json()["items"]}
    assert listed == {"a.wav", "b.flac", "inside.wav"}
    for name in ("outside.wav", "alias.wav", ".hidden.wav"):
        for api in ("audio", "peaks", "library"):
            assert client.get(f"/api/{api}/{name}").status_code == 404, (api, name)
    assert client.get("/api/audio/inside.wav").status_code == 200


def test_library_survives_bad_sidecars_and_picks_up_new_ones(client, fake):
    out = _make_library(fake)
    (out / "b.flac.json").write_text("[1, 2]")  # valid JSON, not an object
    post.write(out / "c.wav", sine(0.2), 44100, "wav")
    (out / "c.wav.json").write_text("{broken")
    lib = client.get("/api/library?q=wav").json()
    assert {i["file"] for i in lib["items"]} == {"a.wav", "c.wav"}
    assert client.get("/api/library").json()["total"] == 3
    # a sidecar written after the first listing (cached entry) shows up
    post.write_sidecar(out / "c.wav", {"kind": "generate", "title": "late", "seed": 1})
    assert [i["file"] for i in client.get("/api/library?q=late").json()["items"]] == ["c.wav"]


def test_library_without_output_dir(client, fake):
    assert client.get("/api/library").json()["total"] == 0


def test_audio_served_with_range_and_download(client, fake):
    _make_library(fake)
    r = client.get("/api/audio/a.wav")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content == (server.OUTPUT_DIR / "a.wav").read_bytes()
    part = client.get("/api/audio/a.wav", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    d = client.get("/api/audio/a.wav?download=1")
    assert d.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("name", [
    "../secret.wav", "..%2Fsecret.wav", "%2e%2e/secret.wav", "sub/x.wav", "..\\secret.wav",
    "notes.txt", "a.wav.json", "missing.wav", "/etc/passwd", "%2Fetc%2Fpasswd", "link.wav",
])
def test_audio_path_traversal_blocked(client, fake, name):
    out = _make_library(fake)
    secret = out.parent / "secret.wav"
    post.write(secret, sine(0.1), 44100, "wav")
    (out / "sub").mkdir()
    post.write(out / "sub" / "x.wav", sine(0.1), 44100, "wav")
    if name == "link.wav":
        try:
            (out / "link.wav").symlink_to(secret)
        except OSError:  # Windows without symlink privilege
            pytest.skip("symlinks not available")
    r = client.get(f"/api/audio/{name}")
    assert r.status_code == 404, (name, r.status_code)


def test_rejects_foreign_host_and_cross_origin_writes(fake, monkeypatch):
    monkeypatch.setattr(web, "jobs", web.JobQueue())
    app = web.create_app(port=8765)
    good = "http://127.0.0.1:8765"
    with TestClient(app, base_url=good) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/status", headers={"Host": "localhost:8765"}).status_code == 200
        # DNS rebinding: attacker's name resolving to 127.0.0.1
        assert c.get("/api/status", headers={"Host": "evil.example:8765"}).status_code == 403
        assert c.get("/api/library", headers={"Host": "127.0.0.1:9999"}).status_code == 403
        body = {"arguments": {"prompt": "x", "duration_seconds": 1}}
        assert c.post("/api/jobs", json=body).status_code == 403  # no Origin
        assert c.post("/api/jobs", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/jobs", json=body, headers={"Origin": "null"}).status_code == 403
        assert c.post("/api/unload", json={}, headers={"Origin": "http://127.0.0.1:1234"}).status_code == 403
        # a cross-site HTML form can only send form/text bodies
        assert c.post("/api/jobs", content=b'{"arguments":{"prompt":"x"}}',
                      headers={"Origin": good, "Content-Type": "text/plain"}).status_code == 403
        assert c.post("/api/jobs", json=body, headers={"Origin": good}).status_code == 202
        # reads from another site's page (e.g. <audio src> or fetch) are refused too
        for site in ("cross-site", "same-site"):
            assert c.get("/api/library", headers={"Sec-Fetch-Site": site}).status_code == 403
        assert c.get("/api/library", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert c.get("/api/library", headers={"Sec-Fetch-Site": "none"}).status_code == 200
        # the page cannot be framed (clickjacking)
        r = c.get("/")
        assert r.status_code == 200 and r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert c.get("/api/status", headers={"Host": "evil.example"}).headers["x-frame-options"] == "DENY"


def test_jobs_interrupted_by_a_new_event_loop_are_reported(fake):
    q = web.JobQueue()
    gate = threading.Event()
    orig = fake._generate
    fake._generate = lambda seeds, req: (gate.wait(10), orig(seeds, req))[1]

    async def first():
        q.submit("generate_audio", {"prompt": "x", "duration_seconds": 1})
        q.submit("generate_audio", {"prompt": "y", "duration_seconds": 1})
        await anyio.sleep(0.2)  # first one running, second queued; then the loop ends
    anyio.run(first)
    gate.set()
    running, queued = sorted(q.jobs.values(), key=lambda j: j.seq)
    assert (running.status, running.error) == ("error", "cancelled")
    assert queued.status == "queued"

    async def second():
        job = q.submit("unload_models", {})
        while job.status != "done":
            await anyio.sleep(0.01)
    anyio.run(second)
    assert queued.status == "error" and "interrupted" in queued.error


# ── real process: `audio-studio-mcp --web PORT` ──────────────────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(base, path, body=None):
    """Tiny urllib client (the test env has no httpx)."""
    import json
    import urllib.request
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Origin": base, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read().decode()
    return json.loads(data) if r.headers.get_content_type() == "application/json" else data


@pytest.mark.anyio
async def test_web_flag_serves_ui_api_and_mcp_on_one_port(tmp_path):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "AUDIO_STUDIO_OUTPUT_DIR": str(tmp_path), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, str(TESTS / "fake_web_server.py"), "--web", str(port), "--no-browser"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    http = functools.partial(anyio.to_thread.run_sync, _http, base)
    try:
        with anyio.fail_after(20):
            while True:
                try:
                    await http("/api/status")
                    break
                except OSError:
                    await anyio.sleep(0.1)
        assert "Audio Studio" in await http("/")
        job = await http("/api/jobs", {"arguments": {"prompt": "x", "duration_seconds": 1, "seed": 3}})
        # an MCP client on /mcp of the same process shares the backend
        async with Client(f"{base}/mcp") as c:
            r = await c.call_tool("generate_audio", {"prompt": "y", "duration_seconds": 1, "seed": 4})
            assert not r.is_error, r.content
        with anyio.fail_after(10):
            while (j := await http(f"/api/jobs/{job['id']}"))["status"] not in ("done", "error"):
                await anyio.sleep(0.05)
        assert j["status"] == "done"
        assert (await http("/api/library"))["total"] == 2  # both the web job and the MCP call
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0].decode(errors="replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0].decode(errors="replace")
    assert f"web UI: {base}/" in out, out


def test_web_and_http_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        server.main(["--web", "8000", "--http", "8001"])
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "70000", "x"])
def test_web_port_is_validated(capsys, port):
    with pytest.raises(SystemExit):
        server.main(["--web", port])
    assert "--web" in capsys.readouterr().err


def test_web_opens_browser_once_started(monkeypatch, capsys):
    opened = []

    class FakeServer:  # stands in for uvicorn.Server: startup succeeds or fails (port in use)
        fail = False

        def __init__(self, config):
            self.started = False
            assert config.host == "127.0.0.1" and config.port == 8123

        async def startup(self, sockets=None):
            self.started = not self.fail

        def run(self):
            anyio.run(self.startup)
            time.sleep(0.2)  # the browser opens on a thread

    import uvicorn
    import webbrowser
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    web.serve(8123, open_browser=True)
    assert opened == ["http://127.0.0.1:8123/"]
    assert "web UI: http://127.0.0.1:8123/" in capsys.readouterr().out
    opened.clear()
    web.serve(8123, open_browser=False)
    assert opened == []
    capsys.readouterr()
    FakeServer.fail = True
    web.serve(8123, open_browser=True)
    assert opened == [] and "web UI" not in capsys.readouterr().out
