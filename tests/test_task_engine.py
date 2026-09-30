"""Task Engine のテスト / cross-template task engine tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aipmo.adapters.base import AdapterRegistry
from aipmo.dsl import loader
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.task_engine import TaskEngine, extract_candidates

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def make(tmp_path, now=NOW, **kw) -> TaskEngine:
    return TaskEngine(tmp_path / "ledger.json", now=lambda: now, **kw)


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False}
    return {**base, **kw}


def test_extract_reads_items_and_skips_junk():
    out = {"items": [{"key": "p-1", "summary": "A", "labels": ["Blocked"]},
                     {"foo": 1}, "x", {"title": "B", "status": "Done"}]}
    got = extract_candidates(out)
    assert [c["title"] for c in got] == ["A", "B"]
    assert got[0]["key"] == "P-1" and got[0]["blocked"]
    assert got[1]["done"]
    assert extract_candidates("text") == [] and extract_candidates({"count": 1}) == []


def test_same_key_from_two_templates_merges_and_ranks_higher(tmp_path):
    te = make(tmp_path)
    te.ingest("a", "r1", [cand(key="P-1", title="Fix", priority="Medium"),
                          cand(key="P-2", title="Other", priority="Medium")])
    te.ingest("b", "r2", [cand(key="P-1", title="Fix", priority="High",
                               due_date="2026-09-20", assignee="sato")])
    ranked = te.ranked()
    assert [t.key for t in ranked] == ["P-1", "P-2"]
    top = ranked[0]
    assert top.templates == ["a", "b"] and top.priority == "High"
    assert top.assignee == "sato"
    assert any("超過" in r for r in top.reasons)


def test_keyless_todo_is_linked_to_keyed_task_by_title(tmp_path):
    te = make(tmp_path)
    te.ingest("minutes", "r1", [cand(title="  Write  Report ")])
    te.ingest("overdue", "r2", [cand(key="P-9", title="write report")])
    assert len(te.tasks) == 1
    assert list(te.tasks)[0] == "JIRA:P-9"
    assert te.ranked()[0].templates == ["minutes", "overdue"]


def test_earliest_due_wins_and_done_leaves_ranking(tmp_path):
    te = make(tmp_path)
    te.ingest("a", "r1", [cand(key="P-1", title="x", due_date="2026-10-10")])
    te.ingest("b", "r2", [cand(key="P-1", title="x", due_date="2026-10-01")])
    assert te.tasks["JIRA:P-1"].due_date == "2026-10-01"
    te.ingest("c", "r3", [cand(key="P-1", title="x", done=True)])
    assert te.ranked() == []


def test_ranking_moves_with_time_without_any_run(tmp_path):
    clock = {"now": NOW}
    te = TaskEngine(tmp_path / "l.json", now=lambda: clock["now"])
    te.ingest("a", "r", [cand(key="P-1", title="x", priority="Low", due_date="2026-10-05")])
    before = te.ranked()[0].score
    clock["now"] = NOW + timedelta(days=10)
    te.refresh()
    assert te.ranked()[0].score > before


def test_ledger_survives_restart_and_stale_tasks_drop(tmp_path):
    te = make(tmp_path)
    te.ingest("a", "r", [cand(key="P-1", title="x")])
    again = make(tmp_path)
    assert "JIRA:P-1" in again.tasks
    later = make(tmp_path, now=NOW + timedelta(days=40))
    later.refresh()
    assert later.tasks == {}


def test_ranking_is_deterministic_on_ties(tmp_path):
    te = make(tmp_path)
    te.ingest("a", "r", [cand(key="P-2", title="b"), cand(key="P-1", title="a")])
    assert [t.key for t in te.ranked()] == ["P-1", "P-2"]


def test_engine_hook_collects_after_run_and_survives_listener_failure(tmp_path):
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    engine = Engine(AdapterRegistry(), llms,
                    transforms={"emit": lambda **kw: kw})
    te = make(tmp_path)
    te.attach(engine)
    engine.run_listeners.insert(0, lambda name, ctx: 1 / 0)

    template = loader.load_dict({"name": "emit", "steps": [
        {"id": "mk", "expression": "emit",
         "inputs": {"items": [{"key": "p-5", "summary": "Ship it",
                               "priority": "High"}]}},
    ]})
    engine.run(template)  # must not raise even though a listener does
    assert [t.key for t in te.ranked()] == ["P-5"]
