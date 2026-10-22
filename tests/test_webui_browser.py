"""Browser checks of the web UI (fake backend): every language × theme renders
without untranslated keys, console errors or text below 4.5:1 contrast, and
generate → result → library → regenerate works by clicking.

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


def test_generate_and_library_flow(web, browser, shots):
    """Click through: sfx form → queue of two jobs → candidates → library → regenerate."""
    errors: list[str] = []
    page = _page(browser, web, "en", "light", errors)
    page.click("label:has(input[name=audio_type][value=sfx])")
    assert page.is_visible("#sound_source") and not page.is_visible("#bpm")
    page.fill("#sound_source", "glass bottle")
    page.fill("#sound_action", "shattering on impact")
    page.fill("#duration_seconds", "2")
    page.click("label:has(input[name=variations][value='3'])")
    page.click("#gen-submit")
    page.click("label:has(input[name=audio_type][value=music])")
    page.fill("#prompt", "calm piano")
    page.fill("#duration_seconds", "2")
    page.click("label:has(input[name=variations][value='1'])")
    page.click("#gen-submit")
    # second job waits behind the first
    page.wait_for_selector(".job[data-status=queued] >> text=#1 in queue", timeout=5000)
    page.wait_for_selector(".job[data-status=running]", timeout=5000)
    page.wait_for_function("document.querySelectorAll('.job[data-status=done]').length === 2", timeout=15000)
    assert page.locator(".job").first.locator(".cand").count() == 1
    assert page.locator(".job").nth(1).locator(".cand").count() == 3
    # audio is served
    src = page.locator(".cand audio").first.get_attribute("src")
    assert page.request.get(web + src).status == 200

    # validation error is shown on the field, nothing queued
    page.click("#advanced summary")
    page.fill("#steps", "99")
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
    page.locator(".item").first.locator("text=Regenerate (same seed)").click()
    page.wait_for_selector("#page-generate:not([hidden])")
    page.wait_for_function("document.querySelectorAll('.job').length === 3", timeout=5000)
    page.wait_for_function("document.querySelectorAll('.job[data-status=done]').length === 3", timeout=15000)
    assert page.input_value("#sound_source") == "glass bottle"
    assert not errors, errors


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
        page.click("label:has(input[name=variations][value='2'])")
        page.click("#gen-submit")
    page.wait_for_selector(".job[data-status=done]", timeout=15000)
    page.click("label:has(input[name=audio_type][value=sfx])")
    page.click("#advanced summary")
    _check(page, f"{lang}/{theme}/generate")
    page.screenshot(path=str(shots / f"generate-{lang}-{theme}.png"), full_page=True)

    page.click("#tab-library")
    page.wait_for_selector(".item")
    page.locator(".item").first.locator(".item-actions button").first.click()
    _check(page, f"{lang}/{theme}/library")
    page.screenshot(path=str(shots / f"library-{lang}-{theme}.png"), full_page=True)
    assert not errors, errors


def test_manual_theme_and_language_switch(web, browser):
    errors: list[str] = []
    page = _page(browser, web, "en", "light", errors)
    page.select_option("#theme", "dark")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.select_option("#lang", "ja")
    assert page.text_content("#tab-library") == "ライブラリ"
    page.reload()  # both choices persist
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == bg
    assert page.text_content("#tab-library") == "ライブラリ"
    page.select_option("#theme", "system")
    page.select_option("#lang", "auto")
    assert page.evaluate("document.documentElement.dataset.theme") is None
    assert page.text_content("#tab-library") == "Library"
    assert not errors, errors
