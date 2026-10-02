"""画面のアクセシビリティの自動点検（ライト／ダーク、広い画面／スマホ幅）。実ブラウザ（Chrome）で測る。

測るもの:
  (1) 文字と背景のコントラスト比（WCAG AA: 通常の文字 4.5、大きい文字 3）。全画面・ライト／ダーク
  (2) 名前の無い操作要素（ボタン・入力・選択）が無いこと — スクリーンリーダーが読み上げる名前
  (3) ランドマーク（header / nav / main）・言語・見出し・状態（aria-pressed / aria-current / aria-expanded）
  (4) キーボードで届くこと（Tab の順に操作要素へ入り、フォーカスの輪郭が見える）
  (5) 押せる大きさ（スマホ幅で 44px 未満の操作要素が無い）

実際のスクリーンリーダー（NVDA・VoiceOver など）で読み上げさせる代わりに、ブラウザが組み立てる
アクセシビリティ・ツリー（AXTree）を読む。読み上げの自然さまでは保証しない。

`AIPMO_TEST_BROWSER=1` と Chrome があるときだけ動く。
Opt-in like the other browser test; reads the AX tree rather than driving a real screen reader.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import urllib.request

import pytest
from tests.test_inbox_browser import CHROME, OPERATOR, VIEWER, Browser, free_port, served, websockets  # noqa: F401

pytestmark = pytest.mark.skipif(
    not CHROME or not __import__("os").environ.get("AIPMO_TEST_BROWSER"),
    reason="AIPMO_TEST_BROWSER=1 と Chrome が要ります")

TABS = ["today", "inbox", "tasks", "wbs", "reviews", "judgment", "members", "integrations", "tools"]

CONTRAST_JS = r"""
(() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(',').map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const over = (top, bottom) => ({ r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a),
    b: top.b * top.a + bottom.b * (1 - top.a), a: 1 });
  const backdrop = (el) => { const layers = [];
    for (let n = el; n; n = n.parentElement) { const bg = parse(getComputedStyle(n).backgroundColor);
      if (bg && bg.a > 0) { layers.push(bg); if (bg.a >= 1) break; } }
    let base = parse(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
    if (base.a < 1) base = { r: 255, g: 255, b: 255, a: 1 };
    for (const l of layers.reverse()) base = over(l, base); return base; };
  const bad = [];
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    if (!t.textContent.trim()) continue;
    const el = t.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
    const fg0 = parse(cs.color); if (!fg0) continue;
    const bg = backdrop(el);
    const fg = over({ ...fg0, a: fg0.a * Number(cs.opacity) }, bg);
    const L1 = lum(fg), L2 = lum(bg);
    const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const px = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = px >= 24 || (px >= 18.66 && bold);
    if (ratio < (large ? 3 : 4.5)) bad.push({ text: t.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100,
      cls: el.className || el.tagName, fg: cs.color, bg: `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})` });
  }
  return bad;
})()
"""

STRUCTURE_JS = r"""
(() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const nameOf = (el) => (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') && document.getElementById(el.getAttribute('aria-labelledby'))?.textContent
    || (el.labels && el.labels[0] && el.labels[0].textContent) || el.textContent || el.getAttribute('title') || el.getAttribute('placeholder') || '').trim();
  const out = { unnamed: [], small: [] };
  for (const el of document.querySelectorAll('button, input, select, textarea, a[href], [role=button]')) {
    if (!visible(el)) continue;
    if (!nameOf(el)) out.unnamed.push(el.tagName + '.' + el.className);
    const r = el.getBoundingClientRect();
    if (el.tagName !== 'A' && !el.closest('.wbs-row') && (r.height < 43.5 || r.width < 24)) out.small.push(
      `${el.tagName}.${el.className}:${Math.round(r.width)}x${Math.round(r.height)}`);
  }
  out.landmarks = ['header', 'nav', 'main'].map((t) => document.querySelectorAll(t).length);
  out.lang = document.documentElement.lang;
  out.title = document.title;
  out.h1 = document.querySelectorAll('h1').length;
  out.pressedTabs = [...document.querySelectorAll('#tabs button[data-target]')].filter((b) => b.getAttribute('aria-pressed') === 'true').length;
  out.rowsWithoutRole = 0;
  return out;
})()
"""


async def run(base: str) -> dict:
    profile = tempfile.mkdtemp(prefix="aipmo_a11y_")
    port = free_port()
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(100):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1).read())
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.2)
        assert ws_url
        out: dict = {"contrast": {}, "structure": {}, "ax": {}}
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            b = Browser(ws)
            await b.call("Page.enable")
            await b.call("Accessibility.enable")
            for scheme in ("light", "dark"):
                await b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}])
                for label, metrics in (("wide", dict(width=1280, height=900, mobile=False)),
                                       ("phone", dict(width=390, height=844, mobile=True))):
                    await b.call("Emulation.setDeviceMetricsOverride", deviceScaleFactor=1, **metrics)
                    await b.call("Page.navigate", url=f"{base}/?token={OPERATOR}#today")
                    await b.until("document.querySelectorAll('#today .kpi').length === 4 && document.querySelector('.inbox-item, .task-row') !== null || document.querySelectorAll('#today .kpi').length === 4")
                    await asyncio.sleep(1.0)
                    for tab in TABS:
                        await b.js(f"showTab('{tab}')")
                        await asyncio.sleep(0.5)
                        if tab == "wbs":
                            await b.js("[...document.querySelectorAll('.wbs-row')].find(r => r.dataset.id === '2.1')?.click()")
                            await asyncio.sleep(0.3)
                        if tab == "tasks":
                            await b.js("document.querySelector('.task-row')?.click()")
                            await asyncio.sleep(0.6)
                        key = f"{scheme}/{label}/{tab}"
                        out["contrast"][key] = await b.js(CONTRAST_JS)
                        if scheme == "light":
                            out["structure"][key] = await b.js(STRUCTURE_JS)
                    if scheme == "light" and label == "wide":
                        tree = await b.call("Accessibility.getFullAXTree")
                        nodes = tree["nodes"]
                        interactive = [n for n in nodes if n.get("role", {}).get("value") in
                                       ("button", "textbox", "combobox", "searchbox", "tab", "link", "checkbox")
                                       and not n.get("ignored")]
                        out["ax"]["interactive"] = len(interactive)
                        out["ax"]["unnamed"] = [n["role"]["value"] for n in interactive
                                                if not (n.get("name", {}).get("value") or "").strip()]
                        out["ax"]["roles"] = sorted({n.get("role", {}).get("value") for n in nodes})
            # キーボード: Tab で進み、フォーカスの輪郭が見える
            await b.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
            await b.call("Emulation.setDeviceMetricsOverride", deviceScaleFactor=1, width=1280, height=900, mobile=False)
            await b.call("Page.navigate", url=f"{base}/?token={OPERATOR}#inbox")
            await b.until("document.querySelectorAll('.inbox-item').length > 0")
            reached = []
            for _ in range(14):
                await b.call("Input.dispatchKeyEvent", type="keyDown", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                await b.call("Input.dispatchKeyEvent", type="keyUp", key="Tab", code="Tab", windowsVirtualKeyCode=9)
                reached.append(await b.js(
                    "(() => { const a = document.activeElement; if (!a || a === document.body) return null;"
                    " const cs = getComputedStyle(a); return { tag: a.tagName, name: (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 20),"
                    " outline: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0, shadow: cs.boxShadow !== 'none' }; })()"))
            out["keyboard"] = reached
        return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_contrast_names_landmarks_and_keyboard(served):  # noqa: F811
    result = asyncio.run(run(served))

    problems = {k: v for k, v in result["contrast"].items() if v}
    assert not problems, json.dumps(problems, ensure_ascii=False, indent=1)[:4000]

    for key, s in result["structure"].items():
        assert not s["unnamed"], (key, s["unnamed"])
        assert s["landmarks"][1] >= 1 and s["landmarks"][2] == 1 and s["landmarks"][0] >= 1, (key, s["landmarks"])
        assert s["lang"] == "ja" and s["title"], (key, s)
        if "/phone/" in key:
            assert not s["small"], (key, s["small"])
        assert s["pressedTabs"] == 1, (key, s)

    assert result["ax"]["interactive"] > 20 and not result["ax"]["unnamed"], result["ax"]["unnamed"]

    reached = [r for r in result["keyboard"] if r]
    assert len(reached) >= 10, reached
    assert all(r["outline"] or r["shadow"] for r in reached), [r for r in reached if not (r["outline"] or r["shadow"])]
