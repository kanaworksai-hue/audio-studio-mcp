"""Browser checks of the web UI (fake backend): every language × theme renders
without untranslated keys, console errors or text below 4.5:1 contrast, and
generate → result → library → regenerate and library → editor → inpaint /
export work by clicking.

Needs Playwright and a Chrome/Chromium; skipped otherwise (CI does not install
them). Run locally:
    uv run --with playwright pytest tests/test_webui_browser.py
Screenshots go to $WEBUI_SCREENSHOTS (default: pytest's tmp dir).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

TESTS = Path(__file__).parent
LANGS = ("en", "ja", "zh")
THEMES = ("light", "dark")

# Walks every visible text node's element, finds its effective background by
# going up until an opaque background, and returns WCAG contrast failures.
CONTRAST_JS = r"""
() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const bgOf = (el) => { for (let n = el; n; n = n.parentElement) { const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c[3] >= 0.99) return c; } return parse(getComputedStyle(document.body).backgroundColor); };
  const out = []; const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode; const el = node.parentElement;
    if (!node.textContent.trim() || !el || seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === "hidden" || el.closest("[hidden]") || el.closest("option")) continue;
    if (el.closest("button:disabled, [aria-disabled=true]")) continue;  // WCAG exempts disabled controls
    const fg = parse(cs.color), bg = bgOf(el); if (!fg || !bg) continue;
    const a = fg[3]; const mix = [0, 1, 2].map((i) => fg[i] * a + bg[i] * (1 - a));
    const l1 = lum(mix), l2 = lum(bg); const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    if (ratio < 4.5) out.push({ text: node.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100, cls: el.className });
  }
  return out;
}
"""
# Any element still showing an i18n key ("gen.title") means a missing string.
KEYS_JS = r"""() => [...document.querySelectorAll("body *")].filter((n) => n.children.length === 0
  && /^[a-z]+\.[a-zA-Z.]+$/.test(n.textContent.trim()) && !n.closest("[hidden]")).map((n) => n.textContent.trim())"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    out = tmp_path_factory.mktemp("web-out")
    port = _free_port()
    env = {**os.environ, "AUDIO_STUDIO_OUTPUT_DIR": str(out), "FAKE_DELAY": "1.5", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, str(TESTS / "fake_web_server.py"), "--web", str(port), "--no-browser"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/status", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail(proc.stdout.read().decode(errors="replace"))
    yield base
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = None
        for kw in ({"channel": "chrome"}, {}):
            try:
                b = p.chromium.launch(**kw)
                break
            except Exception:
                continue
        if b is None:
            pytest.skip("no Chrome/Chromium for Playwright")
        yield b
        b.close()


@pytest.fixture(scope="module")
def shots(tmp_path_factory):
    d = os.environ.get("WEBUI_SCREENSHOTS")
    p = Path(d) if d else tmp_path_factory.mktemp("shots")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _page(browser, base, lang, theme, errors):
    ctx = browser.new_context(locale={"en": "en-US", "ja": "ja-JP", "zh": "zh-CN"}[lang],
                              color_scheme=theme, viewport={"width": 1360, "height": 900})
    page = ctx.new_page()
    # The browser logs every non-2xx fetch; a 422 is the expected answer to a bad form.
    page.on("console", lambda m: m.type == "error" and "(Unprocessable" not in m.text and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/")
    return page


def _check(page, where):
    keys = page.evaluate(KEYS_JS)
    assert not keys, f"{where}: untranslated keys {keys}"
    bad = page.evaluate(CONTRAST_JS)
    assert not bad, f"{where}: contrast below 4.5:1: {json.dumps(bad, ensure_ascii=False)}"


def test_empty_states(web, browser, shots):
    """Runs first, on an empty output folder: every page explains what to do."""
    errors: list[str] = []
    page = _page(browser, web, "en", "light", errors)
    page.wait_for_selector("#steps li")
    _check(page, "empty/generate")
    page.screenshot(path=str(shots / "empty-generate.png"), full_page=True)
    page.click("#gen-form label:has(input[name=audio_type][value=sfx])")
    page.click("#examples >> text=Glass breaking")
    assert page.input_value("#sound_source") == "glass bottle"
    page.evaluate("toast('plain message')")  # a toast without an action button
    assert page.text_content("#toast") == "plain message"
    page.click("#gen-reset")  # clear, then undo from the toast
    assert page.input_value("#sound_source") == ""
    page.click("#toast >> text=Undo")
    assert page.input_value("#sound_source") == "glass bottle"
    assert page.is_checked("#gen-form input[name=audio_type][value=sfx]")
    page.click("#tab-edit")
    page.wait_for_selector("#edit-empty:not([hidden])")
    _check(page, "empty/edit")
    page.click("#edit-to-library")
    page.wait_for_selector("#lib-empty:not([hidden])")
    _check(page, "empty/library")
    page.click("#lib-empty-btn")
    page.wait_for_selector("#page-generate:not([hidden])")
    assert not errors, errors


def test_generate_and_library_flow(web, browser, shots):
    """Click through: sfx form → queue of two jobs → candidates → library → regenerate."""
    errors: list[str] = []
    page = _page(browser, web, "en", "light", errors)
    page.click("#gen-form label:has(input[name=audio_type][value=sfx])")
    assert page.is_visible("#sound_source") and not page.is_visible("#bpm")
    page.fill("#sound_source", "glass bottle")
    page.fill("#sound_action", "shattering on impact")
    page.fill("#duration_seconds", "2")
    page.click("#gen-form label:has(input[name=variations][value='3'])")
    assert page.text_content("#gen-submit") == "Generate 3 versions"
    page.click("#gen-submit")
    page.click("#gen-form label:has(input[name=audio_type][value=music])")
    page.fill("#prompt", "calm piano")
    page.fill("#duration_seconds", "2")
    page.click("#gen-form label:has(input[name=variations][value='1'])")
    page.click("#gen-submit")
    # second job waits behind the first and can be cancelled; queue it again
    page.wait_for_selector(".job[data-status=queued] >> text=#1 in line", timeout=5000)
    page.locator(".job[data-status=queued] >> text=Cancel").click()
    page.wait_for_selector(".job[data-status=cancelled]", timeout=5000)
    page.click("#gen-submit")
    page.wait_for_selector(".job[data-status=running]", timeout=5000)
    page.wait_for_function("document.querySelectorAll('.job[data-status=done]').length === 2", timeout=15000)
    done = page.locator(".job[data-status=done]")
    assert done.first.locator(".cand").count() == 1
    assert done.nth(1).locator(".cand").count() == 3
    # audio and its waveform are served; one player at a time
    file = page.locator(".mini-wave").first.get_attribute("data-file")
    assert page.request.get(f"{web}/api/audio/{file}").status == 200
    page.wait_for_function("document.querySelectorAll('.mini-wave-canvas').length && [...document.querySelectorAll('.mini-wave-canvas')].every(c => c.width > 1)")
    page.locator(".job[data-status=done]").nth(1).locator(".play-btn").first.click()
    page.wait_for_function("document.querySelector('.play-btn.playing') !== null", timeout=5000)
    page.locator(".job[data-status=done]").nth(1).locator(".play-btn").nth(1).click()
    page.wait_for_timeout(300)
    assert page.locator(".play-btn.playing").count() == 1

    # an example chip fills the form
    page.click("#gen-form label:has(input[name=audio_type][value=sfx])")
    page.click("#examples >> text=Footsteps on gravel")
    assert page.input_value("#sound_source") == "footsteps" and page.input_value("#duration_seconds") == "5"

    # validation error is shown on the field, nothing queued
    page.click("#advanced summary")
    page.fill("#steps", "99")
    assert page.is_visible("#adv-dot")
    page.click("#gen-submit")
    page.wait_for_selector("#form-error:not([hidden])")
    assert page.get_attribute("#steps", "aria-invalid") == "true"
    page.click("#gen-reset")

    page.click("#tab-library")
    page.wait_for_selector(".item")
    assert page.locator(".item").count() == 4
    page.fill("#lib-search", "glass")
    page.wait_for_function("document.querySelectorAll('.item').length === 3")
    page.locator(".item").first.locator("text=Details").click()
    assert page.locator(".item-details").first.is_visible()
    assert "glass bottle" in page.locator(".item-details").first.text_content()
    page.locator(".item").first.locator(".menu-btn").click()
    page.locator(".item").first.locator("text=Regenerate (same seed)").click()
    page.wait_for_selector("#page-generate:not([hidden])")
    page.wait_for_function("document.querySelectorAll('#jobs .job').length === 4", timeout=5000)
    page.wait_for_function("document.querySelectorAll('.job[data-status=done]').length === 3", timeout=15000)
    assert page.input_value("#sound_source") == "glass bottle"
    page.click("#tab-library")
    page.fill("#lib-search", "nothing-matches-this")
    page.wait_for_selector("#lib-empty:not([hidden])")
    page.click("#lib-empty-btn")  # clear search
    page.wait_for_function("document.querySelectorAll('.item').length === 5")
    # re-rendering the library does not keep old thumbnails alive
    for q in ("glass", "piano", "", "glass", ""):
        page.fill("#lib-search", q)
        page.wait_for_timeout(350)
    assert page.evaluate("Player.listeners.size") <= page.evaluate("document.querySelectorAll('.mini-wave').length")
    assert not errors, errors


def _drag(page, sel, x0, x1):
    box = page.locator(sel).bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * x0, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * x1, y, steps=6)
    page.mouse.up()


def test_editor_flow(web, browser, shots):
    """Library → editor: select a range, replace it with AI, export it; zoom and keys."""
    errors: list[str] = []
    page = _page(browser, web, "en", "light", errors)
    page.fill("#prompt", "drum loop")
    page.fill("#duration_seconds", "4")
    page.click("#gen-form label:has(input[name=variations][value='1'])")
    page.wait_for_selector("#jobs .job")  # restored jobs from earlier tests
    page.click("#gen-submit")
    page.wait_for_selector("#jobs .job:first-child[data-status=done] >> text=drum loop", timeout=15000)
    page.locator("#jobs .job").first.locator("text=Edit").first.click()
    page.wait_for_selector("#page-edit:not([hidden])")
    page.wait_for_selector("#w-loading", state="hidden")
    file = page.evaluate("Editor.file")
    assert page.evaluate("location.hash") == "#edit/" + file
    assert page.input_value("#ai-prompt") == "drum loop"
    assert page.evaluate("Editor.seconds") == pytest.approx(4, abs=0.01)

    # inpaint needs a selection
    page.click("label.mode-card:has(input[value=inpaint])")
    assert page.is_disabled("#ai-submit")
    _drag(page, "#w-wave", 0.25, 0.5)
    sel = page.evaluate("Editor.sel")
    assert sel["a"] == pytest.approx(1.0, abs=0.05) and sel["b"] == pytest.approx(2.0, abs=0.05)
    assert page.input_value("#w-start") == f"{sel['a']:.2f}"
    assert page.text_content("#ai-submit").startswith("Replace 1.0")
    assert page.text_content("#ai-submit").endswith("· 2 versions")
    # the numeric fields edit the selection too
    page.fill("#w-start", "1.5")
    page.press("#w-start", "Enter")
    page.fill("#w-end", "3")
    page.press("#w-end", "Enter")
    assert page.evaluate("Editor.sel") == {"a": 1.5, "b": 3}
    page.click("#ai-submit")
    page.wait_for_selector("#edit-jobs .job[data-status=done]", timeout=15000)
    job = page.evaluate("[...state.jobs.values()].find(j => j.tool === 'edit_audio')")
    assert job["arguments"]["start_seconds"] == 1.5 and job["arguments"]["end_seconds"] == 3
    assert job["arguments"]["input_audio"].endswith(file)
    assert page.locator("#edit-jobs .cand").count() == 2

    # extend: target length, button says so
    page.click("label.mode-card:has(input[value=continue])")
    page.click("#extend-chips >> text=+5 s")
    assert page.input_value("#ai-duration") == "9"
    assert page.text_content("#ai-submit") == "Extend to 9 s · 2 versions"

    # export the selection as a game sound effect
    page.click("#st-export")
    page.click("label.preset:has(input[value=game_sfx])")
    assert page.is_checked("#x-trim-sel")
    assert "OGG 48 kHz" in page.text_content("#x-summary")
    page.click("#x-submit")
    page.wait_for_selector("#toast >> text=Saved", timeout=10000)
    exp = page.evaluate("[...state.jobs.values()].find(j => j.tool === 'process_audio')")
    assert exp["status"] == "done" and exp["result"]["format"] == "ogg" and exp["result"]["channels"] == 1
    assert exp["result"]["seconds"] <= 1.5 + 0.01
    # fades never swallow a short selection
    page.fill("#w-start", "0.2")
    page.press("#w-start", "Enter")
    page.fill("#w-end", "0.6")
    page.press("#w-end", "Enter")
    page.click("label.preset:has(input[value=video_bgm])")
    assert "shortened to fit" in page.text_content("#x-summary")
    assert page.text_content("#x-summary").startswith("Will export 0.40 s")
    page.click("#x-submit")
    page.wait_for_function("[...state.jobs.values()].filter(j => j.tool === 'process_audio' && j.status === 'done').length === 2", timeout=10000)
    vid = page.evaluate("[...state.jobs.values()].filter(j => j.tool === 'process_audio').sort((a, b) => b.created - a.created)[0]")
    assert vid["arguments"]["fade_in_ms"] == 100 and vid["arguments"]["fade_out_ms"] == 100
    assert vid["result"]["seconds"] == pytest.approx(0.4, abs=0.01)
    # changing a detail switches the preset to Custom
    page.click("#x-custom summary")
    page.click("#export-form label.check:has(input[name=mono])")
    assert page.is_checked("label.preset:has(input[value=custom]) input")

    # zoom: overview appears, finer peaks are fetched, Fit restores
    page.click("#w-zoom-sel")
    assert page.is_visible("#w-overview")
    v = page.evaluate("Editor.view")
    assert v["s"] >= 0.15 and v["e"] <= 0.65
    for _ in range(6):
        page.click("#w-zoom-in")
    page.wait_for_function("Editor.detail !== null", timeout=5000)
    page.click("#w-zoom-fit")
    assert page.evaluate("Editor.view") == {"s": 0, "e": page.evaluate("Editor.seconds")}

    # handles: arrow keys stop at the other edge instead of dropping the selection
    page.fill("#w-start", "1")
    page.press("#w-start", "Enter")
    page.fill("#w-end", "1.05")
    page.press("#w-end", "Enter")
    page.focus("#w-h-start")
    for _ in range(10):
        page.keyboard.press("ArrowRight")
    assert page.evaluate("Editor.sel") == {"a": pytest.approx(1.04), "b": pytest.approx(1.05)}
    page.keyboard.press("Home")
    assert page.evaluate("Editor.sel.a") == 0
    # an emptied number field means the tool's default, not 0
    page.click("#st-export")
    page.click("label.preset:has(input[value=game_sfx])")
    page.fill("#x-peak", "")
    page.click("#x-submit")
    page.wait_for_function("[...state.jobs.values()].filter(j => j.tool === 'process_audio' && j.status === 'done').length === 3", timeout=10000)
    last = page.evaluate("[...state.jobs.values()].filter(j => j.tool === 'process_audio').sort((a, b) => b.created - a.created)[0]")
    assert "peak_ceiling_db" not in last["arguments"] and last["result"]["peak_dbfs"] <= -0.99
    page.click("#st-ai")

    # keys: Esc clears, click moves the playhead, Space plays and pauses
    page.click("#w-wave")
    page.keyboard.press("Escape")
    assert page.evaluate("Editor.sel") is None
    _drag(page, "#w-wave", 0.6, 0.6)  # a click, not a drag
    assert page.evaluate("Editor.playhead") == pytest.approx(2.4, abs=0.05)
    page.keyboard.press("Space")
    page.wait_for_function("!Editor.audio.paused", timeout=5000)
    page.keyboard.press("Space")
    page.wait_for_function("Editor.audio.paused", timeout=5000)
    page.keyboard.press("Control+a")
    assert page.evaluate("Editor.sel")["b"] == pytest.approx(4, abs=0.01)

    assert not errors, errors
    # a missing file falls back to the empty editor (the browser logs the 404)
    # the library filters by type
    page.click("#tab-library")
    page.click("#lib-kinds >> text=Exports")
    page.wait_for_function("document.querySelectorAll('.item').length === 3")
    assert all(f.endswith(".ogg") or f.endswith(".wav") for f in page.locator(".item-file").all_text_contents())
    page.click("#lib-kinds >> text=AI edits")
    page.wait_for_function("document.querySelectorAll('.item').length === 2")
    page.click("#lib-kinds >> text=All")
    page.evaluate("location.hash = '#edit/%E0%A4%A'")  # malformed link: no exception
    page.wait_for_selector("#page-generate:not([hidden])")
    assert not errors, errors
    page.goto(web + "/#edit/missing.wav")
    page.wait_for_selector("#edit-empty:not([hidden])")
    page.wait_for_selector("#toast >> text=missing.wav is not in the output folder")
    assert page.evaluate("location.hash") == "#edit"
    assert all("404" in e for e in errors) and len(errors) == 1, errors


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("lang", LANGS)
def test_languages_and_themes(web, browser, shots, lang, theme):
    errors: list[str] = []
    page = _page(browser, web, lang, theme, errors)
    # language follows the browser, theme follows the system
    assert page.evaluate("document.documentElement.lang").startswith(lang)
    page.wait_for_selector("#model-state[data-state]")
    page.wait_for_load_state("networkidle")
    if not page.locator(".job").count():
        page.fill("#prompt", "lo-fi piano loop")
        page.fill("#duration_seconds", "2")
        page.click("#gen-form label:has(input[name=variations][value='2'])")
        page.click("#gen-submit")
    page.wait_for_selector(".job[data-status=done]", timeout=15000)
    page.click("#gen-form label:has(input[name=audio_type][value=sfx])")
    page.click("#advanced summary")
    _check(page, f"{lang}/{theme}/generate")
    page.screenshot(path=str(shots / f"generate-{lang}-{theme}.png"), full_page=True)

    page.click("#tab-library")
    page.wait_for_selector(".item")
    page.locator(".item").first.locator(".item-actions button").nth(1).click()  # Details
    _check(page, f"{lang}/{theme}/library")
    page.screenshot(path=str(shots / f"library-{lang}-{theme}.png"), full_page=True)

    page.locator(".item").first.locator(".item-actions button").first.click()  # Edit
    page.wait_for_selector("#w-loading", state="hidden")
    _drag(page, "#w-wave", 0.2, 0.45)
    page.click("label.mode-card:has(input[value=inpaint])")
    _check(page, f"{lang}/{theme}/edit")
    page.screenshot(path=str(shots / f"edit-{lang}-{theme}.png"), full_page=True)
    page.click("#st-export")
    page.click("#x-custom summary")
    _check(page, f"{lang}/{theme}/export")
    page.screenshot(path=str(shots / f"export-{lang}-{theme}.png"), full_page=True)
    assert not errors, errors


def test_manual_theme_and_language_switch(web, browser):
    errors: list[str] = []
    page = _page(browser, web, "en", "light", errors)
    # three icons: light / follow the system (default) / dark
    assert page.get_attribute("#theme [data-mode=system]", "aria-checked") == "true"
    assert page.get_attribute("#theme [data-mode=system]", "aria-label") == "Follow the system"
    page.click("#theme [data-mode=dark]")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.get_attribute("#theme [data-mode=dark]", "aria-checked") == "true"
    assert page.get_attribute("#theme [data-mode=system]", "aria-checked") == "false"
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.select_option("#lang", "ja")
    assert page.text_content("#tab-library") == "ライブラリ"
    assert page.get_attribute("#theme [data-mode=system]", "title") == "システムに合わせる"
    page.reload()  # both choices persist
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == bg
    assert page.text_content("#tab-library") == "ライブラリ"
    page.focus("#theme [data-mode=dark]")
    page.keyboard.press("ArrowLeft")  # arrow keys move within the group: dark → system
    page.select_option("#lang", "auto")
    assert page.evaluate("document.documentElement.dataset.theme") is None
    assert page.evaluate("localStorage.getItem('as.theme')") is None
    assert page.text_content("#tab-library") == "Library"
    # selects keep their arrow away from the edge
    pad = page.evaluate("parseFloat(getComputedStyle(document.querySelector('#lang')).paddingRight)")
    assert pad >= 24
    assert not errors, errors
