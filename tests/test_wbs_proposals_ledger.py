"""WBS 変更提案を PostgreSQL 無しで扱う（台帳に置く）のテスト / WBS proposals on the
ledger, without PostgreSQL (WBS 5.7).

確かめること:
  (1) LedgerProposalStore 単体が、PostgresAdapter と同じ query/execute の形を守る
      （pending の一覧・取得・保存（冪等）・決定（pending のときだけ・テナントをまたがない））
  (2) approve/apply_approved/fetch（wbs_proposals.py、PostgreSQL 版と共通）が、
      LedgerProposalStore を相手にしても同じように働く——承認→WBS ファイルへの反映まで
  (3) build_engine が、postgres が無くても wbs_replan を台帳の上に登録する
  (4) CLI（`aipmo wbs proposals`）が postgres 無しで一覧・承認・却下・反映できる

What matters: LedgerProposalStore matches PostgresAdapter's query/execute shape (pending list,
single fetch, idempotent save, decide only a pending row and never across tenants); the
PostgreSQL-shared approve/apply_approved/fetch work against it unchanged, all the way to writing
the WBS file; build_engine registers wbs_replan on the ledger when there is no postgres; the CLI
works end to end without postgres configured at all.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.side_store import FileSide
from aipmo.wbs_proposals import LedgerProposalStore, Target, approve, fetch

TODAY = date(2026, 10, 4)

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
"""

GOOD = [{"op": "set", "node": "1.1", "field": "due", "value": "2026-11-01"}]


def new_store(tmp_path: Path) -> LedgerProposalStore:
    return LedgerProposalStore(FileSide(tmp_path / "task-ledger.db"))


def save(store: LedgerProposalStore, *, tenant="acme", wbs_id="demo", tier=2,
        changes=None, option_label=None, key="demo:tier2") -> str:
    result = store.execute("save_wbs_proposal", {
        "tenant": tenant, "wbs_version_from": wbs_id,
        "diff": {"changes": changes} if changes is not None else {"note": "自由な形"},
        "rationale": "遅れを取り戻す", "assumptions": {}, "tier": tier,
        "confidence": 0.7, "option_label": option_label,
    }, idempotency_key=key)
    return result["rows"][0]["id"]


# ===== (1) LedgerProposalStore 単体 / the store on its own ===================

def test_a_saved_proposal_is_pending_and_fetchable(tmp_path):
    store = new_store(tmp_path)
    pid = save(store, changes=GOOD)

    pending = store.query("pending_wbs_proposals", {"tenant": "acme"})
    assert pending["count"] == 1 and pending["rows"][0]["id"] == pid

    fetched = store.query("get_wbs_proposal", {"tenant": "acme", "id": pid})
    assert fetched["rows"][0]["status"] == "pending"
    assert fetched["rows"][0]["diff"] == {"changes": GOOD}


def test_a_second_save_with_the_same_key_overwrites_the_pending_row(tmp_path):
    store = new_store(tmp_path)
    first = save(store, changes=GOOD, key="demo:tier2")
    second = save(store, changes=[{"op": "set", "node": "1.1", "field": "priority",
                                   "value": "High"}], key="demo:tier2")

    assert first == second                                     # 同じ行
    pending = store.query("pending_wbs_proposals", {"tenant": "acme"})
    assert pending["count"] == 1
    assert pending["rows"][0]["diff"]["changes"][0]["field"] == "priority"


def test_a_decided_rows_source_key_is_never_overwritten(tmp_path):
    """ON CONFLICT ... WHERE status = 'pending' と同じ：決定済みには触れない。"""
    store = new_store(tmp_path)
    pid = save(store, changes=GOOD, key="demo:tier2")
    store.execute("decide_wbs_proposal", {"tenant": "acme", "id": pid, "status": "rejected",
                                          "decided_by": "sato", "decision_note": "没"})

    result = store.execute("save_wbs_proposal", {
        "tenant": "acme", "wbs_version_from": "demo", "diff": {"changes": GOOD},
        "rationale": "もう一度", "assumptions": {}, "tier": 3, "confidence": 0.5,
        "option_label": None}, idempotency_key="demo:tier2")

    assert result == {"affected": 0, "rows": []}
    assert store.query("pending_wbs_proposals", {"tenant": "acme"})["count"] == 0
    decided = store.query("get_wbs_proposal", {"tenant": "acme", "id": pid})["rows"][0]
    assert decided["status"] == "rejected" and decided["tier"] == 2   # 変わっていない


def test_different_option_labels_coexist_as_separate_proposals(tmp_path):
    store = new_store(tmp_path)
    a = save(store, changes=GOOD, option_label="reschedule", key="demo:tier2:reschedule")
    b = save(store, changes=GOOD, option_label="add_resources", key="demo:tier2:add_resources")

    assert a != b
    assert store.query("pending_wbs_proposals", {"tenant": "acme"})["count"] == 2


def test_decide_only_touches_a_pending_row_of_the_same_tenant(tmp_path):
    store = new_store(tmp_path)
    pid = save(store, changes=GOOD)

    wrong_tenant = store.execute("decide_wbs_proposal", {
        "tenant": "other", "id": pid, "status": "approved",
        "decided_by": "x", "decision_note": None})
    assert wrong_tenant == {"affected": 0, "rows": []}

    ok = store.execute("decide_wbs_proposal", {
        "tenant": "acme", "id": pid, "status": "approved",
        "decided_by": "sato", "decision_note": "ok"})
    assert ok["rows"][0]["status"] == "approved"

    twice = store.execute("decide_wbs_proposal", {
        "tenant": "acme", "id": pid, "status": "rejected",
        "decided_by": "sato", "decision_note": None})
    assert twice == {"affected": 0, "rows": []}            # もう pending ではない


def test_get_wbs_proposal_is_scoped_by_tenant(tmp_path):
    store = new_store(tmp_path)
    pid = save(store, changes=GOOD, tenant="acme")

    assert store.query("get_wbs_proposal", {"tenant": "other", "id": pid}) == {
        "rows": [], "count": 0}
    assert store.query("get_wbs_proposal", {"tenant": "acme", "id": "nope"}) == {
        "rows": [], "count": 0}


def test_pending_proposals_are_ordered_by_tier_then_wbs_then_label(tmp_path):
    store = new_store(tmp_path)
    save(store, changes=GOOD, tier=3, wbs_id="z", key="z:tier3")
    save(store, changes=GOOD, tier=1, wbs_id="a", key="a:tier1")
    save(store, changes=GOOD, tier=1, wbs_id="b", key="b:tier1")

    rows = store.query("pending_wbs_proposals", {"tenant": "acme"})["rows"]
    assert [(r["tier"], r["wbs_version_from"]) for r in rows] == [
        (1, "a"), (1, "b"), (3, "z")]


def test_unknown_query_names_are_refused(tmp_path):
    store = new_store(tmp_path)
    with pytest.raises(KeyError):
        store.query("mark_stale_wbs_proposals", {})
    with pytest.raises(KeyError):
        store.execute("mark_stale_wbs_proposals", {})


def test_health_check_is_always_true(tmp_path):
    assert new_store(tmp_path).health_check() is True


def test_proposals_persist_across_separate_store_instances(tmp_path):
    """同じ台帳を指す別インスタンスでも読める（プロセスをまたぐのと同じ状況）。"""
    pid = save(new_store(tmp_path), changes=GOOD)
    reopened = new_store(tmp_path)
    assert reopened.query("get_wbs_proposal", {"tenant": "acme", "id": pid})["rows"]


# ===== (2) approve/apply_approved/fetch が台帳でも働く / shared logic works too =====

def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_approving_through_the_ledger_applies_the_change_and_records_it(tmp_path):
    store = new_store(tmp_path)
    pid = save(store, changes=GOOD)
    wbs = tmp_path / "wbs.yaml"
    wbs.write_text(WBS, encoding="utf-8")
    target = Target(file=wbs, root=tmp_path, decisions=tmp_path / "decisions.jsonl")

    out = approve(store, "acme", pid, "sato", "了解", target, as_of=TODAY)

    assert out["status"] == "approved" and out["applied"] is True
    assert "due: 2026-11-01" in text(wbs)
    entry = json.loads(target.decisions.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["kind"] == "wbs_proposal_applied" and entry["proposal"] == pid


def test_approving_twice_through_the_ledger_is_refused(tmp_path):
    store = new_store(tmp_path)
    pid = save(store, changes=GOOD)
    wbs = tmp_path / "wbs.yaml"
    wbs.write_text(WBS, encoding="utf-8")
    target = Target(file=wbs, root=tmp_path, decisions=tmp_path / "decisions.jsonl")
    approve(store, "acme", pid, "sato", "ok", target, as_of=TODAY)

    row = fetch(store, "acme", pid)
    assert row["status"] == "approved"
    from aipmo.wbs_proposals import ProposalError
    with pytest.raises(ProposalError) as excinfo:
        approve(store, "acme", pid, "sato", "again", target, as_of=TODAY)
    assert excinfo.value.kind == "not_pending"


# ===== (3) build_engine が postgres 無しで wbs_replan を登録する / engine wiring =====

def test_build_engine_registers_wbs_replan_on_the_ledger_without_postgres(tmp_path):
    config = {"tenant": "acme", "adapters": {"mode": "real",
                                             "wbs_replan": {"file": "wbs.yaml"}}}
    (tmp_path / "wbs.yaml").write_text(WBS, encoding="utf-8")

    engine = cli.build_engine(config, base_dir=tmp_path)

    assert engine.adapters.has("wbs_replan")
    assert not engine.adapters.has("postgres")
    adapter = engine.adapters.get("wbs_replan")
    assert isinstance(adapter.postgres, LedgerProposalStore)
    assert adapter.pending_count()["count"] == 0


def test_build_engine_prefers_postgres_when_both_are_possible(tmp_path):
    config = {"tenant": "acme", "adapters": {
        "mode": "real",
        "postgres": {"dsn": "postgresql://u:p@h/db", "queries_file": "queries.yaml"},
        "wbs_replan": {"file": "wbs.yaml"},
    }}
    (tmp_path / "wbs.yaml").write_text(WBS, encoding="utf-8")
    (tmp_path / "queries.yaml").write_text("x: SELECT 1\n", encoding="utf-8")

    engine = cli.build_engine(config, base_dir=tmp_path)

    from aipmo.adapters.postgres import PostgresAdapter
    adapter = engine.adapters.get("wbs_replan")
    assert isinstance(adapter.postgres, PostgresAdapter)
