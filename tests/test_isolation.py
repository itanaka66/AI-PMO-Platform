"""テナント・プロジェクトの分離のテスト / tenant and project isolation tests.

見せてはいけないものが見えないこと、混ぜてはいけないものが混ざらないこと。
That what must not be visible is not, and what must not mix does not.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aipmo import cli  # noqa: E402
from aipmo.adapters.base import AdapterRegistry  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import (  # noqa: E402
    Member,
    PmoCore,
    load_members,
    scope_briefing,
    suggest_assignee,
)
from aipmo.task_engine import (  # noqa: E402
    LedgerTenantError,
    Task,
    TaskEngine,
    project_of_key,
    side_path,
)
from aipmo.web.server import RunStore, create_app  # noqa: E402

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
OPERATOR, VIEWER = "operator-token-1", "viewer-token-2"


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": []}
    return {**base, **kw}


def engine(path, tenant=None):
    return TaskEngine(path, now=lambda: NOW, tenant=tenant)


# ===== テナント / tenant =======================================================

def test_a_ledger_belongs_to_the_first_tenant_to_open_it(tmp_path):
    engine(tmp_path / "task-ledger.db", "acme").ingest("t", "r", [cand(key="A-1")])
    engine(tmp_path / "task-ledger.db", "acme")               # 同じ持ち主は開ける
    with pytest.raises(LedgerTenantError, match="acme"):
        engine(tmp_path / "task-ledger.db", "globex")
    # 拒否されても、データは読まれていない・変わっていない
    assert list(engine(tmp_path / "task-ledger.db", "acme").tasks) == ["JIRA:A-1"]


def test_no_tenant_means_no_guard_and_an_unstamped_ledger_is_adopted(tmp_path):
    engine(tmp_path / "a.db").ingest("t", "r", [cand(key="A-1")])        # 刻印なし
    engine(tmp_path / "a.db", None)
    assert len(engine(tmp_path / "a.db", "acme").tasks) == 1           # 最初の持ち主に
    with pytest.raises(LedgerTenantError):
        engine(tmp_path / "a.db", "globex")


def test_a_legacy_json_ledger_is_stamped_on_import(tmp_path):
    legacy = tmp_path / "task-ledger.json"
    legacy.write_text(json.dumps({"tasks": [], "outcomes": []}), encoding="utf-8")
    engine(tmp_path / "task-ledger.db", "acme")
    with pytest.raises(LedgerTenantError):
        engine(tmp_path / "task-ledger.db", "globex")


def test_two_tenants_in_one_directory_do_not_share_side_files(tmp_path):
    assert side_path(tmp_path / "task-ledger.db", "pmo-briefing.json").name \
        == "pmo-briefing.json"                                  # 既定は従来の名前
    assert side_path(tmp_path / "acme.db", "pmo-briefing.json").name \
        == "acme.pmo-briefing.json"
    for name in ("acme", "globex"):
        te = engine(tmp_path / f"{name}.db", name)
        te.ingest("t", "r", [cand(key="A-1", assignee="x", due_date="2026-09-01")])
        PmoCore(task_engine=te).cycle()
    assert json.loads((tmp_path / "acme.pmo-briefing.json").read_text("utf-8"))
    assert (tmp_path / "globex.pmo-decisions.jsonl").exists()
    assert not (tmp_path / "pmo-briefing.json").exists()


# ===== プロジェクト / project ==================================================

def test_project_comes_from_the_key_an_explicit_field_or_the_run_params(tmp_path):
    assert project_of_key("PROJ-123") == "PROJ" and project_of_key("#12") == ""
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r", [cand(key="PROJ-1", title="a"),
                         cand(key="X-9", title="b", project="Explicit"),
                         cand(title="keyless")], project="FromParams")
    assert te.tasks["JIRA:PROJ-1"].project == "PROJ"
    assert te.tasks["JIRA:X-9"].project == "Explicit"
    assert next(t for t in te.tasks.values() if t.title == "keyless").project == "FromParams"


def test_run_parameters_supply_the_default_project(tmp_path):
    te = engine(tmp_path / "l.db")
    result = SimpleNamespace(status="success",
                             output={"items": [{"summary": "議事録を共有する"}]})
    ctx = SimpleNamespace(run_id="r1", results={"s": result},
                          params={"jira_project": "ALPHA"})
    te.on_run_complete("tpl", ctx)
    (task,) = te.tasks.values()
    assert task.project == "ALPHA"


def test_same_titled_keyless_tasks_in_different_projects_stay_separate(tmp_path):
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r1", [cand(title="議事録を共有する")], project="ALPHA")
    te.ingest("t", "r2", [cand(title="議事録を共有する")], project="BETA")
    assert sorted(t.project for t in te.tasks.values()) == ["ALPHA", "BETA"]
    te.ingest("t", "r3", [cand(title="議事録を共有する")], project="ALPHA")   # 同じものは1件
    assert len(te.tasks) == 2


def test_a_task_seen_before_projects_existed_is_adopted_not_duplicated(tmp_path):
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r1", [cand(title="古いタスク")])                  # project なし
    te.ingest("t", "r2", [cand(title="古いタスク")], project="ALPHA")
    (task,) = te.tasks.values()
    assert task.project == "ALPHA"
    te.ingest("t", "r3", [cand(key="ALPHA-5", title="古いタスク")])   # キーも後から
    (task,) = te.tasks.values()
    assert task.key == "ALPHA-5" and task.id == "JIRA:ALPHA-5"


def test_rows_written_before_projects_existed_get_one_from_their_key(tmp_path):
    te = engine(tmp_path / "l.db")
    with te.transaction():
        te.tasks["JIRA:OLD-1"] = Task(id="JIRA:OLD-1", key="OLD-1", title="old",
                                      first_seen=NOW.isoformat(), last_seen=NOW.isoformat())
    # project を持たない古い形式の行を直接書く
    import sqlite3
    raw = sqlite3.connect(te.path)
    data = json.loads(raw.execute("SELECT data FROM tasks").fetchone()[0])
    data.pop("project")
    raw.execute("UPDATE tasks SET data = ?", (json.dumps(data),))
    raw.commit()
    raw.close()
    assert engine(tmp_path / "l.db").tasks["JIRA:OLD-1"].project == "OLD"


def test_ranking_can_be_limited_to_projects_and_unprojected_tasks_are_hidden(tmp_path):
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r", [cand(key="A-1", title="a"), cand(key="B-1", title="b"),
                         cand(title="no project")])
    assert [t.key for t in te.ranked(project="a")] == ["A-1"]          # 大小文字は無視
    assert sorted(t.key for t in te.ranked(projects=["A", "B"])) == ["A-1", "B-1"]
    assert te.ranked(projects=[]) == []
    assert te.ranked(project="A", projects=["B"]) == []                # 共通部分
    assert te.projects() == ["A", "B"]
    assert len(te.ranked()) == 3


# ===== 担当割当 / assignment ====================================================

def test_a_member_limited_to_projects_is_never_offered_other_work():
    alpha = Task(id="1", title="t", project="ALPHA")
    beta = Task(id="2", title="t", project="BETA")
    bare = Task(id="3", title="t")
    members = [Member("ann", projects=("alpha",)), Member("bob", projects=("beta",)),
               Member("cat")]
    loads = {m.name: 0 for m in members}
    assert suggest_assignee(alpha, members, loads)[0].name == "ann"
    assert suggest_assignee(beta, members, loads)[0].name == "bob"
    assert suggest_assignee(bare, members, loads)[0].name == "cat"     # 制限の無い人だけ
    assert suggest_assignee(bare, members[:2], loads) is None
    assert load_members([{"name": "x", "projects": ["Alpha"]}])[0].projects == ("alpha",)


# ===== ブリーフィング / briefing ================================================

def seeded(tmp_path, tenant="acme"):
    te = engine(tmp_path / "task-ledger.db", tenant)
    te.ingest("t", "r", [
        cand(key="A-1", title="alpha late", assignee="x", priority="High",
             due_date="2026-09-01"),
        cand(key="A-2", title="alpha open"),
        cand(key="B-1", title="beta fine", assignee="y", due_date="2099-01-01"),
    ])
    core = PmoCore(task_engine=te, members=[
        Member("ann", projects=("a",)), Member("bob", projects=("b",))])
    return te, core, core.cycle()


def test_the_briefing_summarises_each_project(tmp_path):
    _, _, briefing = seeded(tmp_path)
    summary = {p["project"]: p for p in briefing["projects"]}
    assert summary["A"]["level"] == "critical" and summary["B"]["level"] == "low"
    assert summary["A"]["active_count"] == 2
    assert all("project" in a for a in briefing["alerts"])
    assert {p["assignee"] for p in briefing["assignment_proposals"]
            if p["project"] == "A"} == {"ann"}


def test_scoping_rebuilds_the_level_and_redacts_the_organisation(tmp_path):
    te, _, briefing = seeded(tmp_path)
    scoped = scope_briefing(briefing, te.ranked(projects=["b"]), {"b"}, redact_org=True)
    assert scoped["overall_level"] == "low" and scoped["alerts"] == []
    assert scoped["active_count"] == 1
    assert [p["project"] for p in scoped["projects"]] == ["B"]
    assert scoped["member_loads"] == [] and scoped["learning"] is None
    assert scoped["responses"] == []
    open_ = scope_briefing(briefing, te.ranked(projects=["b"]), {"b"}, redact_org=False)
    assert open_["member_loads"]                     # 操作者の絞り込みは隠さない


# ===== CLI ======================================================================

def test_cli_filters_by_project_and_refuses_another_tenants_ledger(tmp_path, capsys):
    te, _, _ = seeded(tmp_path)
    te.close()
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\npmo_core:\n  members: [ann]\n", encoding="utf-8")

    assert cli.main(["--config", str(config), "tasks", "--project", "B"]) == 0
    out = capsys.readouterr().out
    assert "beta fine" in out and "alpha" not in out

    assert cli.main(["--config", str(config), "pmo", "--project", "A"]) == 0
    out = capsys.readouterr().out
    assert "alpha late" in out and "beta fine" not in out

    config.write_text("tenant: globex\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "tasks"]) == 1
    err = capsys.readouterr().err
    assert "acme" in err and "globex" in err


# ===== Web ======================================================================

def client_for(tmp_path, viewer_projects=None, tenant="acme"):
    seeded(tmp_path)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", OPERATOR,
                     viewer_token=VIEWER, lang="en", store=RunStore(), tenant=tenant,
                     pmo_ledger=tmp_path / "task-ledger.db",
                     viewer_projects=viewer_projects)
    return TestClient(app)


def op():
    return {"x-aipmo-token": OPERATOR}


def viewer():
    return {"x-aipmo-token": VIEWER}


def test_a_confined_viewer_sees_only_its_projects(tmp_path):
    client = client_for(tmp_path, viewer_projects=["B"])
    body = client.get("/api/pmo", headers=viewer()).json()
    assert [t["key"] for t in body["tasks"]] == ["B-1"]
    assert body["projects"] == ["B"] and body["scoped"] is True
    assert body["briefing"]["overall_level"] == "low"
    assert body["briefing"]["alerts"] == []
    assert body["briefing"]["member_loads"] == []       # 組織の情報は出さない
    assert "alpha" not in json.dumps(body)


def test_a_confined_viewer_cannot_name_another_project(tmp_path):
    client = client_for(tmp_path, viewer_projects=["B"])
    assert client.get("/api/pmo?project=A", headers=viewer()).status_code == 403
    assert client.get("/api/pmo/decisions?project=A", headers=viewer()).status_code == 403
    assert client.get("/api/pmo?project=b", headers=viewer()).status_code == 200


def test_decisions_are_limited_to_the_visible_projects(tmp_path):
    client = client_for(tmp_path, viewer_projects=["B"])
    everything = client.get("/api/pmo/decisions", headers=op()).json()["items"]
    assert any(d.get("task") == "JIRA:A-1" for d in everything)
    seen = client.get("/api/pmo/decisions", headers=viewer()).json()["items"]
    assert all(d.get("task") == "JIRA:B-1" for d in seen)
    assert not any("model_updated" == d["kind"] for d in seen)


def test_the_operator_sees_everything_and_may_filter(tmp_path):
    client = client_for(tmp_path, viewer_projects=["B"])
    everything = client.get("/api/pmo", headers=op()).json()
    assert {t["key"] for t in everything["tasks"]} == {"A-1", "A-2", "B-1"}
    assert everything["projects"] == ["A", "B"] and everything["scoped"] is False
    assert everything["briefing"]["member_loads"]
    only_a = client.get("/api/pmo?project=A", headers=op()).json()
    assert {t["key"] for t in only_a["tasks"]} == {"A-1", "A-2"}
    assert only_a["briefing"]["overall_level"] == "critical"


def test_unset_or_empty_viewer_projects_means_no_limit(tmp_path):
    for setting in (None, []):
        client = client_for(tmp_path / ("n" if setting is None else "e"), setting) \
            if (tmp_path / ("n" if setting is None else "e")).mkdir() is None else None
        body = client.get("/api/pmo", headers=viewer()).json()
        assert {t["key"] for t in body["tasks"]} == {"A-1", "A-2", "B-1"}
        assert body["scoped"] is False


def test_a_ledger_of_another_tenant_is_refused_with_a_clear_error(tmp_path):
    client = client_for(tmp_path)                        # 台帳は acme のもの
    (tmp_path / "t").mkdir(exist_ok=True)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    wrong = TestClient(create_app(
        Engine(AdapterRegistry(), llms), tmp_path / "t", OPERATOR, viewer_token=VIEWER,
        lang="en", tenant="globex", pmo_ledger=tmp_path / "task-ledger.db"))
    response = wrong.get("/api/pmo", headers=op())
    assert response.status_code == 503 and "acme" in response.json()["detail"]
    assert wrong.post("/api/pmo/assignments/accept", json={"ref": "A-2"},
                      headers=op()).status_code == 503
    assert client.get("/api/pmo", headers=op()).status_code == 200


def test_the_ui_offers_a_project_filter_without_parsing_html():
    js = (Path(__file__).resolve().parents[1] / "aipmo" / "web" / "static"
          / "app.js").read_text(encoding="utf-8")
    assert "pmoProject" in js and "encodeURIComponent(pmoProject)" in js
    assert "innerHTML" not in js
