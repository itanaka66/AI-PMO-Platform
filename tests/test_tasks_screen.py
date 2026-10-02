"""タスク画面の API（GET /api/tasks, /api/tasks/{id}）のテスト。

デモのデータで、絞り込み・並び・ページ、点数の内訳（項目の合計が点数と一致する）、詳細（由来・警告・履歴）、
読むだけであること、権限（viewer の範囲）を確かめる。

Task list/detail API against the demo data: filters, sort, paging, per-item score parts that sum to
the score, detail with alerts and history, read-only, viewer scope.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aipmo import cli, demo

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "tasks-operator", "tasks-viewer"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    base = tmp_path_factory.mktemp("tasks") / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path, viewer_projects=None) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]),
                     filing=cli._web_filing(config), viewer_projects=viewer_projects)
    return TestClient(app)


def get(client, path, token=OPERATOR):
    return client.get(path, headers={"x-aipmo-token": token})


def test_the_parts_of_every_score_add_up_to_the_score(base):
    data = get(client_for(base), "/api/tasks?limit=200").json()
    assert data["total"] >= 20 and data["items"]
    for item in data["items"]:
        assert sum(p["points"] for p in item["parts"]) == item["score"], item["id"]
        assert item["parts"][0]["kind"] == "priority"
    assert [i["score"] for i in data["items"]] == sorted((i["score"] for i in data["items"]), reverse=True)


def test_filters_sort_and_paging(base):
    client = client_for(base)
    everything = get(client, "/api/tasks?limit=200").json()
    projects = sorted({i["project"] for i in everything["items"]})
    assert len(projects) == 3 and set(projects) <= set(everything["projects"])
    one = get(client, f"/api/tasks?project={projects[0]}&limit=200").json()
    assert 0 < one["total"] < everything["total"] and {i["project"] for i in one["items"]} == {projects[0]}
    page1 = get(client, "/api/tasks?limit=5").json()
    page2 = get(client, "/api/tasks?limit=5&offset=5").json()
    assert len(page1["items"]) == 5 and not {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}
    assert page1["total"] == everything["total"]
    by_due = get(client, "/api/tasks?sort=due&limit=200").json()["items"]
    dues = [i["due_date"] or "9999" for i in by_due]
    assert dues == sorted(dues)
    nobody = get(client, "/api/tasks?assignee=-&limit=200").json()
    assert nobody["items"] and all(not i["assignee"] for i in nobody["items"])
    person = everything["assignees"][0]
    assert all(i["assignee"] == person for i in get(client, f"/api/tasks?assignee={person}").json()["items"])
    title = everything["items"][0]["title"]
    found = get(client, f"/api/tasks?q={title[:4]}").json()
    assert any(i["title"] == title for i in found["items"])
    done = get(client, "/api/tasks?state=done&limit=200").json()
    assert all(i["done"] and i["parts"] == [] for i in done["items"])   # デモには完了のタスクは残っていない
    assert get(client, "/api/tasks?state=all").json()["total"] >= everything["total"]
    assert get(client, "/api/tasks?state=nope").status_code == 422


def test_detail_has_the_parts_alerts_and_history(base):
    client = client_for(base)
    top = get(client, "/api/tasks?limit=1").json()["items"][0]
    detail = get(client, f"/api/tasks/{top['id']}").json()
    assert detail["id"] == top["id"] and sum(p["points"] for p in detail["parts"]) == detail["score"]
    assert "alerts" in detail and "history" in detail and "dispatches" in detail
    alerted = [a["task"] for a in get(client, "/api/pmo").json()["briefing"]["alerts"]]
    assert alerted
    with_alert = get(client, f"/api/tasks/{alerted[0]}").json()
    assert with_alert["alerts"] and all(a["task"] == alerted[0] for a in with_alert["alerts"])
    assert get(client, "/api/tasks/nope").status_code == 404


def test_reading_changes_nothing(base):
    ledger = base / "task-ledger.db"
    client = client_for(base)
    get(client, "/api/tasks?limit=200")
    first = get(client, "/api/tasks?limit=200").json()
    assert first == get(client, "/api/tasks?limit=200").json()
    assert ledger.exists()


def test_a_scoped_viewer_sees_only_their_projects(base):
    free = get(client_for(base), "/api/tasks?limit=200").json()
    mine = free["items"][0]["project"]
    client = client_for(base, viewer_projects=[mine])
    scoped = get(client, "/api/tasks?limit=200", VIEWER).json()
    assert scoped["total"] and {i["project"] for i in scoped["items"]} == {mine}
    assert scoped["projects"] == [mine]   # 範囲外のプロジェクト名も出さない
    other = next(i for i in free["items"] if i["project"] != mine)
    assert get(client, f"/api/tasks/{other['id']}", VIEWER).status_code == 404
    assert get(client, f"/api/tasks/{other['id']}", OPERATOR).status_code == 200
    assert get(client, f"/api/tasks?project={other['project']}", VIEWER).status_code == 403
    assert client.get("/api/tasks").status_code == 401


def test_without_a_ledger_there_is_nothing_to_list(tmp_path):
    config = cli.load_config(ROOT / "demo" / "config.yaml")
    built = cli.build_engine(config, base_dir=ROOT / "demo")
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), tmp_path, OPERATOR, viewer_token=VIEWER,
                     pmo_ledger=tmp_path / "none.db")
    assert get(TestClient(app), "/api/tasks").status_code == 404


# ===== メンバーと学習 / members and learning ==========================================================

def test_the_learning_screen_shows_why_each_member_was_adjusted(base):
    data = get(client_for(base), "/api/learning/members").json()
    by_name = {m["name"]: m for m in data["members"]}
    assert {"佐藤", "鈴木", "田中", "高橋"} <= set(by_name)
    sato, suzuki = by_name["佐藤"], by_name["鈴木"]
    assert sato["factor"] < 1 < suzuki["factor"]                              # 遅れがちは減、期限どおりは増
    assert sato["on_time_rate"] < suzuki["on_time_rate"] and sato["avg_late_days"] > 0
    assert sato["effective_capacity"] == max(1, round(sato["capacity"] * sato["factor"]))
    assert sato["samples"] >= 5 and len(sato["recent"]) == 5 and sato["pace"] > suzuki["pace"]
    few = by_name["高橋"]                                                      # 実績が少ない人は補正しない
    assert few["samples"] < 5 and few["factor"] == 1.0 and few["effective_capacity"] == few["capacity"]
    assert data["samples"] == 20 and data["baseline_late_rate"] == 0.5
    assert {x["label"] for x in data["labels"]} == {"bug", "dev"} and data["pace"]["reliable"] is True
    assert sum(m["load"] for m in data["members"]) > 0


def test_the_learning_screen_is_read_only_and_not_for_a_scoped_viewer(base):
    client = client_for(base, viewer_projects=["infra"])
    assert get(client, "/api/learning/members", VIEWER).status_code == 403
    assert get(client, "/api/learning/members", OPERATOR).status_code == 200
    assert get(client_for(base), "/api/learning/members", VIEWER).status_code == 200
    assert client.get("/api/learning/members").status_code == 401


# ===== 連携 / integrations ============================================================================

def test_the_integrations_screen_reports_adapters_filing_wbs_and_accounts(base):
    data = get(client_for(base), "/api/integrations").json()
    names = {a["name"] for a in data["adapters"]}
    assert "jira" in names and all(isinstance(a["healthy"], bool) for a in data["adapters"])
    assert data["filing"]["tracker"] == "jira" and data["filing"]["can_file"] is True
    assert data["filing"]["pending"] == 1                                # デモには起票待ちが 1 件ある
    assert data["wbs"]["found"] == 2 and data["wbs"]["error"] is None    # 仕込んだ 2 つのずれ
    assert {a["member"] for a in data["accounts"]} >= {"佐藤", "開発AI"}
    assert next(a for a in data["accounts"] if a["member"] == "開発AI")["is_agent"] is True


def test_the_integrations_screen_is_read_only_and_not_for_a_scoped_viewer(base):
    ok = client_for(base)
    assert get(ok, "/api/integrations", VIEWER).status_code == 200
    scoped = client_for(base, viewer_projects=["infra"])
    assert get(scoped, "/api/integrations", VIEWER).status_code == 403
    assert get(scoped, "/api/integrations", OPERATOR).status_code == 200
    assert ok.get("/api/integrations").status_code == 401


# ===== 画面からの書き込みで、学習した補正が失われない =====================================================

def test_approving_a_proposal_in_the_screen_keeps_the_learned_scores(base):
    client = client_for(base)

    def scores():
        return {i["id"]: i["score"] for i in get(client, "/api/tasks?limit=200").json()["items"]}

    before = scores()
    proposal = next(i for i in get(client, "/api/inbox").json()["items"] if i["kind"] == "followup")
    action = proposal["actions"][0]
    done = client.request(action["method"], action["path"], headers={"x-aipmo-token": OPERATOR},
                          json=action["body"])
    assert done.status_code == 200
    after = scores()
    assert {k: v for k, v in before.items() if after.get(k) != v} == {}   # 既存のタスクの点数は動かない
    assert before["JIRA:WEB-103"] == after["JIRA:WEB-103"] == 74          # 見積りのペースの加点も残る
