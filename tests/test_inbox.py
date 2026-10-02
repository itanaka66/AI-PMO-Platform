"""受信箱（GET /api/inbox）のテスト。

デモのサンプルデータ（docs/DEMO.md）を台帳に入れ、「人の判断を待つもの」が 1 つの一覧に集まること、
そこに書かれた操作（actions）を実際に実行すると項目が消えること、権限と範囲の扱いを確かめる。

確かめること:
  (1) 種類をまたいで集まる（判断 4・対応 2・WBS 2・担当 4・成果 1・起票 1 = 14）。緊急度順で、理由つき
  (2) 読むだけ：呼んでも台帳は変わらない
  (3) 権限：viewer には actions を返さない。プロジェクトを限定された viewer には、組織全体の項目を出さない
  (4) 各 action は、既存の API としてそのまま動く（承認・却下・確定・認める・差し戻す・起票・見送り・再計画案）
  (5) 全部決めると、空になる
  (6) 台帳が無い・空のとき、読めない再計画案があるときも落ちない

What matters: one list across every kind of pending human decision, urgency-ordered with reasons;
read-only; viewers get no actions and a confined viewer sees no org-wide items; each action runs as
described against the existing endpoints and removes its item; deciding everything empties the inbox.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aipmo import cli, demo
from aipmo.inbox import KINDS, build_inbox
from aipmo.task_engine import TaskEngine

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "inbox-operator", "inbox-viewer"


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """デモを入れた台帳（読むだけのテストで共有する）。"""
    base = tmp_path_factory.mktemp("inbox") / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    config = cli.load_config(base / "config.yaml")
    demo.load(config, base)
    return base


@pytest.fixture
def fresh(tmp_path):
    """デモを入れた台帳（操作して壊すテスト用に、毎回別に作る）。"""
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path, *, viewer_projects: list[str] | None = None,
               postgres: Any = None) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    members = load_members(config["pmo_core"]["members"])
    built = cli.build_engine(config, base_dir=base)
    if postgres is not None:
        built.adapters.register(postgres)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db", members=members,
                     filing=cli._web_filing(config), viewer_projects=viewer_projects)
    return TestClient(app)


def get(client: TestClient, token: str = OPERATOR, query: str = "") -> dict[str, Any]:
    response = client.get(f"/api/inbox{query}", headers={"x-aipmo-token": token})
    assert response.status_code == 200, response.text
    return response.json()


def by_kind(data: dict[str, Any]) -> dict[str, int]:
    return {k: v for k, v in data["by_kind"].items() if v}


# ===== (1) 集まる / gathering ======================================================================

def test_everything_waiting_for_a_human_is_one_list_across_kinds(loaded):
    data = get(client_for(loaded))
    assert data["total"] == 14 and data["can_act"] is True
    assert by_kind(data) == {"judgment": 4, "followup": 2, "wbs": 2, "assignment": 4, "review": 1,
                             "filing": 1}
    assert set(data["by_kind"]) == set(KINDS)
    assert len({i["id"] for i in data["items"]}) == 14                      # id は重複しない

    for item in data["items"]:
        assert item["title"] and item["summary"] and item["detail"]["sections"]
        assert item["actions"], item["id"]
        for section in item["detail"]["sections"]:
            assert section["heading"] and section["lines"]
        for action in item["actions"]:
            assert action["method"] == "POST" and action["path"].startswith("/api/")
            assert action["label"] and action["style"] in ("primary", "danger", "secondary")


def test_the_order_is_urgency_then_the_older_first(loaded):
    items = get(client_for(loaded))["items"]
    keys = [(-i["urgency"], -i["age_seconds"], i["id"]) for i in items]
    assert keys == sorted(keys)
    assert items[0]["kind"] == "judgment" and items[0]["urgency"] >= 90         # mobile-app のリスク
    assert items[-1]["kind"] == "filing"                                       # 起票は急がない


def test_each_kind_explains_why_and_what_approving_does(loaded):
    items = {i["kind"]: i for i in get(client_for(loaded))["items"]}
    headings = lambda kind: {s["heading"] for s in items[kind]["detail"]["sections"]}   # noqa: E731
    assert {"診断の根拠", "選んだ対処", "承認すると", "しないこと", "却下すると"} <= headings("judgment")
    assert {"きっかけ", "承認すると", "却下すると"} <= headings("followup")
    assert {"きっかけ", "承認すると", "しないこと"} <= headings("wbs")
    assert {"理由", "確定すると"} <= headings("assignment")
    assert {"成果", "認めると", "差し戻すと"} <= headings("review")
    assert {"起票先", "起票すると"} <= headings("filing")
    judgment = items["judgment"]
    text = json.dumps(judgment["detail"], ensure_ascii=False)
    assert "承認しただけでは実行されません" in text and "常駐" in text
    wbs = json.dumps(items["wbs"]["detail"], ensure_ascii=False)
    assert "WBS ファイルは変わりません" in wbs


def test_assignments_carry_the_reason_and_review_carries_the_result(loaded):
    items = get(client_for(loaded))["items"]
    assignment = next(i for i in items if i["kind"] == "assignment")
    assert assignment["suggested_assignee"] in ("鈴木", "高橋")
    assert "スキル一致" in json.dumps(assignment["detail"], ensure_ascii=False)
    review = next(i for i in items if i["kind"] == "review")
    assert review["agent"] == "開発AI" and "デモの下書き" in json.dumps(review["detail"], ensure_ascii=False)
    reject = next(a for a in review["actions"] if a["id"] == "reject")
    assert reject["needs_note"] is True and not next(a for a in review["actions"] if a["id"] == "accept")["needs_note"]


# ===== (2) 読むだけ / read-only ==========================================================================

def test_reading_the_inbox_changes_nothing(loaded):
    ledger = TaskEngine(loaded / "task-ledger.db", tenant="demo")
    before = {k: json.dumps(v.__dict__, default=str, sort_keys=True) for k, v in ledger.tasks.items()}
    ledger.close()
    client = client_for(loaded)
    for _ in range(3):
        get(client)
    after_ledger = TaskEngine(loaded / "task-ledger.db", tenant="demo")
    after = {k: json.dumps(v.__dict__, default=str, sort_keys=True) for k, v in after_ledger.tasks.items()}
    after_ledger.close()
    assert before == after


# ===== (3) 権限と範囲 / permissions and scope =======================================================================

def test_a_viewer_sees_the_list_without_any_action(loaded):
    data = get(client_for(loaded), VIEWER)
    assert data["total"] == 14 and data["can_act"] is False
    assert all(i["actions"] == [] for i in data["items"])


def test_a_viewer_cannot_run_an_action_even_if_it_knows_the_path(loaded):
    client = client_for(loaded)
    item = next(i for i in get(client)["items"] if i["kind"] == "judgment")
    action = item["actions"][0]
    denied = client.post(action["path"], json=action["body"], headers={"x-aipmo-token": VIEWER})
    assert denied.status_code in (401, 403)


def test_a_viewer_confined_to_a_project_sees_only_that_projects_items(loaded):
    data = get(client_for(loaded, viewer_projects=["web-renewal"]), VIEWER)
    kinds = by_kind(data)
    assert "judgment" not in kinds and "filing" not in kinds and "replan" not in kinds    # 組織全体の項目
    assert data["total"] > 0 and all(i["project"] == "web-renewal" for i in data["items"])
    assert all(i["actions"] == [] for i in data["items"])


def test_the_operator_can_narrow_by_project_and_org_wide_items_drop_out(loaded):
    client = client_for(loaded)
    data = get(client, query="?project=infra")
    assert data["total"] > 0 and all(i["project"] == "infra" for i in data["items"])
    assert "judgment" not in by_kind(data) or all(
        i["project"] == "infra" for i in data["items"] if i["kind"] == "judgment")


def test_asking_for_a_project_outside_the_viewers_scope_is_refused(loaded):
    client = client_for(loaded, viewer_projects=["web-renewal"])
    response = client.get("/api/inbox?project=infra", headers={"x-aipmo-token": VIEWER})
    assert response.status_code == 403


# ===== (4)(5) actions run as described / every action works =============================================================

def run_action(client: TestClient, action: dict[str, Any], note: str = "") -> Any:
    body = dict(action["body"])
    if action.get("needs_note"):
        body["note"] = note
    return client.request(action["method"], action["path"], json=body, headers={"x-aipmo-token": OPERATOR})


def test_deciding_every_item_with_its_own_actions_empties_the_inbox(fresh):
    client = client_for(fresh)
    first = get(client)
    total = first["total"]
    assert total == 14
    seen: list[str] = []
    for _ in range(40):                                        # 承認で起票待ちが増えるので、落ち着くまで
        items = get(client)["items"]
        if not items:
            break
        item = items[0]
        seen.append(item["kind"])
        choice = next(a for a in item["actions"] if a["id"] in ("approve", "confirm", "accept", "file"))
        response = run_action(client, choice)
        assert response.status_code == 200, f"{item['id']} {choice['id']}: {response.text}"
        remaining = {i["id"] for i in get(client)["items"]}
        assert item["id"] not in remaining                     # 決めた項目は、消える
    assert get(client)["total"] == 0
    assert {"judgment", "followup", "wbs", "assignment", "review", "filing"} <= set(seen)


def test_reject_and_skip_remove_items_too_and_a_rejection_needs_its_reason(fresh):
    client = client_for(fresh)
    items = get(client)["items"]

    followup = next(i for i in items if i["kind"] == "followup")
    reject = next(a for a in followup["actions"] if a["id"] == "reject")
    assert run_action(client, reject).status_code == 200
    assert followup["id"] not in {i["id"] for i in get(client)["items"]}

    filing = next(i for i in items if i["kind"] == "filing")
    skip = next(a for a in filing["actions"] if a["id"] == "skip")
    assert run_action(client, skip).status_code == 200
    assert filing["id"] not in {i["id"] for i in get(client)["items"]}

    review = next(i for i in items if i["kind"] == "review")
    send_back = next(a for a in review["actions"] if a["id"] == "reject")
    refused = run_action(client, send_back, note="")             # 理由なしの差し戻しは断られる
    assert refused.status_code == 409 and review["id"] in {i["id"] for i in get(client)["items"]}
    assert run_action(client, send_back, note="確認項目が足りない").status_code == 200
    assert review["id"] not in {i["id"] for i in get(client)["items"]}


def test_an_approved_judgment_leaves_the_inbox_but_is_not_executed_by_the_screen(fresh):
    client = client_for(fresh)
    item = next(i for i in get(client)["items"] if i["kind"] == "judgment")
    approve = next(a for a in item["actions"] if a["id"] == "approve")
    assert run_action(client, approve).status_code == 200
    ledger = TaskEngine(fresh / "task-ledger.db", tenant="demo")
    state = ledger.tasks[item["ref"]].payload["state"]
    ledger.close()
    assert state == "approved"                                  # 実行は常駐の仕事。画面は実行しない


def test_confirming_an_assignment_sets_the_assignee(fresh):
    client = client_for(fresh)
    item = next(i for i in get(client)["items"] if i["kind"] == "assignment")
    confirm = item["actions"][0]
    assert confirm["body"]["writeback"] is False                # mock の Jira には書き戻し先が無い
    assert run_action(client, confirm).status_code == 200
    ledger = TaskEngine(fresh / "task-ledger.db", tenant="demo")
    assert ledger.tasks[item["ref"]].assignee == item["suggested_assignee"]
    ledger.close()


# ===== 単体 / the builder alone ============================================================================

def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None, "priority": None,
            "status": None, "blocked": False, "done": False, "labels": []}
    return {**base, **kw}


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def test_an_empty_ledger_gives_an_empty_inbox(tmp_path):
    engine = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    data = build_inbox(engine, now=NOW)
    assert data["items"] == [] and data["total"] == 0 and set(data["by_kind"]) == set(KINDS)


def test_the_writeback_flag_follows_the_trackers_that_can_be_written(tmp_path):
    engine = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    engine.ingest("t", "r", [cand(key="A-1", title="担当未定", project="P", labels=["dev"])])
    with engine.transaction():
        engine.tasks["JIRA:A-1"].suggested_assignee = "ann"
        engine.tasks["JIRA:A-1"].suggestion_reason = "スキル一致"
    plain = build_inbox(engine, now=NOW)["items"][0]
    assert plain["actions"][0]["body"]["writeback"] is False
    jira = build_inbox(engine, writable={"jira"}, now=NOW)["items"][0]
    assert jira["actions"][0]["body"]["writeback"] is True
    assert "担当者も更新します" not in json.dumps(plain["detail"], ensure_ascii=False)
    assert "jira の担当者も更新します" in json.dumps(jira["detail"], ensure_ascii=False)


def test_replan_proposals_join_the_list_with_their_own_endpoints(tmp_path):
    engine = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    rows = [{"id": "r1", "wbs_version_from": "demo-product", "tier": 3, "confidence": 0.7,
             "option_label": "A", "rationale": "遅れを取り戻す", "created_at": NOW.isoformat(),
             "diff": {"changes": [{"op": "set", "node": "1.1", "field": "due", "value": "2026-11-01"}]}},
            {"id": "r2", "wbs_version_from": "demo-product", "tier": 1, "confidence": 0.5,
             "rationale": "x", "created_at": NOW.isoformat(), "diff": {"summary": "自由な形"}}]
    data = build_inbox(engine, replans=rows, now=NOW)
    first, second = data["items"]
    assert (first["kind"], first["urgency"]) == ("replan", 95) and second["urgency"] == 45
    assert [a["path"] for a in first["actions"]] == ["/api/wbs-proposals/r1/approve",
                                                     "/api/wbs-proposals/r1/reject"]
    assert "反映できる変更 1 件" in json.dumps(first["detail"], ensure_ascii=False)
    assert "記録だけ" in json.dumps(second["detail"], ensure_ascii=False)
    scoped = build_inbox(engine, replans=rows, allowed={"p"}, now=NOW)
    assert scoped["items"] == []                                # プロジェクトを限定すると、組織全体の項目は出さない


class StubPostgres:
    name = "postgres"

    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.decided = rows or [], fail, []

    def health_check(self):
        return True

    def query(self, name, params=None):
        if self.fail:
            raise RuntimeError("database is down")
        return {"rows": list(self.rows), "count": len(self.rows)}

    def execute(self, name, params=None, idempotency_key=None):
        self.decided.append(params)
        return {"affected": 1, "rows": [{"id": params["id"], "status": params["status"]}]}


def test_web_merges_replan_proposals_and_survives_an_unreadable_database(fresh):
    rows = [{"id": "r1", "wbs_version_from": "demo-product", "tier": 2, "confidence": 0.7,
             "rationale": "遅れ", "created_at": datetime.now(timezone.utc).isoformat(),
             "diff": {"summary": "x"}}]
    stub = StubPostgres(rows)
    client = client_for(fresh, postgres=stub)
    data = get(client)
    assert data["total"] == 15 and by_kind(data)["replan"] == 1
    replan = next(i for i in data["items"] if i["kind"] == "replan")
    assert run_action(client, next(a for a in replan["actions"] if a["id"] == "approve")).status_code == 200
    assert stub.decided and stub.decided[0]["status"] == "approved"

    broken = client_for(fresh, postgres=StubPostgres(rows, fail=True))
    assert get(broken)["total"] == 14                           # 読めなくても、ほかは出る


def test_no_ledger_yet_is_an_empty_inbox_not_an_error(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    # ledger は作られていないが、PMO は設定されている
    data = get(client_for(base))
    assert data["total"] == 0


def test_effect_sections_carry_a_tone_so_the_screen_can_tell_do_from_dont(loaded):
    tones = {"承認すると": "do", "確定すると": "do", "認めると": "do", "起票すると": "do",
             "しないこと": "dont", "却下すると": "dont", "差し戻すと": "dont"}
    seen = set()
    for item in get(client_for(loaded))["items"]:
        for section in item["detail"]["sections"]:
            assert section["tone"] in ("info", "do", "dont")
            if section["heading"] in tones:
                assert section["tone"] == tones[section["heading"]], (item["id"], section["heading"])
                seen.add(section["heading"])
            else:
                assert section["tone"] == "info", (item["id"], section["heading"])
    assert seen == set(tones)
