"""受信箱を、実際のブラウザ（Chrome）で操作して確かめる。

画面の JavaScript は、サーバー側のテストでは動かない。ここでは Chrome をヘッドレスで起動し、
開発者ツールのプロトコル（CDP）で画面を直接操作する：スマホ幅で横にはみ出さないこと、
決めると一覧と件数が減ること、差し戻しは理由が無いと止まること、タブが切り替わること、
閲覧用ではボタンが出ないこと。

Chrome が無い環境や、環境変数 `AIPMO_TEST_BROWSER=1` が無いときは飛ばす（CI では既定で動かさない）。
実行例: `AIPMO_TEST_BROWSER=1 pytest tests/test_inbox_browser.py`

Drives the inbox in real headless Chrome over the DevTools protocol. Skipped unless Chrome exists and
AIPMO_TEST_BROWSER=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
CHROME = next((c for c in CANDIDATES if Path(c).exists()), None) or shutil.which("google-chrome") \
    or shutil.which("chromium")

pytestmark = pytest.mark.skipif(
    not os.environ.get("AIPMO_TEST_BROWSER") or not CHROME,
    reason="AIPMO_TEST_BROWSER=1 と Chrome が要ります / needs AIPMO_TEST_BROWSER=1 and Chrome")

websockets = pytest.importorskip("websockets")
uvicorn = pytest.importorskip("uvicorn")

from aipmo import cli, demo  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "browser-operator", "browser-viewer"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    config = cli.load_config(base / "config.yaml")
    demo.load(config, base)
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]), filing=cli._web_filing(config))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.css", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class Browser:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    async def js(self, expression):
        r = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r.get("result", {}).get("value")

    async def until(self, expression, seconds=15.0):
        """条件が真になるまで待つ（固定の待ち時間は、遅い環境で壊れる）。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if await self.js(expression):
                    return
            except RuntimeError:
                pass
            await asyncio.sleep(0.1)
        raise AssertionError(f"待っても条件が満たされませんでした: {expression}")

    async def open(self, url, **metrics):
        await self.call("Emulation.setDeviceMetricsOverride", deviceScaleFactor=1, **metrics)
        await self.call("Page.navigate", url=url)
        await self.until("document.querySelectorAll('.inbox-item').length > 0")


async def drive(base: str) -> dict:
    profile = tempfile.mkdtemp(prefix="aipmo_chrome_")
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
        assert ws_url, "Chrome に接続できません"
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            b = Browser(ws)
            await b.call("Page.enable")
            await b.call("Network.enable")
            out: dict = {}

            # スマホ幅
            await b.open(f"{base}/?token={OPERATOR}#inbox", width=390, height=844, mobile=True)
            out["mobile_scroll_width"] = await b.js("document.documentElement.scrollWidth")
            out["badge"] = await b.js("document.getElementById('inbox-count').textContent")
            out["tabs"] = await b.js("[...document.querySelectorAll('#tabs button')].map(b => b.dataset.target)")
            out["mobile_items"] = await b.js("document.querySelectorAll('.inbox-item').length")

            # デスクトップ: 選んだ項目を承認する
            await b.call("Page.navigate", url="about:blank")
            await b.open(f"{base}/?token={OPERATOR}#inbox", width=1280, height=900, mobile=False)
            out["detail_sections"] = await b.js("document.querySelectorAll('#inbox-detail .inbox-section').length")
            out["tones"] = await b.js(
                "[...document.querySelectorAll('#inbox-detail .inbox-section')].map(s => s.dataset.tone)")
            selected = await b.js("document.querySelector('.inbox-item[aria-current=true]').dataset.id")
            before = int(await b.js("document.getElementById('inbox-count').textContent"))
            await b.js("[...document.querySelectorAll('.inbox-actions button')].find(b => b.textContent.includes('承認')).click()")
            await b.until("document.getElementById('toast').textContent === '決めました'")
            await asyncio.sleep(0.3)
            ids = await b.js("[...document.querySelectorAll('.inbox-item')].map(e => e.dataset.id)")
            out["approved_gone"] = selected not in ids
            out["count"] = (before, int(await b.js("document.getElementById('inbox-count').textContent")))
            out["toast"] = await b.js("document.getElementById('toast').textContent")
            out["next_selected"] = await b.js("document.querySelector('.inbox-item[aria-current=true]') !== null")

            # 成果: 理由なしの差し戻しは止まり、理由があれば通る
            await b.js("[...document.querySelectorAll('.inbox-filters button')].find(b => b.textContent.startsWith('成果')).click()")
            await asyncio.sleep(0.4)
            await b.js("[...document.querySelectorAll('.inbox-actions button')].find(b => b.textContent.includes('差し戻す')).click()")
            await asyncio.sleep(0.6)
            out["blocked_toast"] = await b.js("document.getElementById('toast').textContent")
            out["review_listed"] = await b.js("document.querySelectorAll('.inbox-item').length")
            await b.js("document.getElementById('inbox-note').value = '確認項目が足りない'")
            await b.js("[...document.querySelectorAll('.inbox-actions button')].find(b => b.textContent.includes('差し戻す')).click()")
            await b.until("document.querySelectorAll('.inbox-item').length === 0")
            out["review_after"] = await b.js("document.querySelectorAll('.inbox-item').length")

            # タブ
            await b.js("[...document.querySelectorAll('#tabs button')].find(b => b.dataset.target === 'pmo').click()")
            await asyncio.sleep(0.5)
            out["pmo_visible"] = await b.js("!document.getElementById('pmo').hasAttribute('data-off')")
            out["inbox_hidden"] = await b.js("document.getElementById('inbox-view').hasAttribute('data-off')")
            out["hash"] = await b.js("location.hash")

            # 閲覧用
            await b.call("Network.clearBrowserCookies")
            await b.call("Page.navigate", url="about:blank")
            await b.open(f"{base}/?token={VIEWER}#inbox", width=1280, height=900, mobile=False)
            out["viewer_buttons"] = await b.js("document.querySelectorAll('.inbox-actions button').length")
            out["viewer_items"] = await b.js("document.querySelectorAll('.inbox-item').length")
            return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_inbox_works_in_a_real_browser(served):
    result = asyncio.run(drive(served))
    assert result["mobile_scroll_width"] <= 390                       # スマホ幅で横にはみ出さない
    assert result["badge"] == "14" and result["tabs"] == ["inbox", "pmo", "wbs", "templates", "runs"]
    assert result["mobile_items"] == 14
    assert result["detail_sections"] >= 4 and {"do", "dont", "info"} <= set(result["tones"])
    assert result["approved_gone"] is True and tuple(result["count"]) == (14, 13)
    assert result["toast"] == "決めました" and result["next_selected"] is True
    assert result["blocked_toast"] == "理由を書いてください" and result["review_listed"] == 1
    assert result["review_after"] == 0
    assert result["pmo_visible"] is True and result["inbox_hidden"] is True and result["hash"] == "#pmo"
    assert result["viewer_buttons"] == 0 and result["viewer_items"] > 0   # 閲覧用は見えるが、決められない
