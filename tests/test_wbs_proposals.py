"""承認した WBS 変更提案を WBS ファイルへ反映する（wbs_replan との接続）のテスト。

確かめること:
  (1) 承認すると決まった形の変更が WBS ファイルへ反映され、反映の記録が残る
  (2) 反映できない提案は、承認待ちのまま理由を返す（ファイルも提案も変えない）
  (3) 別の WBS の提案・競合・二重反映は止まる。承認は済んで反映だけ失敗したら apply で直せる
  (4) 自由な形の提案は、これまでどおり記録だけ。却下はファイルを変えない
  (5) wbs_replan.propose は、反映できない diff を提案の時点でAIに差し戻す
  (6) CLI と Web（operator のみ）から使える

What matters: approving applies the fixed-shape change and records it; an unapplicable proposal
stays pending with its reasons and nothing changes; another WBS's proposal, a conflict and a
double application are stopped, and an approval whose write failed is repaired by `apply`;
free-form proposals still only record; rejecting never touches the file; the replan tool bounces
an unapplicable diff back to the model; CLI and web (operator only) work.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aipmo import cli
from aipmo.adapters.base import AdapterError, AdapterRegistry
from aipmo.adapters.wbs_replan import WbsReplanAdapter
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.wbs_proposals import (
    ProposalError,
    Target,
    apply_approved,
    applied_before,
    approve,
    changes_of,
)

TODAY = date(2026, 10, 2)

WBS = """\
wbs:
  id: demo
  name: デモ
  nodes:
    - id: "1"
      name: "基盤"
      children:
        - id: "1.1"
          name: "作業 A"
          status: todo
          effort: 3
          priority: Low
        - id: "1.2"
          name: "作業 B"
          status: todo
          effort: 2
"""

GOOD = [{"op": "set", "node": "1.1", "field": "due", "value": "2026-11-01"},
        {"op": "set", "node": "1.1", "field": "priority", "value": "High"}]


class StubPostgres:
    """wbs_replan_proposals の代わり。承認待ちのときだけ決定できる（実クエリと同じ）。"""
    name = "postgres"

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.hook = None

    def add(self, pid: str, diff: Any, *, wbs: str = "demo", status: str = "pending",
            tier: int = 2) -> str:
        self.rows[pid] = {"id": pid, "tenant": "acme", "wbs_version_from": wbs, "diff": diff,
                          "rationale": "遅れを取り戻す", "assumptions": {}, "tier": tier,
                          "confidence": 0.7, "option_label": None, "status": status}
        return pid

    def health_check(self) -> bool:
        return True

    def query(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if name == "pending_wbs_proposals":
            rows = [r for r in self.rows.values() if r["status"] == "pending"]
            return {"rows": rows, "count": len(rows)}
        if name == "get_wbs_proposal":
            row = self.rows.get(params["id"])
            return {"rows": [row] if row else [], "count": 1 if row else 0}
        raise AssertionError(name)

    def execute(self, name: str, params: dict[str, Any] | None = None,
                idempotency_key: str | None = None) -> dict[str, Any]:
        assert name == "decide_wbs_proposal"
        params = params or {}
        self.executed.append((name, params))
        if self.hook:
            self.hook()
        row = self.rows.get(params["id"])
        if row is None or row["status"] != "pending":
            return {"affected": 0, "rows": []}
        row["status"] = params["status"]
        return {"affected": 1, "rows": [{"id": row["id"], "status": params["status"]}]}


@pytest.fixture
def world(tmp_path):
    wbs = tmp_path / "wbs.yaml"
    wbs.write_text(WBS, encoding="utf-8")
    target = Target(file=wbs, root=tmp_path, decisions=tmp_path / "pmo-decisions.jsonl")
    return StubPostgres(), target, wbs


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ===== (1) 承認して反映 / approve and apply ===================================================================

def test_changes_are_extracted_only_from_the_fixed_shape():
    assert changes_of({"diff": {"changes": GOOD}}) == GOOD
    assert changes_of({"diff": json.dumps({"changes": GOOD})}) == GOOD         # JSON 文字列でも
    assert changes_of({"diff": {"move": "1.1 を後ろへ"}}) is None
    assert changes_of({"diff": "not json"}) is None and changes_of({}) is None


def test_approving_applies_the_change_and_records_it(world):
    pg, target, wbs = world
    pg.add("p1", {"changes": GOOD})
    out = approve(pg, "acme", "p1", "sato", "了解", target, as_of=TODAY)
    assert out["status"] == "approved" and out["applied"] is True and out["error"] is None
    assert "due: 2026-11-01" in text(wbs) and "priority: High" in text(wbs)
    assert pg.rows["p1"]["status"] == "approved"
    assert pg.executed[0][1]["decided_by"] == "sato" and pg.executed[0][1]["decision_note"] == "了解"
    entry = json.loads(target.decisions.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["kind"] == "wbs_proposal_applied" and entry["proposal"] == "p1"
    assert entry["by"] == "sato" and entry["changes"]
    assert applied_before(target.decisions, "p1") and not applied_before(target.decisions, "p2")


# ===== (2) 反映できない提案 / an unapplicable proposal ====================================================

def test_an_unapplicable_proposal_stays_pending_and_nothing_changes(world):
    pg, target, wbs = world
    before = wbs.read_bytes()
    pg.add("p1", {"changes": [{"op": "set", "node": "9.9", "field": "effort", "value": 1}]})
    with pytest.raises(ProposalError) as caught:
        approve(pg, "acme", "p1", "sato", None, target, as_of=TODAY)
    assert caught.value.kind == "invalid" and "9.9" in str(caught.value)
    assert pg.rows["p1"]["status"] == "pending" and pg.executed == []         # 承認も走らない
    assert wbs.read_bytes() == before and not target.decisions.exists()

    pg.add("p2", {"changes": [{"op": "set", "node": "1.1", "field": "status", "value": "done"}]})
    with pytest.raises(ProposalError, match="done_on"):
        approve(pg, "acme", "p2", "sato", None, target, as_of=TODAY)
    assert pg.rows["p2"]["status"] == "pending"

    pg.add("p3", {"changes": "壊れた形"})
    with pytest.raises(ProposalError) as bad:
        approve(pg, "acme", "p3", "sato", None, target)
    assert bad.value.kind == "invalid" and pg.rows["p3"]["status"] == "pending"


# ===== (3) 取り違え・競合・二重 / wrong wbs, conflict, double apply =====================================

def test_a_proposal_for_another_wbs_is_never_applied(world):
    pg, target, wbs = world
    before = wbs.read_bytes()
    pg.add("p1", {"changes": GOOD}, wbs="wbs-main")
    with pytest.raises(ProposalError, match="別の WBS"):
        approve(pg, "acme", "p1", "sato", None, target, as_of=TODAY)
    assert wbs.read_bytes() == before and pg.rows["p1"]["status"] == "pending"


def test_a_decided_or_missing_proposal_is_refused(world):
    pg, target, wbs = world
    before = wbs.read_bytes()
    with pytest.raises(ProposalError) as missing:
        approve(pg, "acme", "nope", "sato", None, target)
    assert missing.value.kind == "not_found"
    pg.add("p1", {"changes": GOOD}, status="rejected")
    with pytest.raises(ProposalError) as decided:
        approve(pg, "acme", "p1", "sato", None, target)
    assert decided.value.kind == "not_pending" and wbs.read_bytes() == before


def test_losing_the_race_to_another_approver_applies_nothing(world):
    pg, target, wbs = world
    before = wbs.read_bytes()
    pg.add("p1", {"changes": GOOD})
    pg.hook = lambda: pg.rows["p1"].update(status="rejected")                 # 直前に誰かが却下
    with pytest.raises(ProposalError) as caught:
        approve(pg, "acme", "p1", "sato", None, target, as_of=TODAY)
    assert caught.value.kind == "not_pending" and wbs.read_bytes() == before


def test_a_file_edited_between_checking_and_writing_is_kept_and_apply_repairs_it(world):
    pg, target, wbs = world
    pg.add("p1", {"changes": GOOD})
    pg.hook = lambda: wbs.write_text(text(wbs) + "# 人が追記\n", encoding="utf-8")   # 承認の直前に編集
    out = approve(pg, "acme", "p1", "sato", None, target, as_of=TODAY)
    assert out["status"] == "approved" and out["applied"] is False
    assert "書き換えられました" in out["error"] and "apply" in out["error"]
    assert "# 人が追記" in text(wbs) and "due: 2026-11-01" not in text(wbs)
    assert not applied_before(target.decisions, "p1")

    pg.hook = None
    done = apply_approved(pg, "acme", "p1", "sato", target, as_of=TODAY)        # 確かめてから反映し直す
    assert done["applied"] is True and "due: 2026-11-01" in text(wbs) and "# 人が追記" in text(wbs)


def test_applying_twice_is_refused_unless_forced_so_a_later_human_edit_is_not_undone(world):
    pg, target, wbs = world
    pg.add("p1", {"changes": GOOD})
    approve(pg, "acme", "p1", "sato", None, target, as_of=TODAY)
    wbs.write_text(text(wbs).replace("priority: High", "priority: Medium"), encoding="utf-8")   # 人の後の修正
    with pytest.raises(ProposalError) as caught:
        apply_approved(pg, "acme", "p1", "sato", target, as_of=TODAY)
    assert caught.value.kind == "applied" and "priority: Medium" in text(wbs)
    forced = apply_approved(pg, "acme", "p1", "sato", target, force=True, as_of=TODAY)
    assert forced["applied"] is True and "priority: High" in text(wbs)


def test_only_an_approved_proposal_with_changes_can_be_applied(world):
    pg, target, wbs = world
    pg.add("p1", {"changes": GOOD})
    with pytest.raises(ProposalError) as pending:
        apply_approved(pg, "acme", "p1", "sato", target)
    assert pending.value.kind == "not_approved"
    pg.add("p2", {"move": "free form"}, status="approved")
    with pytest.raises(ProposalError, match="決まった形"):
        apply_approved(pg, "acme", "p2", "sato", target)
    pg.add("p3", {"changes": GOOD}, status="rejected")
    with pytest.raises(ProposalError):
        apply_approved(pg, "acme", "p3", "sato", target)
    assert "due:" not in text(wbs)


# ===== (4) 自由な形・却下 / free form and rejection ======================================================

def test_a_free_form_proposal_is_only_recorded(world):
    pg, target, wbs = world
    before = wbs.read_bytes()
    pg.add("p1", {"summary": "1.1 を来週へ"})
    out = approve(pg, "acme", "p1", "sato", None, target)
    assert out["status"] == "approved" and out["applied"] is None
    assert wbs.read_bytes() == before and not target.decisions.exists()
    pg.add("p2", {"changes": GOOD})
    out = approve(pg, "acme", "p2", "sato", None, None)                          # 反映先なし
    assert out["applied"] is None and wbs.read_bytes() == before


# ===== (5) 提案の時点での確認 / checked when proposed ===================================================

class RecordingPostgres:
    name = "postgres"

    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def health_check(self) -> bool:
        return True

    def query(self, name, params=None):
        return {"rows": [{"tier": 2}], "count": 1}

    def execute(self, name, params=None, idempotency_key=None):
        self.saved.append(params)
        return {"rows": [{"id": params["id"]}]}


def replan(tmp_path: Path, with_file: bool = True) -> tuple[WbsReplanAdapter, RecordingPostgres]:
    pg = RecordingPostgres()
    wbs = tmp_path / "wbs.yaml"
    wbs.write_text(WBS, encoding="utf-8")
    return WbsReplanAdapter(postgres=pg, file=str(wbs) if with_file else None,
                            root=str(tmp_path)), pg  # type: ignore[arg-type]


def test_propose_bounces_an_unapplicable_diff_back_to_the_model(tmp_path):
    adapter, pg = replan(tmp_path)
    with pytest.raises(AdapterError, match="9.9"):
        adapter.propose("demo", {"changes": [{"op": "set", "node": "9.9", "field": "effort",
                                              "value": 1}]}, "理由", 0.5)
    with pytest.raises(AdapterError, match="status"):
        adapter.propose("demo", {"changes": [{"op": "set", "node": "1.1", "field": "status",
                                              "value": "finished"}]}, "理由", 0.5)
    with pytest.raises(AdapterError, match="別の WBS"):
        adapter.propose("other", {"changes": GOOD}, "理由", 0.5)
    assert pg.saved == []                                                     # 何も記録されない


def test_propose_accepts_a_good_diff_and_a_free_form_one(tmp_path):
    adapter, pg = replan(tmp_path)
    assert adapter.propose("demo", {"changes": GOOD}, "理由", 0.8)["proposed"] is True
    assert adapter.propose("demo", {"summary": "自由な形"}, "理由", 0.8,
                           option_label="b")["proposed"] is True
    assert pg.saved[0]["diff"]["changes"][0]["op"] == "set"
    assert pg.saved[1]["diff"] == {"summary": "自由な形"}                      # 従来どおり


def test_without_a_target_file_only_the_shape_is_checked(tmp_path):
    adapter, pg = replan(tmp_path, with_file=False)
    assert adapter.propose("demo", {"changes": GOOD}, "理由", 0.8)["proposed"] is True
    with pytest.raises(AdapterError, match="op"):
        adapter.propose("demo", {"changes": [{"op": "zap"}]}, "理由", 0.8)


# ===== (6) CLI ===================================================================================================

def cli_world(tmp_path, monkeypatch, pg: StubPostgres):
    registry = AdapterRegistry()
    registry.register(pg)                                                    # type: ignore[arg-type]
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry, llms))
    wbs = tmp_path / "wbs.yaml"
    wbs.write_text(WBS, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\nadapters:\n  wbs_replan: {file: wbs.yaml}\n", encoding="utf-8")
    return config, wbs


def run(config: Path, *args: str) -> int:
    return cli.main(["--config", str(config), "wbs", "proposals", *args])


def test_cli_lists_shows_approves_rejects_and_reapplies(tmp_path, monkeypatch, capsys):
    pg = StubPostgres()
    pg.add("p1", {"changes": GOOD})
    pg.add("p2", {"summary": "自由な形"}, tier=1)
    pg.add("p3", {"changes": GOOD})
    config, wbs = cli_world(tmp_path, monkeypatch, pg)
    before = wbs.read_bytes()

    assert run(config) == 0
    out = capsys.readouterr().out
    assert "p1" in out and "反映できる変更 2 件" in out and "自由な形" in out
    assert run(config, "show", "p1") == 0
    shown = capsys.readouterr().out
    assert "due: 2026-11-01" in shown and "priority" in shown
    assert wbs.read_bytes() == before and pg.executed == []                  # 見るだけでは何も変わらない

    assert run(config, "reject", "p3") == 0
    assert pg.rows["p3"]["status"] == "rejected" and wbs.read_bytes() == before

    assert run(config, "approve", "p1", "--by", "sato") == 0
    assert "反映しました" in capsys.readouterr().out
    assert "due: 2026-11-01" in text(wbs) and pg.rows["p1"]["status"] == "approved"
    assert run(config, "apply", "p1") == 1                                     # 二重反映は止まる
    assert "すでに反映済み" in capsys.readouterr().err
    assert run(config, "apply", "p1", "--force") == 0

    assert run(config, "approve", "p2") == 0
    assert "自由な形" in capsys.readouterr().out
    assert run(config, "approve") == 1 and run(config, "show", "nope") == 1


def test_cli_approve_of_an_unapplicable_proposal_exits_nonzero_and_leaves_it_pending(
        tmp_path, monkeypatch, capsys):
    pg = StubPostgres()
    pg.add("p1", {"changes": [{"op": "set", "node": "9.9", "field": "effort", "value": 1}]})
    config, wbs = cli_world(tmp_path, monkeypatch, pg)
    assert run(config, "approve", "p1") == 1
    assert "9.9" in capsys.readouterr().err and pg.rows["p1"]["status"] == "pending"


def test_cli_without_postgres_falls_back_to_the_ledger(tmp_path, monkeypatch, capsys):
    """postgres が無くても台帳（SQLite 既定）で動く（5.7）。"""
    registry = AdapterRegistry()
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry, LLMRegistry()))
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")
    assert run(config) == 0
    assert "pending WBS proposals (0)" in capsys.readouterr().out


def test_cli_reports_a_broken_ledger_config_clearly(tmp_path, monkeypatch, capsys):
    """postgres も、使える台帳も無ければ、両方を案内する理由を返す。"""
    registry = AdapterRegistry()
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry, LLMRegistry()))
    config = tmp_path / "config.yaml"
    config.write_text(
        "tenant: acme\ntask_engine:\n  backend: postgres\n", encoding="utf-8")   # dsn が無い
    assert run(config) == 1
    err = capsys.readouterr().err
    assert "postgres" in err and "task_engine" in err


# ===== (6) Web =====================================================================================================

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "operator-token-1", "viewer-token-2"


def web(tmp_path, pg: StubPostgres, with_target: bool = True):
    wbs = tmp_path / "wbs.yaml"
    wbs.write_text(WBS, encoding="utf-8")
    registry = AdapterRegistry()
    registry.register(pg)                                                    # type: ignore[arg-type]
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    target = Target(file=wbs, root=tmp_path, decisions=tmp_path / "pmo-decisions.jsonl")
    app = create_app(Engine(registry, llms), tmp_path / "t", OPERATOR, viewer_token=VIEWER,
                     tenant="acme", lang="en", store=RunStore(),
                     wbs_target=target if with_target else None)
    return TestClient(app), wbs


def headers(token):
    return {"x-aipmo-token": token}


def test_web_approval_applies_for_the_operator_and_not_for_a_viewer(tmp_path):
    pg = StubPostgres()
    pg.add("p1", {"changes": GOOD})
    client, wbs = web(tmp_path, pg)
    denied = client.post("/api/wbs-proposals/p1/approve", headers=headers(VIEWER), json={})
    assert denied.status_code in (401, 403) and "due:" not in text(wbs)
    ok = client.post("/api/wbs-proposals/p1/approve", headers=headers(OPERATOR), json={"note": "ok"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "approved" and body["applied"] is True and body["report"]
    assert "due: 2026-11-01" in text(wbs)
    again = client.post("/api/wbs-proposals/p1/approve", headers=headers(OPERATOR), json={})
    assert again.status_code == 409


def test_web_returns_the_reasons_and_keeps_an_unapplicable_proposal_pending(tmp_path):
    pg = StubPostgres()
    pg.add("p1", {"changes": [{"op": "set", "node": "9.9", "field": "effort", "value": 1}]})
    client, wbs = web(tmp_path, pg)
    before = wbs.read_bytes()
    response = client.post("/api/wbs-proposals/p1/approve", headers=headers(OPERATOR), json={})
    assert response.status_code == 422
    assert "9.9" in json.dumps(response.json(), ensure_ascii=False)
    assert pg.rows["p1"]["status"] == "pending" and wbs.read_bytes() == before
    assert client.post("/api/wbs-proposals/nope/approve", headers=headers(OPERATOR),
                       json={}).status_code == 404


def test_web_reject_never_touches_the_file_and_without_a_target_behaves_as_before(tmp_path):
    pg = StubPostgres()
    pg.add("p1", {"changes": GOOD})
    pg.add("p2", {"changes": GOOD})
    client, wbs = web(tmp_path, pg)
    before = wbs.read_bytes()
    assert client.post("/api/wbs-proposals/p1/reject", headers=headers(OPERATOR),
                       json={}).status_code == 200
    assert wbs.read_bytes() == before

    plain, wbs2 = web(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, pg, False)
    out = plain.post("/api/wbs-proposals/p2/approve", headers=headers(OPERATOR), json={})
    assert out.status_code == 200 and out.json() == {"id": "p2", "status": "approved"}
    assert "due:" not in text(wbs2)


def test_a_decided_proposal_is_reported_as_decided_even_if_its_changes_are_also_bad(world):
    pg, target, _ = world
    pg.add("p1", {"changes": [{"op": "set", "node": "9.9", "field": "effort", "value": 1}]},
           status="rejected")
    with pytest.raises(ProposalError) as caught:
        approve(pg, "acme", "p1", "sato", None, target)
    assert caught.value.kind == "not_pending"


# ===== (7) 反映前のプレビュー / preview before approving ==========================================================

def test_the_preview_shows_what_approving_would_do_and_writes_nothing(tmp_path):
    pg = StubPostgres()
    pg.add("p1", {"changes": GOOD})
    client, wbs = web(tmp_path, pg)
    before = wbs.read_bytes()
    body = client.get("/api/wbs-proposals/p1/preview", headers=headers(VIEWER)).json()
    assert body["applicable"] is True and body["changed"] is True and body["report"]
    assert "2026-11-01" in body["diff"] and body["already_applied"] is False
    assert wbs.read_bytes() == before and pg.rows["p1"]["status"] == "pending" and not pg.executed


def test_the_preview_says_why_it_cannot_apply_instead_of_failing(tmp_path):
    pg = StubPostgres()
    pg.add("bad", {"changes": [{"op": "set", "node": "9.9", "field": "effort", "value": 1}]})
    pg.add("free", {"move": "1.1 を後ろへ"})
    client, _ = web(tmp_path, pg)
    bad = client.get("/api/wbs-proposals/bad/preview", headers=headers(OPERATOR)).json()
    assert bad["applicable"] is False and bad["reason"] == "invalid" and "9.9" in " ".join(bad["problems"])
    free = client.get("/api/wbs-proposals/free/preview", headers=headers(OPERATOR)).json()
    assert free["applicable"] is False and free["reason"] == "free_form"
    assert client.get("/api/wbs-proposals/nope/preview", headers=headers(OPERATOR)).status_code == 404
    assert client.get("/api/wbs-proposals/bad/preview").status_code == 401


def test_the_preview_without_a_target_and_after_applying(tmp_path):
    pg = StubPostgres()
    pg.add("p1", {"changes": GOOD})
    plain, _ = web(tmp_path, pg, with_target=False)
    out = plain.get("/api/wbs-proposals/p1/preview", headers=headers(OPERATOR)).json()
    assert out["applicable"] is False and out["reason"] == "no_target"
    client, _ = web(tmp_path, pg)
    assert client.post("/api/wbs-proposals/p1/approve", headers=headers(OPERATOR), json={}).status_code == 200
    after = client.get("/api/wbs-proposals/p1/preview", headers=headers(OPERATOR)).json()
    assert after["already_applied"] is True
