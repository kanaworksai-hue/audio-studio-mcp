"""Regression coverage for Auto and the shared MCP/WebUI song queue."""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import numpy as np
import pytest
import soundfile as sf
from mcp import Client
from starlette.testclient import TestClient

from audio_studio_mcp import gpu, server, song_setup, songs, web

ALL = {"metal-q4", "metal-q8"}
MAC = songs.Resources(32, 16, 24, 1, "Apple M6")
REQUEST = songs.SongRequest(lyrics="[Verse]\nBright sky", style="city pop", seed=1234)


@pytest.fixture
def metal_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AUDIO_STUDIO_SONG_POLICY", raising=False)
    monkeypatch.setattr(songs, "song_backend", lambda: "metal")
    return songs.binary_path("metal")


@pytest.mark.parametrize("backend", ["metal", "cuda"])
def test_setup_can_install_on_low_ram(metal_setup, monkeypatch, capsys, backend):
    events = []
    monkeypatch.setattr(songs, "song_backend", lambda: backend)
    monkeypatch.setattr(song_setup.song_cuda, "nvidia_devices",
                        lambda: {0: song_setup.song_cuda.NvidiaDevice("610.88", (8, 6), 2 * songs.GIB)})
    monkeypatch.setattr(song_setup.song_cuda, "memory_status", lambda: (16 * songs.GIB, 2 * songs.GIB))
    monkeypatch.setattr(song_setup, "_install_metal_runtime", lambda *_: events.append("build"))
    monkeypatch.setattr(song_setup, "_install_cuda_runtime", lambda *_: events.append("install runtime"))
    monkeypatch.setattr(song_setup, "_install_weights", lambda *_: events.append("weights"))
    monkeypatch.setattr(songs, "probe_resources", lambda *_: songs.Resources(16, 2, 2, 4, "Test GPU"))

    assert song_setup.main(["--accept-license", "--profile", "both"]) == 0
    assert events == ["build" if backend == "metal" else "install runtime", "weights"]
    output = capsys.readouterr()
    assert "16.0 GiB installed RAM is below the song recommendation" in output.out
    assert not output.err


@pytest.mark.parametrize("backend", ["metal", "cuda"])
def test_low_ram_status_keeps_setup_and_installed_modes_available(
        metal_setup, fake, monkeypatch, capsys, backend):
    monkeypatch.setattr(songs, "song_backend", lambda: backend)
    monkeypatch.setattr(songs, "metal_installed_ram", lambda: 16)
    monkeypatch.setattr(song_setup.song_cuda, "memory_status", lambda: (16 * songs.GIB, 2 * songs.GIB))
    binary = songs.binary_path(backend)
    with TestClient(web.create_app(), base_url="http://127.0.0.1") as client:
        result = client.get("/api/status").json()["songs"]
        assert result["supported"] and not any(result["installed_modes"].values())
        assert result["preflight_error"] is None
        assert result["preflight"] == {"reason": "installed_ram", "recommended_gib": 32 if backend == "metal" else 31,
                                       "detected_gib": 16}
        assert result["runtime_problem"] and result["runtime_missing"]
        assert song_setup.main(["--check"]) == 1
        assert binary.name in capsys.readouterr().err

        binary.parent.mkdir(parents=True)
        binary.touch(mode=0o755)
        if backend == "cuda":
            for name in song_setup.song_cuda.REQUIRED_DLLS:
                (binary.parent / name).touch()
        monkeypatch.setattr(songs, "installed_profiles", lambda *_: {f"{backend}-q4"})
        monkeypatch.setattr(song_setup, "_verify", lambda _, name: "q8_0" not in name)
        monkeypatch.setattr(songs, "probe_resources", lambda *_: songs.Resources(16, 2, 2, 4, "Test GPU"))
        result = client.get("/api/status").json()["songs"]
        assert result["installed_modes"] == {"auto": True, "fast": True, "quality": False}
        assert result["runtime_problem"] is None
        assert song_setup.main(["--check"]) == 0
        assert "Auto at 180 s: Q4_0" in capsys.readouterr().out

        def broken_read(*_):
            raise OSError("test memory read failure")

        monkeypatch.setattr(songs, "metal_installed_ram", broken_read)
        monkeypatch.setattr(song_setup.song_cuda, "memory_status", broken_read)
        result = client.get("/api/status").json()["songs"]
        assert result["preflight_error"] is None and result["preflight"] is None
        assert result["installed_modes"]["auto"]


def test_auto_uses_installed_profiles_duration_budget_and_current_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STUDIO_HOME", str(tmp_path))
    policy = songs.Policy()
    assert REQUEST.duration_seconds == 180
    choice = songs.select(policy, MAC, ALL, REQUEST)
    assert choice.profile.id == "metal-q4" and "budget" in choice.reasons[0]
    assert songs.select(policy, MAC, ALL, REQUEST.model_copy(update={"duration_seconds": 30})).profile.id == "metal-q8"
    assert songs.select(policy, MAC, ALL, REQUEST.model_copy(update={"wait_budget_seconds": 300})).profile.id == "metal-q8"
    loaded = songs.Resources(32, 12, 24, 1, "Apple M6")
    choice = songs.select(songs.Policy(wait_budget_seconds=300), loaded, ALL, REQUEST)
    assert choice.profile.id == "metal-q4" and "available RAM" in choice.reasons[0]
    quality = songs.select(policy, loaded, ALL, REQUEST.model_copy(update={"mode": "quality"}))
    assert quality.profile.id == "metal-q8" and "available RAM" in quality.reasons[0]
    with pytest.raises(songs.SongRuntimeError, match="180s Auto budget"):
        songs.select(policy, MAC, ALL, REQUEST.model_copy(update={"wait_budget_seconds": 180}))
    with pytest.raises(songs.SongRuntimeError, match="weights are not installed"):
        songs.select(policy, MAC, {"metal-q4"}, REQUEST, pinned="metal-q8")
    stressed = songs.Resources(16, 2, 3, 4, "Apple M1")
    auto = songs.select(policy, stressed, ALL, REQUEST)
    assert auto.profile.id == "metal-q4" and any("critical" in reason for reason in auto.reasons)
    only_q8 = songs.select(policy, stressed, {"metal-q8"},
                           REQUEST.model_copy(update={"wait_budget_seconds": 300}))
    assert only_q8.profile.id == "metal-q8" and any("headroom" in reason for reason in only_q8.reasons)
    long = songs.SongRequest(seed=1234, duration_seconds=240)
    with pytest.raises(songs.SongRuntimeError, match="301s exceeds the 240s Auto budget"):
        songs.select(policy, MAC, ALL, long)
    assert songs.select(policy, MAC, ALL, long.model_copy(update={"wait_budget_seconds": 330})).profile.id == "metal-q4"


def test_lease_serializes_separate_processes(tmp_path):
    lock = tmp_path / "lease"
    waiting, entered = tmp_path / "waiting", tmp_path / "entered"
    program = """
import sys
from pathlib import Path
from types import SimpleNamespace
from audio_studio_mcp.gpu import file_lease
lock, waiting, entered = map(Path, sys.argv[1:])
waiting.touch()
with file_lease(lock):
    entered.touch()
"""
    process = None
    try:
        with gpu.file_lease(lock):
            process = subprocess.Popen([sys.executable, "-c", program, str(lock), str(waiting), str(entered)])
            deadline = time.monotonic() + 10
            while not waiting.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert waiting.is_file()
            time.sleep(.25)
            assert process.poll() is None and not entered.exists()
        assert process.wait(timeout=10) == 0 and entered.is_file()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


@pytest.fixture
def native(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(web, "jobs", web.JobQueue())
    monkeypatch.setattr(songs, "song_backend", lambda: "metal")
    monkeypatch.setattr(songs, "metal_installed_ram", lambda: MAC.total_gib)
    monkeypatch.setattr(songs, "installed_profiles", lambda *_: ALL)
    monkeypatch.setattr(songs, "probe_metal", lambda _: MAC)
    binary = songs.binary_path("metal")
    binary.parent.mkdir(parents=True)
    binary.touch(mode=0o755)
    return fake


def finish_cli(_binary, _weights, request, profile, work, cancel, update, *, seconds=1, **_):
    update("synthesis")
    out = work / "result"
    out.mkdir()
    sf.write(out / "song.wav", np.zeros((seconds * 48000, 2), dtype=np.float32), 48000, subtype="PCM_16")
    return 1.25, False


def test_oom_reselects_only_for_auto(native, monkeypatch, tmp_path):
    events = []

    def fail_q8(*args, **kwargs):
        profile = args[3]
        events.append(profile.id)
        if profile.id == "metal-q8":
            raise songs.SongMemoryError("allocation failed")
        return finish_cli(*args, **kwargs)

    def probe(_):
        # An allocation failure can refresh the resource estimate below every threshold.
        return songs.Resources(16, 1, 2, 4, MAC.device) if events else MAC

    monkeypatch.setattr(songs, "_run_cli", fail_q8)
    monkeypatch.setattr(songs, "probe_metal", probe)
    request = REQUEST.model_copy(update={"wait_budget_seconds": 300})
    _, meta = songs.generate(request, tmp_path, threading.Event(), lambda *args: None)
    assert events == ["metal-q8", "metal-q4"]
    assert meta["selection"]["reasons"][0] == "Q8_0 ran out of memory."
    assert any("available RAM" in reason for reason in meta["selection"]["reasons"])
    second = tmp_path / "manual"
    second.mkdir()
    with pytest.raises(songs.SongMemoryError):
        songs.generate(request.model_copy(update={"mode": "quality"}), second, threading.Event(), lambda *args: None)
    assert events[-1:] == ["metal-q8"]


async def tool(client, name, **kwargs):
    result = await client.call_tool(name, kwargs)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def wait_job(c, job_id, *, terminal=True):
    with anyio.fail_after(5):
        while True:
            status = await tool(c, "song_status", job_id=job_id)
            if terminal and status["status"] in ("done", "error", "cancelled"):
                return status
            if not terminal and status["phase"] == "synthesis":
                return status
            await anyio.sleep(.02)


@pytest.mark.anyio
async def test_song_holds_the_sa3_lease_and_running_cancel_releases_it(native, monkeypatch):
    def render(_binary, _weights, request, profile, work, cancel, update, **_):
        assert not native.loaded(), "SA3 must be evicted on its own worker"
        update("synthesis")
        assert cancel.wait(5)
        raise gpu.Cancelled()

    monkeypatch.setattr(songs, "_run_cli", render)
    native._preload()
    async with Client(server.server) as c:
        lead = await tool(c, "generate_song", lyrics=REQUEST.lyrics, style=REQUEST.style, seed=1234)
        await wait_job(c, lead["job_id"], terminal=False)
        next_song = await tool(c, "generate_song", lyrics=REQUEST.lyrics, style=REQUEST.style)
        assert (await tool(c, "cancel_song", job_id=next_song["job_id"]))["status"] == "cancelled"
        sa3 = asyncio.create_task(tool(c, "generate_audio", prompt="tone", duration_seconds=1))
        await anyio.sleep(.05)
        assert not sa3.done() and not native.requests
        await tool(c, "cancel_song", job_id=lead["job_id"])
        assert (await wait_job(c, lead["job_id"]))["status"] == "cancelled"
        assert (await sa3)["included_model_load"]
    assert not list(server.OUTPUT_DIR.glob(".song-*"))
    assert len(list(server.OUTPUT_DIR.glob("*.wav"))) == 1


@pytest.mark.parametrize("truncated", [True, False])
def test_web_song_accepts_job_and_regenerates_with_saved_profile(native, monkeypatch, tmp_path, truncated):
    rate = 48000
    pcm = np.tile(np.array([12000, -10000], dtype=np.int16), (3 * rate, 1))
    raw = tmp_path / "raw.wav"
    sf.write(raw, pcm, rate, subtype="PCM_16")

    def render(_binary, _weights, request, profile, work, cancel, update, **_):
        assert request.duration_seconds in (180, 240)
        output = work / "result"
        output.mkdir()
        (output / "song.wav").write_bytes(raw.read_bytes())
        return 1.25, truncated

    monkeypatch.setattr(songs, "_run_cli", render)
    base = "http://127.0.0.1:8765"
    with TestClient(web.create_app(port=8765), base_url=base) as c:
        def submit(arguments, status=202):
            response = c.post("/api/jobs", json={"tool": "generate_song", "arguments": arguments},
                              headers={"Origin": base})
            assert response.status_code == status, response.text
            return response.json()

        def completed(job_id):
            start = time.monotonic()
            while time.monotonic() - start < 5:
                job = c.get(f"/api/jobs/{job_id}").json()
                if job["status"] in ("done", "error"):
                    assert job["status"] == "done", job
                    return job
                time.sleep(.02)
            pytest.fail("song never finished")

        args = {"lyrics": REQUEST.lyrics, "style": REQUEST.style, "seed": 1234, "mode": "fast"}
        assert submit({**args, "duration_seconds": 241}, status=422)["fields"][0]["field"] == "duration_seconds"
        q4 = songs.profiles(songs.Policy())[1]
        songs.record_speed(q4, MAC.device, 30, 25.0)
        original = completed(submit({**args, "duration_seconds": 240})["id"])
        result = original["result"]
        assert original["selection"]["profile"] == "metal-q4"
        assert original["selection"]["eta_seconds"] == 301 and "speed_factor" not in original["selection"]
        audio = Path(result["candidates"][0]["path"])
        assert c.get("/api/audio/" + audio.name).status_code == 200
        assert c.get("/api/library?kind=song").json()["total"] == 1
        meta = json.loads(Path(result["candidates"][0]["metadata"]).read_text())
        assert meta["duration_limit_fade_ms"] == 2000
        if truncated:
            faded, sample_rate = sf.read(audio, dtype="int16")
            assert result["end_reason"] == "duration_limit" and sample_rate == rate
            assert faded.shape == pcm.shape and sf.info(audio).subtype == "PCM_16"
            np.testing.assert_array_equal(faded[:rate + 1], pcm[:rate + 1])
            np.testing.assert_allclose(faded[2 * rate], pcm[2 * rate] / 2, atol=1)
            np.testing.assert_array_equal(faded[-1], [0, 0])
        else:
            assert result["end_reason"] == "eos" and audio.read_bytes() == raw.read_bytes()

        repeated = completed(submit({"replay_from": audio.name})["id"])
        assert repeated["selection"]["profile"] == "metal-q4"
        assert repeated["result"]["candidates"][0]["seed"] == 1234
        assert Path(repeated["result"]["candidates"][0]["path"]).read_bytes() == audio.read_bytes()

        if truncated:
            legacy = audio.with_name("legacy.wav")
            legacy.write_bytes(raw.read_bytes())
            del meta["duration_limit_fade_ms"]
            meta["request"]["duration_seconds"] = 180
            profile = songs.profiles(songs.Policy())[1]
            meta["parameters"] = profile.parameters(180)
            legacy.with_suffix(".wav.json").write_text(json.dumps(meta), encoding="utf-8")
            replay = completed(submit({"replay_from": legacy.name})["id"])["result"]["candidates"][0]
            assert Path(replay["path"]).read_bytes() == legacy.read_bytes()
            assert json.loads(Path(replay["metadata"]).read_text())["duration_limit_fade_ms"] == 0


def test_speed_factors_are_isolated_by_length_profile_device_and_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STUDIO_HOME", str(tmp_path / "home"))
    q8, q4 = songs.profiles(songs.Policy())
    assert songs.record_speed(q4, "Apple M1", 30, 221.0) == pytest.approx(221 / 55.1, rel=1e-3)
    short = songs.speed_factors((q8, q4), "Apple M1", 60)
    assert short == {"metal-q4": pytest.approx(221 / 55.1, rel=1e-3)}
    assert songs.speed_factors((q8, q4), "Apple M6", 30) == {}
    assert songs.speed_factors((q8, q4), "Apple M1", 61) == {}
    assert songs.speed_factors((q8, q4), "Apple M1", 149) == {}
    assert songs.record_speed(q4, "Apple M1", 180, 230.6 * 2) == pytest.approx(2.0)
    assert songs.speed_factors((q8, q4), "Apple M1", 150) == {"metal-q4": 2.0}
    assert songs.record_speed(q4, "Apple M1", 180, 230.6) == pytest.approx(1.5)
    assert songs.speed_factors((q8, q4), "Apple M1", 30) == short
    songs.record_speed(q8, "Apple M1", 30, 0.001)
    assert songs.speed_factors((q8, q4), "Apple M1", 30)["metal-q8"] == songs.SPEED_FACTOR_LIMITS[0]
    with monkeypatch.context() as newer_runtime:
        newer_runtime.setattr(songs, "RUNTIME_REV", "a" * 40)
        assert songs.speed_factors((q8, q4), "Apple M1", 30) == {}
    songs._speed_path().write_text("not json", encoding="utf-8")
    assert songs.speed_factors((q8, q4), "Apple M1", 30) == {}
    assert songs.record_speed(q4, "Apple M1", 30, 55.1) == pytest.approx(1.0)


def test_short_cuda_calibration_does_not_discount_long_auto_or_setup_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUDIO_STUDIO_HOME", str(tmp_path))
    config = songs.CudaPolicy()
    q8, q4 = songs.profiles(config, "cuda")
    resources = songs.Resources(32, 18, 11, 1, "RTX 3060")
    installed = {q8.id, q4.id}
    long_request = REQUEST.model_copy(update={"duration_seconds": 240})
    short_request = REQUEST.model_copy(update={"duration_seconds": 30})
    assert songs.select(config, resources, installed, short_request, backend="cuda").estimate_seconds == 69

    path = songs._speed_path()
    path.parent.mkdir()
    old_key = f"{q8.id}|{resources.device}|{songs.runtime_revision('cuda')[:12]}"
    path.write_text(json.dumps({old_key: 0.3248}), encoding="utf-8")
    assert songs.select(config, resources, installed, short_request, backend="cuda").estimate_seconds == 69
    songs.record_speed(q8, resources.device, 30, 21.9)
    assert songs.select(config, resources, installed, short_request, backend="cuda").estimate_seconds == 22
    with pytest.raises(songs.SongRuntimeError, match="Q8_0 estimate 220s exceeds the 180s Auto budget"):
        songs.select(config, resources, installed, long_request, backend="cuda")
    pinned = songs.select(config, resources, installed, long_request, pinned=q8.id, backend="cuda")
    assert pinned.estimate_seconds == 220

    monkeypatch.setattr(songs, "runtime_problem", lambda *_: None)
    monkeypatch.setattr(songs, "probe_resources", lambda *_: resources)
    monkeypatch.setattr(song_setup, "_verify", lambda *_: True)
    song_setup._diagnose(tmp_path / "cli", "cuda", config)
    assert "Auto at 180 s: Q8_0 (estimated 177s)." in capsys.readouterr().out

    songs.record_speed(q8, resources.device, 199.84, 132.5)
    choice = songs.select(config, resources, installed, long_request, backend="cuda")
    assert choice.profile == q8 and choice.estimate_seconds == 153


def test_generate_records_speed_for_the_actual_length_after_early_eos(native, monkeypatch, tmp_path):
    def finish_early(*args, **kwargs):
        finish_cli(*args, **kwargs, seconds=30)
        return 44.0, False

    monkeypatch.setattr(songs, "_run_cli", finish_early)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, meta = songs.generate(REQUEST.model_copy(update={"mode": "fast"}), tmp_path / "a",
                             threading.Event(), lambda *args: None)
    assert meta["seconds"] == 30 and meta["end_reason"] == "eos"
    assert meta["selection"]["eta_seconds"] == 231 and "speed_factor" not in meta["selection"]
    q8, q4 = songs.profiles(songs.Policy())
    assert songs.speed_factors((q8, q4), MAC.device, 180) == {}
    _, meta = songs.generate(REQUEST.model_copy(update={"mode": "fast", "duration_seconds": 30}), tmp_path / "b",
                             threading.Event(), lambda *args: None)
    assert meta["selection"]["speed_factor"] == pytest.approx(44 / 55.1, rel=1e-3)
    assert meta["selection"]["eta_seconds"] == math.ceil(55.1 * meta["selection"]["speed_factor"])


@pytest.mark.parametrize("backend", ["metal", "cuda"])
def test_running_low_memory_only_warns_and_recovers(monkeypatch, tmp_path, backend):
    class FinishUnderPressure:
        def __init__(self, *args, stdout, **kwargs):
            self.polls = 0
            stdout.write(b"yue2.nar.weights.buffer_name Metal\n"
                         b"yue2.semantic.truncated 0\n")
            stdout.flush()

        returncode = 0

        def poll(self):
            self.polls += 1
            return 0 if self.polls >= 5 else None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class NoWait(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()

    readings = iter([2, 4, 4, 1] if backend == "metal" else [0, 0, 0, songs.GIB])
    clock = iter(range(0, 120, 20))
    monkeypatch.setattr(songs, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(songs.subprocess, "Popen", FinishUnderPressure)
    monkeypatch.setattr(songs, "memory_pressure", lambda: next(readings))
    monkeypatch.setattr(songs.song_cuda, "memory_status", lambda: (16 * songs.GIB, next(readings)))
    monkeypatch.setattr(songs.song_cuda, "process_options", lambda: {})
    _, fast = songs.profiles(songs.Policy(), backend)
    selection = songs.Selection(fast, 100, ())
    updates = []

    _, truncated = songs._run_cli(Path("audio.cpp"), tmp_path, REQUEST, fast, tmp_path,
                                  NoWait(), lambda *args: updates.append(args),
                                  backend=backend, device=0, selection=selection)

    assert not truncated
    assert updates[0] == ("synthesis",)
    warnings = [view["reasons"][-1] for _, view in updates[1:-1]]
    assert "You can cancel this song." in warnings[0]
    assert ("critical" if backend == "metal" else "below 1 GiB") in warnings[-1]
    assert updates[-1] == ("synthesis", selection.view())


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell and SIGKILL")
def test_metal_sigkill_is_reported_as_memory(native, monkeypatch, tmp_path):
    script = tmp_path / "killed.sh"
    script.write_text("#!/bin/sh\nkill -9 $$\n")
    script.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    q8, q4 = songs.profiles(songs.Policy())
    with pytest.raises(songs.SongMemoryError, match="SIGKILL"):
        songs._run_cli(script, tmp_path, REQUEST, q4, work, threading.Event(), lambda *_: None)
