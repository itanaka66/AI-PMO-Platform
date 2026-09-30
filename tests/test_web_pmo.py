"""Web 画面の PMO Core 表示のテスト / PMO Core web view tests.

画面は読むだけ。書けるのは担当の確定だけで、それは operator に限る。
The screen reads; its single write is confirming an assignment, operator only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aipmo.adapters.base import Adapter, AdapterRegistry, action  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import Member, PmoCore  # noqa: E402
from aipmo.task_engine import TaskEngine  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "operator-token-1", "viewer-token-2"


class FakeJira(Adapter):
    name = "jira"

    def __init__(self, unresolved: bool = False) -> None:
        super().__init__()
        self.updates: list[dict[str, Any]] = []
        self.unresolved = unresolved

    @action(writes=True)
    def update_issue(self, issue_key: str, assignee: str | None = None) -> dict[str, Any]:
        self.updates.append({"issue_key": issue_key, "assignee": assignee})
        return {"issue_key": issue_key,
                **({"unresolved_assignee": assignee} if self.unresolved else {})}


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": []}
    return {**base, **kw}


def make_client(tmp_path: Path, jira: FakeJira | None = None, with_ledger: bool = True):
    ledger = tmp_path / "task-ledger.json"
    if with_ledger:
        te = TaskEngine(ledger)
        te.ingest("t", "r", [
            cand(key="P-1", title="遅延している課題", priority="High",
                 due_date="2026-09-01", assignee="ann"),
            cand(key="P-2", title="担当未定の課題", priority="Medium"),
        ])
        PmoCore(task_engine=te, members=[Member("ann"), Member("bob")]).cycle()

    adapters = AdapterRegistry()
    if jira is not None:
        adapters.register(jira)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    app = create_app(Engine(adapters, llms), templates, OPERATOR,
                     viewer_token=VIEWER, lang="en", store=RunStore(),
                     pmo_ledger=ledger)
    return TestClient(app), ledger


def op():
    return {"x-aipmo-token": OPERATOR}


def viewer():
    return {"x-aipmo-token": VIEWER}


def test_view_needs_a_token_and_viewer_may_read(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/api/pmo").status_code == 401
    assert client.get("/api/pmo/decisions").status_code == 401
    body = client.get("/api/pmo", headers=viewer()).json()
    assert body["briefing"]["overall_level"] == "critical"
    assert [t["key"] for t in body["tasks"]][0] == "P-1"        # ranked
    assert body["briefing_age_seconds"] is not None
    assert body["tasks"][1]["suggested_assignee"] in {"ann", "bob"}


def test_decisions_are_newest_first_and_limited(tmp_path):
    client, _ = make_client(tmp_path)
    items = client.get("/api/pmo/decisions?limit=1", headers=viewer()).json()["items"]
    assert len(items) == 1 and "kind" in items[0]
    everything = client.get("/api/pmo/decisions", headers=viewer()).json()["items"]
    assert everything[0]["at"] >= everything[-1]["at"]


def test_no_data_is_a_404_not_an_error_page(tmp_path):
    client, _ = make_client(tmp_path, with_ledger=False)
    assert client.get("/api/pmo", headers=viewer()).status_code == 404


def test_unconfigured_server_has_no_pmo_endpoints_data(tmp_path):
    adapters = AdapterRegistry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir()
    app = create_app(Engine(adapters, llms), tmp_path / "t", OPERATOR, lang="en")
    assert TestClient(app).get("/api/pmo", headers=op()).status_code == 404


def test_a_stale_briefing_reports_its_age(tmp_path):
    client, ledger = make_client(tmp_path)
    briefing = ledger.parent / "pmo-briefing.json"
    data = json.loads(briefing.read_text(encoding="utf-8"))
    data["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    briefing.write_text(json.dumps(data), encoding="utf-8")
    age = client.get("/api/pmo", headers=viewer()).json()["briefing_age_seconds"]
    assert age >= 7000


def test_viewer_cannot_confirm_an_assignment(tmp_path):
    client, ledger = make_client(tmp_path)
    response = client.post("/api/pmo/assignments/accept", json={"ref": "P-2"},
                           headers=viewer())
    assert response.status_code == 403
    assert TaskEngine(ledger).tasks["JIRA:P-2"].assignee is None


def test_operator_confirms_and_the_ledger_changes(tmp_path):
    client, ledger = make_client(tmp_path)
    response = client.post("/api/pmo/assignments/accept", json={"ref": "P-2"},
                           headers=op())
    assert response.status_code == 200 and response.json()["jira_updated"] is False
    task = TaskEngine(ledger).tasks["JIRA:P-2"]
    assert task.assignee == response.json()["assignee"] and task.suggested_assignee is None
    log = (ledger.parent / "pmo-decisions.jsonl").read_text(encoding="utf-8")
    assert "assignment_accepted" in log


def test_confirming_can_update_jira_and_a_jira_failure_leaves_the_ledger(tmp_path):
    jira = FakeJira()
    client, ledger = make_client(tmp_path, jira)
    ok = client.post("/api/pmo/assignments/accept", json={"ref": "P-2", "jira": True},
                     headers=op())
    assert ok.status_code == 200 and ok.json()["jira_updated"] is True
    assert jira.updates and jira.updates[0]["issue_key"] == "P-2"

    bad_jira = FakeJira(unresolved=True)
    client, ledger = make_client(tmp_path / "b", bad_jira)
    failed = client.post("/api/pmo/assignments/accept", json={"ref": "P-2", "jira": True},
                         headers=op())
    assert failed.status_code == 502
    assert TaskEngine(ledger).tasks["JIRA:P-2"].assignee is None


def test_accept_errors_are_reported_precisely(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.post("/api/pmo/assignments/accept", json={}, headers=op()).status_code == 422
    assert client.post("/api/pmo/assignments/accept", json={"ref": "NOPE-9"},
                       headers=op()).status_code == 404
    # P-1 already has an assignee -> no proposal to confirm
    assert client.post("/api/pmo/assignments/accept", json={"ref": "P-1"},
                       headers=op()).status_code == 409
    assert client.post("/api/pmo/assignments/accept", json={"ref": "P-2", "jira": True},
                       headers=op()).status_code == 503        # no jira adapter


def test_scheduler_keeps_an_assignment_confirmed_by_another_process(tmp_path):
    """常駐側が古いメモリの台帳を保存しても、Web で確定した担当が消えないこと。"""
    client, ledger = make_client(tmp_path)
    scheduler_view = TaskEngine(ledger)          # 常駐側が読んだ時点の台帳
    client.post("/api/pmo/assignments/accept", json={"ref": "P-2"}, headers=op())
    scheduler_view.refresh()                      # 常駐側の次の周
    assert TaskEngine(ledger).tasks["JIRA:P-2"].assignee is not None
    assert scheduler_view.tasks["JIRA:P-2"].assignee is not None


def test_static_ui_is_wired_safely():
    static = ROOT / "aipmo" / "web" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert 'id="h-pmo"' in html and 'id="pmo"' in html
    for needle in ("refreshPmo", "renderPmo", "/api/pmo", "/api/pmo/assignments/accept"):
        assert needle in js
    # 外部由来の文字列を HTML として解釈しない / never parse external text as HTML
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    # 確定ボタンは operator にだけ出す（権限制御そのものはサーバー側）
    assert re.search(r"if\s*\(canRun\)\s*\{[^}]*web_pmo_accept", js, re.S)
