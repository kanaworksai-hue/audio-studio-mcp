"""Tool-level tests through an in-process MCP client, with a fake backend."""
import json
import shutil
from pathlib import Path

import pytest
from mcp import Client

from audio_studio_mcp import post, server
from conftest import sine

pytestmark = pytest.mark.anyio


async def call(name, args):
    async with Client(server.server) as c:
        r = await c.call_tool(name, args)
    text = "".join(getattr(x, "text", "") for x in r.content)
    if r.is_error:
        raise AssertionError(text)
    return json.loads(text)


async def test_tools_listed():
    async with Client(server.server) as c:
        names = sorted(t.name for t in (await c.list_tools()).tools)
    assert names == ["edit_audio", "generate_audio", "process_audio", "unload_models"]


async def test_generate_writes_wav_and_sidecar(fake):
    r = await call("generate_audio", {
        "audio_type": "sfx", "duration_seconds": 1, "variations": 2, "seed": 7,
        "sound_source": "door", "sound_action": "slam", "title": "door test",
        "original_idea": "ドアがバタンと閉まる"})
    assert r["prompt_used"] == "TrackType: SFX, door, slam."
    assert r["included_model_load"] is True
    assert [c["seed"] for c in r["candidates"]] == [7, 8]
    for c in r["candidates"]:
        path = Path(c["path"])
        assert path.parent == server.OUTPUT_DIR and path.name.startswith("door_test_v")
        audio, sr = post.read(path)
        assert sr == 44100 and audio.shape == (2, 44100)
        assert c["raw_peak_dbfs"] > 0 and c["peak_dbfs"] == pytest.approx(-1.0, abs=0.01)
        meta = json.loads(Path(c["metadata"]).read_text())
        assert meta["original_idea"] == "ドアがバタンと閉まる"
        assert meta["backend"] == "fake" and meta["model"] == "fake-model"


async def test_generate_clamps_to_backend_max(fake):
    r = await call("generate_audio", {"prompt": "drone", "duration_seconds": 300, "seed": 1})
    assert r["seconds"] == fake.max_seconds


async def test_duplicate_titles_get_suffix(fake):
    a = await call("generate_audio", {"prompt": "x", "duration_seconds": 1, "title": "same"})
    b = await call("generate_audio", {"prompt": "x", "duration_seconds": 1, "title": "same"})
    assert Path(a["candidates"][0]["path"]).name == "same.wav"
    assert Path(b["candidates"][0]["path"]).name == "same_2.wav"


async def test_edit_modes_build_requests(fake, tmp_path):
    src = tmp_path / "in.wav"
    post.write(src, sine(4.0), 44100, "wav")

    r = await call("edit_audio", {"input_audio": str(src), "mode": "continue",
                                  "duration_seconds": 6, "seed": 1})
    req = fake.requests[-1][1]
    # regenerates from 1 s (overlap_seconds) before the end of the audible content
    assert req.inpaint_audio == str(src) and req.seconds == 6
    assert req.inpaint_range == (pytest.approx(3.0, abs=0.01), 6.0)
    assert r["region"] == [pytest.approx(3.0, abs=0.01), 6.0]

    await call("edit_audio", {"input_audio": str(src), "mode": "inpaint", "prompt": "calm",
                              "start_seconds": 1, "end_seconds": 2, "seed": 1})
    req = fake.requests[-1][1]
    assert req.inpaint_range == (1, 2) and req.seconds == 4.0
    assert req.prompt.startswith("TrackType: Music")

    await call("edit_audio", {"input_audio": str(src), "mode": "variation", "strength": 0.4,
                              "prompt": "jazz", "seed": 1})
    req = fake.requests[-1][1]
    assert req.init_audio == str(src) and req.init_strength == 0.4 and req.inpaint_audio is None


async def test_edit_validation(fake, tmp_path):
    src = tmp_path / "in.wav"
    post.write(src, sine(4.0), 44100, "wav")
    with pytest.raises(AssertionError, match="continue needs"):
        await call("edit_audio", {"input_audio": str(src), "mode": "continue", "duration_seconds": 3})
    with pytest.raises(AssertionError, match="start_seconds < end_seconds"):
        await call("edit_audio", {"input_audio": str(src), "mode": "inpaint"})
    with pytest.raises(AssertionError, match="not found"):
        await call("edit_audio", {"input_audio": str(tmp_path / "nope.wav"), "mode": "variation"})


async def test_process_keeps_source_metadata(fake, tmp_path):
    g = await call("generate_audio", {"prompt": "loop", "duration_seconds": 2, "seed": 3})
    src = g["candidates"][0]["path"]
    r = await call("process_audio", {"input_audio": src, "mono": True, "normalize": "peak",
                                     "loop": True, "loop_crossfade_ms": 100, "fade_in_ms": 0,
                                     "format": "flac", "sample_rate": 44100})
    assert r["channels"] == 1 and r["format"] == "flac" and r["seconds"] == pytest.approx(1.9, abs=0.01)
    assert r["peak_dbfs"] == pytest.approx(-1.0, abs=0.01)
    meta = json.loads(Path(r["metadata"]).read_text())
    assert meta["source"]["seed"] == 3


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_process_resamples_to_48k(fake, tmp_path):
    src = tmp_path / "in.wav"
    post.write(src, sine(1.0), 44100, "wav")
    r = await call("process_audio", {"input_audio": str(src), "format": "ogg"})
    assert r["sample_rate"] == 48000 and r["steps"][0] == "resample 44100→48000"


async def test_unload(fake):
    assert (await call("unload_models", {}))["unloaded"] is False
    await call("generate_audio", {"prompt": "x", "duration_seconds": 1})
    assert (await call("unload_models", {}))["unloaded"] is True


async def test_input_schema_survives_error_wrapper():
    async with Client(server.server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    props = tools["generate_audio"].input_schema["properties"]
    assert props["audio_type"]["enum"] == ["music", "instrument", "sfx", "raw"]
    assert "sound_source" in props and props["variations"]["maximum"] == 4


async def test_missing_ffmpeg_message_reaches_client(fake, tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIO_STUDIO_FFMPEG", raising=False)
    monkeypatch.setattr(post.shutil, "which", lambda _: None)
    src = tmp_path / "in.wav"
    post.write(src, sine(1.0), 44100, "wav")
    with pytest.raises(AssertionError, match="ffmpeg not found"):
        await call("process_audio", {"input_audio": str(src)})


async def test_backend_failure_message_reaches_client(fake, monkeypatch):
    def boom(seeds, req):
        raise RuntimeError("weights download failed: 503")
    monkeypatch.setattr(fake, "_generate", boom)
    with pytest.raises(AssertionError, match="RuntimeError: weights download failed: 503"):
        await call("generate_audio", {"prompt": "x", "duration_seconds": 1})


async def test_empty_prompt_message_reaches_client(fake):
    with pytest.raises(AssertionError, match="empty prompt"):
        await call("generate_audio", {"audio_type": "sfx"})


async def test_parallel_calls_never_share_a_file(fake, tmp_path):
    import anyio
    src = tmp_path / "in.wav"
    post.write(src, sine(0.5), 44100, "wav")
    paths = []

    async def one():
        paths.append((await call("process_audio", {"input_audio": str(src), "sample_rate": 44100}))["path"])

    async with anyio.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(one)
    assert len(set(paths)) == 6


async def test_seed_minus_one_is_random_and_lower_rejected(fake):
    r = await call("generate_audio", {"prompt": "x", "duration_seconds": 1, "seed": -1})
    assert r["candidates"][0]["seed"] >= 0
    with pytest.raises(AssertionError):
        await call("generate_audio", {"prompt": "x", "duration_seconds": 1, "seed": -2})


async def test_edit_region_is_checked_after_clamping(fake, tmp_path):
    src = tmp_path / "in.wav"
    post.write(src, sine(4.0), 44100, "wav")
    with pytest.raises(AssertionError, match="nothing to regenerate"):
        await call("edit_audio", {"input_audio": str(src), "mode": "inpaint",
                                  "start_seconds": 50, "end_seconds": 55, "duration_seconds": 40})
    r = await call("edit_audio", {"input_audio": str(src), "mode": "inpaint", "start_seconds": 2,
                                  "end_seconds": 9, "duration_seconds": 6})
    assert fake.requests[-1][1].inpaint_range == (2, 6.0) and r["region"] == [2, 6.0]
    long_src = tmp_path / "long.wav"
    post.write(long_src, sine(61.0, sr=8000), 8000, "wav")  # longer than the fake max (60 s)
    with pytest.raises(AssertionError, match="nothing to regenerate"):
        await call("edit_audio", {"input_audio": str(long_src), "mode": "continue", "duration_seconds": 90})


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_loudness_with_ceiling_below_loudnorm_range(fake, tmp_path):
    src = tmp_path / "in.wav"
    post.write(src, sine(5.0, amp=0.05), 44100, "wav")
    r = await call("process_audio", {"input_audio": str(src), "normalize": "loudness",
                                     "target_lufs": -20, "peak_ceiling_db": -12})
    assert r["peak_dbfs"] <= -12.0


async def test_continue_skips_trailing_silence(fake, tmp_path):
    import numpy as np
    sr = 44100
    src = tmp_path / "ends_silent.wav"
    post.write(src, np.concatenate([sine(4.0), np.zeros((2, sr), np.float32)], axis=1), sr, "wav")
    r = await call("edit_audio", {"input_audio": str(src), "mode": "continue",
                                  "duration_seconds": 8, "overlap_seconds": 0.5, "seed": 1})
    start = fake.requests[-1][1].inpaint_range[0]
    assert 3.4 < start < 3.6, start  # 4 s of sound - 0.5 s overlap, not the 5 s file end
    assert r["region"][0] == round(start, 2)
