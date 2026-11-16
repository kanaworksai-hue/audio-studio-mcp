"""The in-chat player in a real browser, inside a minimal MCP Apps host: a
sandboxed iframe (scripts only, no same-origin) under the spec's default CSP,
speaking the ext-apps postMessage protocol, with tools/call routed to this
server in-process (fake backend). Covers generate → play → A/B → loop →
choose → export → more takes, process and error results, a host without
tool calls, and every language × theme (untranslated keys, contrast).

Needs Playwright and a Chrome/Chromium; skipped otherwise (CI does not install
them). Run locally:
    uv run --with playwright pytest tests/test_chat_player_browser.py
Screenshots go to $WEBUI_SCREENSHOTS (default: pytest's tmp dir).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from audio_studio_mcp import chat_player, server  # noqa: E402
from test_webui_browser import CONTRAST_JS, KEYS_JS  # noqa: E402

# the CSP the ext-apps spec tells hosts to apply when a resource declares none
DEFAULT_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; media-src 'self' data:; connect-src 'none'; object-src 'none'")

HOST_JS = r"""
({ srcdoc, toolName, args, result, caps, theme, locale }) => {
  window.log = { messages: [], context: [], sizes: [], calls: [], errors: [] };
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.style.cssText = "width: 720px; height: 200px; border: 1px solid #ccc; display: block";
  frame.srcdoc = srcdoc;
  document.body.append(frame);
  window.frameEl = frame;
  const post = (msg) => frame.contentWindow.postMessage({ jsonrpc: "2.0", ...msg }, "*");
  window.hostSend = post;
  window.addEventListener("message", async (ev) => {
    if (ev.source !== frame.contentWindow) return;
    const m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.method === "ui/initialize") {
      post({ id: m.id, result: { protocolVersion: "2026-01-26", hostInfo: { name: "test-host", version: "1" },
        hostCapabilities: caps, hostContext: { theme, locale, displayMode: "inline",
          toolInfo: toolName ? { tool: { name: toolName } } : undefined } } });
    } else if (m.method === "ui/notifications/initialized") {
      if (args) post({ method: "ui/notifications/tool-input", params: { arguments: args } });
      if (result) post({ method: "ui/notifications/tool-result", params: result });
    } else if (m.method === "tools/call") {
      log.calls.push(m.params);
      try { post({ id: m.id, result: await window.pyCallTool(m.params.name, m.params.arguments || {}) }); }
      catch (e) { post({ id: m.id, error: { code: -32603, message: String(e) } }); }
    } else if (m.method === "ui/message") {
      log.messages.push(m.params); post({ id: m.id, result: {} });
    } else if (m.method === "ui/update-model-context") {
      log.context.push(m.params); post({ id: m.id, result: {} });
    } else if (m.method === "ui/notifications/size-changed") {
      log.sizes.push(m.params); if (m.params.height) frame.style.height = `${m.params.height}px`;
    } else if (m.id != null && m.method) {
      post({ id: m.id, error: { code: -32601, message: "not found" } });
    }
  });
}
"""
HOST_BG = {"light": "#ffffff", "dark": "#262624"}  # Claude's chat backgrounds
FULL_CAPS = {"serverTools": {}, "message": {}, "updateModelContext": {}, "logging": {}}


class Loop:
    """One asyncio loop in a thread for the server's tool calls (Playwright's
    sync API owns the main thread)."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(120)


@pytest.fixture(scope="module")
def loop():
    lp = Loop()
    yield lp
    lp.loop.call_soon_threadsafe(lp.loop.stop)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = None
        for kw in ({"channel": "chrome"}, {}):
            try:
                b = p.chromium.launch(args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"], **kw)
                break
            except Exception:
                continue
        if b is None:
            pytest.skip("no Chrome/Chromium for Playwright")
        yield b
        b.close()


@pytest.fixture
def shots(tmp_path):
    d = Path(os.environ.get("WEBUI_SCREENSHOTS") or tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def call_tool(loop, name, args) -> dict:
    r = loop.run(server.server.call_tool(name, args))
    return r.model_dump(mode="json", by_alias=True, exclude_none=True)


def body(result: dict) -> dict:
    """The JSON a tool result carries as text (what the model reads)."""
    return json.loads(result["content"][0]["text"])


def open_player(browser, loop, *, tool="generate_audio", args=None, result=None, caps=FULL_CAPS,
                theme="light", locale="en-US", width=760):
    page = browser.new_page(viewport={"width": width, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.expose_function("pyCallTool", lambda name, a: call_tool(loop, name, a))
    page.set_content("<!doctype html><body style='margin:16px;background:#fff'></body>")
    html = chat_player.player_html().replace(
        "<head>", f'<head><meta http-equiv="Content-Security-Policy" content="{DEFAULT_CSP}">', 1)
    page.evaluate(HOST_JS, {"srcdoc": html, "toolName": tool, "args": args, "result": result,
                            "caps": caps, "theme": theme, "locale": locale})
    # the card is transparent: the host's chat background shows through
    page.evaluate(f"document.body.style.background = '{HOST_BG[theme]}'")
    page.errors = errors
    frame = page.frame_locator("iframe")
    return page, frame


def player(page):
    f = page.frames[1]
    f.wait_for_function("() => typeof UI !== 'undefined'")
    theme = f.evaluate("document.documentElement.dataset.theme")
    # contrast checks inside the iframe need the host's background behind the transparent card
    f.evaluate(f"document.documentElement.style.background = '{HOST_BG.get(theme, '#ffffff')}'")
    return f


def wait_ready(page, n):
    player(page).wait_for_function(f"() => UI.rows.length >= {n} && UI.rows.slice(0, {n}).every((r) => r.buffer)", timeout=30000)


def generate(loop, **args):
    base = {"prompt": "calm piano", "duration_seconds": 3, "seed": 11, "variations": 2, "title": "piano"}
    base.update(args)
    return base, call_tool(loop, "generate_audio", base)


def test_generate_play_ab_choose_export_more(browser, loop, fake, shots):
    args, result = generate(loop)
    page, frame = open_player(browser, loop, args=args, result=result)
    wait_ready(page, 2)
    p = player(page)
    assert p.evaluate("document.getElementById('title').textContent") == "piano"
    assert p.evaluate("document.getElementById('sub').textContent") == "calm piano"
    assert "2 versions" in p.evaluate("document.getElementById('facts').textContent")
    # the waveform is drawn from the server's peaks
    assert p.evaluate("""(() => { const c = UI.rows[0].cv, d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; })()""") > 500
    page.screenshot(path=str(shots / "chat-player-en-light.png"))

    # play version 1, then switch to version 2 at the same moment (A/B)
    frame.locator(".row").nth(0).locator(".play").click()
    p.wait_for_function("() => Engine.playing && Engine.position() > 0.4", timeout=10000)
    page.keyboard.press("2")
    p.wait_for_function("() => Engine.row === UI.rows[1] && Engine.playing")
    assert p.evaluate("Engine.position()") >= 0.4  # kept the moment instead of restarting
    assert p.evaluate("UI.rows.indexOf(Engine.row)") == 1 and p.evaluate("Engine.src.buffer === UI.rows[1].buffer")
    # loop: the source loops sample-accurately and the seam button appears
    frame.locator("#loop").click()
    assert p.evaluate("Engine.loop && Engine.src.loop") is True
    assert frame.locator("#seam").is_visible()
    frame.locator("#seam").click()
    p.wait_for_function("() => Engine.playing && Engine.loop")
    frame.locator(".play.playing").click()
    p.wait_for_function("() => !Engine.playing")

    # choose version 2 → a user message with its path goes to the chat
    keep = frame.get_by_role("button", name="Use Version 2")
    assert keep.get_attribute("title") == "Tell the assistant you picked this one"
    keep.click()
    page.wait_for_function("() => log.messages.length === 1")
    msg = page.evaluate("log.messages[0]")
    assert msg["role"] == "user" and body(result)["candidates"][1]["path"] in msg["content"][0]["text"]
    assert keep.get_attribute("aria-pressed") == "true" and "Chosen" in keep.inner_text()
    assert p.evaluate("document.activeElement === UI.rows[1].keepBtn")  # focus stays on the button

    # export version 1 as a game loop → process_audio, a playable export row below it
    frame.get_by_role("button", name="Export Version 1").click()
    items = frame.locator(".panel .preset")
    assert items.count() == 4 and "Recommended" in items.nth(0).inner_text()  # music: video first
    assert items.nth(0).inner_text().startswith("Video / YouTube")
    items.filter(has_text="Game music loop").click()
    assert frame.locator(".panel").count() == 0
    assert p.evaluate("document.activeElement === UI.rows[0].exportBtn")  # focus back on Export
    p.wait_for_function("() => UI.rows.some((r) => r.kind === 'export' && r.buffer)", timeout=30000)
    exp = p.evaluate("UI.rows[1].path")
    assert Path(exp).name.startswith("piano_v1_game_loop") and exp.endswith(".ogg") and Path(exp).is_file() and Path(exp).parent == server.OUTPUT_DIR
    assert p.evaluate("UI.rows[1].kind") == "export"
    call = page.evaluate("log.calls.find((c) => c.name === 'process_audio')")
    assert call["arguments"]["input_audio"] == body(result)["candidates"][0]["path"]
    assert call["arguments"]["loop"] is True and call["arguments"]["format"] == "ogg"
    assert "Saved" in frame.locator(".row").nth(0).locator(".note").inner_text()
    assert exp in frame.locator(".row").nth(0).locator(".note code").inner_text()  # full path, selectable
    # number keys pick versions, not the export row between them
    page.keyboard.press("2")
    assert p.evaluate("Engine.row === UI.candidates[1]")

    # two more versions: the same prompt with the next seeds, appended as versions 3 and 4
    frame.get_by_role("button", name="2 more versions").click()
    p.wait_for_function("() => UI.candidates.length === 4 && UI.candidates.every((r) => r.buffer)", timeout=30000)
    more = page.evaluate("log.calls.filter((c) => c.name === 'generate_audio').pop().arguments")
    assert more["prompt"] == "calm piano" and more["variations"] == 2 and more["seed"] == 13
    labels = p.evaluate("UI.rows.map((r) => r.label)")
    assert labels[:2] == ["Version 1", "Export · Game music loop"] and labels[-2:] == ["Version 3", "Version 4"]
    ctx = page.evaluate("log.context[log.context.length - 1].content[0].text")
    assert "chose version 2 (seed 12)" in ctx and 'exported version 1 with the "game_loop" preset' in ctx
    assert "made 2 more versions" in ctx and "version 4 seed 14" in ctx
    # the iframe told the host its height, and nothing errored
    assert page.evaluate("log.sizes.length") > 0
    assert page.evaluate("frameEl.getBoundingClientRect().height") >= p.evaluate("document.documentElement.scrollHeight") - 2
    page.screenshot(path=str(shots / "chat-player-en-light-after.png"))
    assert not page.errors, page.errors
    page.close()


def test_process_result_starts_looped(browser, loop, fake):
    _, g = generate(loop, variations=1)
    pargs = {"input_audio": body(g)["candidates"][0]["path"], "loop": True, "format": "ogg", "sample_rate": 48000}
    result = call_tool(loop, "process_audio", pargs)
    page, frame = open_player(browser, loop, tool="process_audio", args=pargs, result=result)
    wait_ready(page, 1)
    p = player(page)
    assert p.evaluate("Engine.loop") is True
    assert frame.locator("#loop").get_attribute("aria-pressed") == "true"
    assert frame.locator(".row .actions button").count() == 0  # already exported: no choose/export
    assert frame.locator("#more").is_hidden()
    assert frame.locator("#seam").is_visible()  # loop on → the seam button shows
    assert "OGG 48 kHz" in p.evaluate("document.getElementById('facts').textContent")
    assert not page.errors, page.errors
    page.close()


def test_replayed_result_without_input_or_tool_name(browser, loop, fake):
    """After a chat reload hosts may replay only the result: the player still
    works, and "more takes" rebuilds the call from the sidecar."""
    _, result = generate(loop, variations=1, audio_type="instrument", bpm=90)
    page, frame = open_player(browser, loop, tool=None, args=None, result=result)
    wait_ready(page, 1)
    p = player(page)
    assert p.evaluate("UI.toolName()") == "generate_audio"
    assert p.evaluate("document.getElementById('title').textContent") == "piano"
    assert p.evaluate("Engine.loop") is True  # an instrument loop starts looped
    frame.get_by_role("button", name="2 more versions").click()
    p.wait_for_function("() => UI.candidates.length === 3", timeout=30000)
    more = page.evaluate("log.calls.filter((c) => c.name === 'generate_audio').pop().arguments")
    assert more == {"prompt": "calm piano", "audio_type": "instrument", "duration_seconds": 3.0, "variations": 2,
                    "cfg": 1.0, "steps": 8, "bpm": 90, "title": "piano", "seed": 12}
    assert not page.errors, page.errors
    page.close()


def test_error_result_and_host_without_tool_calls(browser, loop, fake):
    page, frame = open_player(browser, loop, args={"prompt": ""}, result={
        "content": [{"type": "text", "text": "prompt is empty"}], "isError": True})
    player(page).wait_for_function("() => document.getElementById('status').classList.contains('error')")
    assert "prompt is empty" in frame.locator("#status").inner_text()
    page.close()

    args, result = generate(loop)
    page, frame = open_player(browser, loop, args=args, result=result, caps={"message": {}})
    frame.locator(".paths li").first.wait_for()
    assert frame.locator(".paths li").all_inner_texts() == [c["path"] for c in body(result)["candidates"]]
    assert page.evaluate("log.calls.length") == 0
    page.close()


# Claude Desktop (Windows, 2026-11) adds a note for the model to the result it gives the app
HOST_NOTE = ("[This tool call rendered an interactive widget in the chat. The user can already see the "
             "result — do not repeat it in text or call a visualization tool.]")


@pytest.mark.parametrize("where", ["same-block", "own-block", "before"])
def test_result_with_a_host_note(browser, loop, fake, where):
    args, result = generate(loop)
    result = json.loads(json.dumps(result))
    if where == "own-block":
        result["content"].append({"type": "text", "text": HOST_NOTE})
    elif where == "same-block":
        result["content"][0]["text"] += HOST_NOTE
    else:  # a note with braces in front of the JSON
        result["content"][0]["text"] = "[note: {not json}] " + result["content"][0]["text"]
    page, frame = open_player(browser, loop, args=args, result=result)
    wait_ready(page, 2)
    assert not player(page).evaluate("document.getElementById('status').classList.contains('error')")
    assert not page.errors, page.errors
    page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("locale", ["en-US", "ja-JP", "zh-CN"])
def test_languages_and_themes(browser, loop, fake, shots, locale, theme):
    args, result = generate(loop, title=None, audio_type="sfx", prompt="glass bottle shattering")
    page, frame = open_player(browser, loop, args=args, result=result, theme=theme, locale=locale)
    page.evaluate(f"document.body.style.background = '{'#262624' if theme == 'dark' else '#ffffff'}'")
    wait_ready(page, 2)
    p = player(page)
    lang = locale[:2]
    assert p.evaluate("document.documentElement.lang").startswith(lang)
    assert p.evaluate("document.documentElement.dataset.theme") == theme
    title = p.evaluate("document.getElementById('title').textContent")
    assert title == {"en": "Sound effect", "ja": "効果音", "zh": "音效"}[lang]
    frame.locator(".row").nth(0).locator(".actions .btn").nth(1).click()  # open the export menu
    assert p.evaluate(KEYS_JS) == []
    assert p.evaluate(CONTRAST_JS) == []
    page.wait_for_function("() => Math.abs(frameEl.clientHeight - log.sizes[log.sizes.length - 1].height) < 2")
    page.wait_for_timeout(100)
    page.screenshot(path=str(shots / f"chat-player-{lang}-{theme}.png"))
    assert not page.errors, page.errors
    page.close()


def test_narrow_layout_stacks(browser, loop, fake, shots):
    args, result = generate(loop)
    page, frame = open_player(browser, loop, args=args, result=result, width=420)
    page.evaluate("frameEl.style.width = '380px'")
    wait_ready(page, 2)
    p = player(page)
    over = p.evaluate("""[...document.querySelectorAll('.row *')].filter((n) => {
        const r = n.getBoundingClientRect(); return r.width && r.right > document.documentElement.clientWidth + 1; }).length""")
    assert over == 0
    page.screenshot(path=str(shots / "chat-player-narrow.png"))
    page.close()


def test_player_html_matches_resource(loop):
    async def read():
        from mcp import Client
        from test_chat_player import AppsClient
        async with Client(server.server, extensions=[AppsClient()]) as c:
            return (await c.read_resource(chat_player.RESOURCE_URI)).contents[0].text
    assert loop.run(read()) == chat_player.player_html()
    assert json.loads(json.dumps(chat_player.EXPORT_PRESETS)) == chat_player.EXPORT_PRESETS


def test_review_regressions(browser, loop, fake):
    """Seeking another take while one plays, the hidden seam button, locale
    changes from the host, teardown, and a replayed edit result."""
    args, result = generate(loop)
    page, frame = open_player(browser, loop, args=args, result=result)
    wait_ready(page, 2)
    p = player(page)
    assert frame.locator("#seam").is_hidden()  # loop off: no seam button ([hidden] must win over display)
    frame.locator(".row").nth(0).locator(".play").click()
    p.wait_for_function("() => Engine.playing && Engine.position() > 0.3")
    frame.locator(".row").nth(1).locator(".track").focus()
    page.keyboard.press("ArrowRight")
    p.wait_for_function("() => Engine.row === UI.rows[1] && Engine.playing")
    assert p.evaluate("Engine.src.buffer === UI.rows[1].buffer")  # the audio heard is the take shown
    # the host switches to Japanese: labels follow
    page.evaluate("hostSend({ method: 'ui/notifications/host-context-changed', params: { locale: 'ja-JP' } })")
    p.wait_for_function("() => document.documentElement.lang === 'ja'")
    assert p.evaluate("UI.rows.map((r) => r.label)") == ["バージョン 1", "バージョン 2"]
    assert frame.locator("#more").inner_text() == "あと 2 バージョン作る"
    # teardown: answered, audio stopped
    page.evaluate("hostSend({ id: 99, method: 'ui/resource-teardown', params: {} })")
    p.wait_for_function("() => !Engine.playing && Engine.ac === null")
    assert not page.errors, page.errors
    page.close()

    # an edit replayed without its input: "more" has nothing to repeat, so it is hidden
    src = body(result)["candidates"][0]["path"]
    edit = call_tool(loop, "edit_audio", {"input_audio": src, "mode": "variation", "prompt": "brighter", "seed": 3})
    page, frame = open_player(browser, loop, tool="edit_audio", args=None, result=edit)
    wait_ready(page, 1)
    assert frame.locator("#more").is_hidden()
    page.close()
    # with its input: the original comes first as "Before edit" (key 0)
    page, frame = open_player(browser, loop, tool="edit_audio",
                              args={"input_audio": src, "mode": "variation", "prompt": "brighter"}, result=edit)
    wait_ready(page, 2)
    p = player(page)
    assert p.evaluate("UI.rows.map((r) => [r.kind, r.label, r.keyNo])") == [
        ["original", "Before edit", "0"], ["cand", "Result", ""]]
    assert frame.locator("#more").is_visible()
    page.close()
