"""In-chat player (MCP Apps): the UI resource, the tools' UI metadata, the
app-only audio_preview tool, and the fallback for clients without MCP Apps
(their tool list and results must stay exactly as before the player)."""
import base64
import io
import json
import re
from pathlib import Path

import pytest
import soundfile as sf
from mcp import Client
from mcp.client.extension import ClientExtension

from audio_studio_mcp import chat_player, post, server
from conftest import sine

pytestmark = pytest.mark.anyio

UI_TOOLS = {"generate_audio", "edit_audio", "process_audio"}
CORE_TOOLS = ["edit_audio", "generate_audio", "process_audio", "unload_models"]


class AppsClient(ClientExtension):
    """What Claude Desktop, VS Code, … advertise at initialize."""
    identifier = "io.modelcontextprotocol/ui"

    def settings(self):
        return {"mimeTypes": ["text/html;profile=mcp-app"]}


def client(apps: bool) -> Client:
    return Client(server.server, extensions=[AppsClient()] if apps else None)


def text(r) -> str:
    return "".join(getattr(x, "text", "") for x in r.content)


async def call(c, name, args):
    r = await c.call_tool(name, args)
    assert not r.is_error, text(r)
    return json.loads(text(r))


# ── clients with MCP Apps ────────────────────────────────────────────────────
async def test_apps_client_sees_the_player():
    async with client(True) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        resources = (await c.list_resources()).resources
        page = (await c.read_resource(chat_player.RESOURCE_URI)).contents[0]
    for name in UI_TOOLS:
        assert tools[name].meta == {"ui": {"resourceUri": chat_player.RESOURCE_URI}}
    assert tools["audio_preview"].meta == {"ui": {"visibility": ["app"]}}  # hidden from the model
    assert tools["unload_models"].meta is None
    assert [str(r.uri) for r in resources] == [chat_player.RESOURCE_URI]
    assert page.mime_type == "text/html;profile=mcp-app"
    assert page.meta == {"ui": {"prefersBorder": True}}


def test_player_html_is_self_contained():
    html = chat_player.player_html()
    assert "/*@" not in html  # every placeholder filled
    assert "function drawPeaks(" in html  # the web UI's waveform code, inlined
    presets = json.loads(re.search(r"const PRESETS = (\{.*?\});", html).group(1))
    assert presets == chat_player.EXPORT_PRESETS
    # the host's default CSP blocks every network load: nothing external, no fetch
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html)
    assert "@import" not in html
    own = (chat_player.STATIC_DIR / "player.html").read_text(encoding="utf-8")
    assert "fetch(" not in own and "XMLHttpRequest" not in own  # (wave.js's Peaks.get is never called)
    # the three UI languages carry the same keys
    for lang in ("ja", "zh"):
        block = re.search(rf"\n  {lang}: \{{(.*?)\n  \}},", html, re.S).group(1)
        en = re.search(r"\n  en: \{(.*?)\n  \},", html, re.S).group(1)
        assert set(re.findall(r'"([\w.]+)":', block)) == set(re.findall(r'"([\w.]+)":', en)), lang


def test_export_presets_are_valid_process_audio_arguments():
    from audio_studio_mcp import web
    for name, args in chat_player.EXPORT_PRESETS.items():
        assert web.validate_arguments("process_audio", {"input_audio": "x.wav", **args}) == [], name


async def test_preview_chunks_rebuild_a_playable_file(fake, monkeypatch):
    monkeypatch.setattr(chat_player, "CHUNK_BYTES", 4096)
    async with client(True) as c:
        g = await call(c, "generate_audio", {"prompt": "rain", "audio_type": "sfx", "duration_seconds": 3,
                                             "seed": 5, "title": "rain test"})
        path = g["candidates"][0]["path"]
        first = await call(c, "audio_preview", {"path": path})
        parts, nxt = [base64.b64decode(first["data"])], first["next"]
        while not parts[-1:] or nxt < first["size"]:
            r = await call(c, "audio_preview", {"path": path, "offset": nxt})
            assert "peaks" not in r and "meta" not in r  # only the first chunk carries them
            parts.append(base64.b64decode(r["data"]))
            nxt = r["next"]
            if r["done"]:
                break
    data = b"".join(parts)
    assert len(parts) > 1 and len(data) == first["size"]
    assert first["mime"] == "audio/ogg" and data[:4] == b"OggS"
    audio, sr = sf.read(io.BytesIO(data))
    assert sr == 44100 and abs(len(audio) / sr - 3.0) < 0.01
    # far smaller than the 24-bit WAV it previews
    assert len(data) < (3 * 44100 * 2 * 3) / 4
    assert first["seconds"] == pytest.approx(3.0, abs=0.01)
    assert len(first["peaks"]["min"]) == len(first["peaks"]["max"]) == chat_player.PEAK_COLUMNS
    assert max(first["peaks"]["max"]) > 0.5
    meta = first["meta"]
    assert meta["title"] == "rain test" and meta["seed"] == 5 and meta["audio_type"] == "sfx"
    assert meta["prompt_used"] == "TrackType: SFX, rain."


async def test_preview_mp3_and_process_results(fake, tmp_path):
    async with client(True) as c:
        g = await call(c, "generate_audio", {"prompt": "loop", "duration_seconds": 2, "seed": 1})
        p = await call(c, "process_audio", {"input_audio": g["candidates"][0]["path"], "loop": True,
                                            "format": "flac", "sample_rate": 44100})
        r = await call(c, "audio_preview", {"path": p["path"], "format": "mp3"})
    assert r["mime"] == "audio/mpeg" and r["done"]
    audio, sr = sf.read(io.BytesIO(base64.b64decode(r["data"])))
    assert sr == 44100 and len(audio) / sr > 1.5
    assert r["meta"]["kind"] == "process" and any(s.startswith("loop") for s in r["meta"]["steps_done"])
    assert "steps" not in r["meta"]  # sampling steps and process steps are not mixed up


async def test_preview_only_reads_the_output_folder(fake, tmp_path):
    outside = tmp_path / "secret.wav"
    post.write(outside, sine(0.5), 44100, "wav")
    async with client(True) as c:
        await call(c, "generate_audio", {"prompt": "x", "duration_seconds": 1})
        for path in (str(outside), "../secret.wav", str(server.OUTPUT_DIR / ".." / "secret.wav"),
                     str(server.OUTPUT_DIR / "missing.wav"), "/etc/hosts"):
            r = await c.call_tool("audio_preview", {"path": path})
            assert r.is_error and "output folder" in text(r), path


async def test_preview_rejects_offsets_past_the_end(fake):
    async with client(True) as c:
        g = await call(c, "generate_audio", {"prompt": "x", "duration_seconds": 1})
        r = await c.call_tool("audio_preview", {"path": g["candidates"][0]["path"], "offset": 10 ** 9})
    assert r.is_error and "outside the preview" in text(r)


# ── clients without MCP Apps: nothing changes ───────────────────────────────
async def test_other_clients_see_the_tools_as_before():
    async with client(False) as c:
        tools = (await c.list_tools()).tools
        resources = (await c.list_resources()).resources
    assert sorted(t.name for t in tools) == CORE_TOOLS
    assert all(t.meta is None for t in tools)  # no _meta.ui
    assert resources == []
    # same definitions as the Apps view, minus the UI fields
    async with client(True) as c:
        apps_tools = {t.name: t for t in (await c.list_tools()).tools}
    for t in tools:
        a = apps_tools[t.name]
        assert (t.description, t.input_schema, t.output_schema) == (a.description, a.input_schema, a.output_schema)


async def test_other_clients_cannot_call_the_app_only_tool(fake):
    async with client(False) as c:
        g = await call(c, "generate_audio", {"prompt": "x", "duration_seconds": 1})
        r = await c.call_tool("audio_preview", {"path": g["candidates"][0]["path"]})
    assert r.is_error and text(r) == "Unknown tool: audio_preview"


async def test_results_are_the_same_text_for_every_client(fake):
    def shape(r):
        d = r.model_dump(by_alias=True, exclude_none=True)
        body = json.loads(d["content"][0]["text"])
        for cand in body.get("candidates", []):
            cand["path"] = cand["metadata"] = "<path>"
        return sorted(d), [c["type"] for c in d["content"]], d.get("structuredContent"), body

    args = {"prompt": "x", "duration_seconds": 1, "seed": 9, "variations": 2}
    async with client(False) as c:
        plain = await c.call_tool("generate_audio", args)
    async with client(True) as c:
        apps = await c.call_tool("generate_audio", args)
    a, b = shape(plain), shape(apps)
    for d in (a[3], b[3]):
        d.pop("wall_seconds"), d.pop("realtime_factor"), d.pop("included_model_load")
    assert a == b
    assert a[1] == ["text"] and a[2] is None  # one JSON text block, no structured content (as before)


@pytest.mark.parametrize("value, apps, expected", [
    ("off", True, False),   # turned off: even MCP Apps clients get text only
    ("on", False, True),    # hosts that render apps without advertising them
    ("auto", True, True), ("auto", False, False), ("bogus", False, False)])
async def test_player_mode_override(monkeypatch, value, apps, expected):
    monkeypatch.setenv("AUDIO_STUDIO_CHAT_PLAYER", value)
    async with client(apps) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        resources = (await c.list_resources()).resources
    assert ("audio_preview" in tools) is expected
    assert (tools["generate_audio"].meta is not None) is expected
    assert bool(resources) is expected


async def test_preview_chunks_carry_an_etag(fake, monkeypatch):
    """Re-encoding Ogg gives other bytes (random stream serial): every chunk says
    which encode it came from, so the player can start over after an eviction."""
    monkeypatch.setattr(chat_player, "CHUNK_BYTES", 2048)
    async with client(True) as c:
        g = await call(c, "generate_audio", {"prompt": "x", "duration_seconds": 3})
        path = g["candidates"][0]["path"]
        a = await call(c, "audio_preview", {"path": path})
        b = await call(c, "audio_preview", {"path": path, "offset": a["next"]})
        assert a["etag"] == b["etag"]
        chat_player._previews.clear()  # evicted mid-download
        c2 = await call(c, "audio_preview", {"path": path, "offset": b["next"]})
    assert c2["etag"] != a["etag"]


async def test_preview_ignores_a_sidecar_linked_out_of_the_folder(fake, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"title": "TOP SECRET", "prompt": "leak"}), encoding="utf-8")
    async with client(True) as c:
        g = await call(c, "generate_audio", {"prompt": "x", "duration_seconds": 1, "title": "linked"})
        wav = Path(g["candidates"][0]["path"])
        side = wav.with_name(wav.name + ".json")
        side.unlink()
        try:
            side.symlink_to(secret)
        except OSError:
            pytest.skip("symlinks not permitted")
        r = await call(c, "audio_preview", {"path": str(wav)})
    assert r["meta"] == {}
