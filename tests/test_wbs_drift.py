"""WBS の更新漏れ・証拠の欠けを、対応タスクの提案にする、のテスト。

確かめること:
  (1) 設定の検証（既定は無効。codes は決まったものだけ）
  (2) 「終わっていそうなのに未完了」「完了なのに証拠が無い」が、承認待ちの提案になる
  (3) 同じ問題で提案を重ねない（冪等）。却下は同じ回では出し直さない
  (4) 直ったら、承認待ちの提案は取り下げる。承認・却下が済んだものは残す
  (5) 直ってからまた起きたら、新しい回として出る
  (6) WBS が読めないときは、足しも取り下げもしない
  (7) WBS ファイルは決して書き換えない。確認は間引かれる
  (8) 承認前は仕事にならない。承認すれば普通のタスクで、起票にも回せる

What matters: drift becomes pending proposals (never work until approved); idempotent per
episode; a rejection sticks for the episode; a fixed WBS withdraws only still-pending
proposals; a returning problem is a new episode; an unreadable WBS changes nothing; the file
is never written.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.filing import FilingConfig, eligible
from aipmo.generation import GenerationConfig, GenerationError, WbsWatch, load_generation
from aipmo.pmo_core import PmoCore
from aipmo.task_engine import TaskEngine

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)

HEAD = """\
wbs:
  id: proj
  name: テスト
  nodes:
"""


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def leaf(id_, name, status="todo", evidence=(), **extra):
    lines = [f'    - id: "{id_}"', f'      name: "{name}"', f"      status: {status}"]
    if status == "done":
        lines.append("      done_on: 2026-09-01")
    for key, value in extra.items():
        lines.append(f"      {key}: {value}")
    if evidence:
        lines.append("      evidence:")
        lines += [f'        - "{e}"' for e in evidence]
    return "\n".join(lines) + "\n"


def write_wbs(tmp_path: Path, *leaves: str) -> Path:
    path = tmp_path / "wbs.yaml"
    path.write_text(HEAD + "".join(leaves), encoding="utf-8")
    return path


def build(tmp_path: Path, **watch_kw):
    clock = Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    watch = WbsWatch(file=str(tmp_path / "wbs.yaml"), root=str(tmp_path), **watch_kw)
    core = PmoCore(task_engine=te, generation=GenerationConfig(wbs=watch))
    return te, core, clock


def proposals(te):
    return [t for t in te.tasks.values() if t.id.startswith("PMO:wb:")]


def kinds(core):
    return [json.loads(line)["kind"] for line in
            core.decisions_path.read_text(encoding="utf-8").splitlines()]


# ===== (1) 設定 / config ======================================================================

def test_it_is_off_by_default_and_the_config_is_validated():
    assert load_generation({}).wbs is None
    assert load_generation({"wbs": False}).wbs is None
    watch = load_generation({"wbs": True}).wbs
    assert watch is not None and watch.codes == ("maybe_done", "done_without_evidence",
                                                  "evidence_missing")
    custom = load_generation({"wbs": {"file": "a.yaml", "codes": ["overdue"], "interval_minutes": 5,
                                      "priority": "High", "project": "x"}}).wbs
    assert custom is not None and custom.codes == ("overdue",) and custom.interval_minutes == 5
    for bad in ({"wbs": "x"}, {"wbs": {"codes": ["nope"]}}, {"wbs": {"codes": []}}):
        with pytest.raises(GenerationError):
            load_generation(bad)


# ===== (2) 提案になる / becomes proposals ======================================================

def test_drift_becomes_pending_proposals_that_are_not_work_yet(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path,
              leaf("1", "終わっていそう", evidence=["a.py"]),                     # maybe_done
              leaf("2", "証拠が無い完了", status="done"),                          # done_without_evidence
              leaf("3", "証拠が消えた完了", status="done", evidence=["gone.py"]),  # evidence_missing
              leaf("4", "ふつうの未完了"))
    te, core, _ = build(tmp_path)
    briefing = core.cycle()
    found = proposals(te)
    assert {t.generated_from for t in found} == {
        "wbs:maybe_done:1", "wbs:done_without_evidence:2", "wbs:evidence_missing:3"}
    assert all(t.proposed and t.origin == "followup" and t.project == "proj" for t in found)
    assert all(t.title.startswith("WBS を確かめる:") for t in found)
    assert "wbs" in found[0].labels
    assert briefing["wbs_drift"]["found"] == 3 and len(briefing["wbs_drift"]["created"]) == 3
    assert not any(t.id in {x["id"] for x in briefing["top_priorities"]} for t in found)  # 仕事ではない
    assert kinds(core).count("task_generated") == 3


def test_notifications_go_out_for_each_new_proposal(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    te, core, _ = build(tmp_path)
    sent: list[str] = []
    core.notify = sent.append
    core.cycle()
    assert len(sent) == 1 and "承認待ち" in sent[0]


# ===== (3) 冪等・却下 / idempotent, rejection ===================================================

def test_the_same_problem_never_raises_a_second_proposal(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    te, core, clock = build(tmp_path)
    core.cycle()
    for hours in (2, 5, 30):
        clock.now = NOW + timedelta(hours=hours)
        core.cycle()
    assert len(proposals(te)) == 1


def test_a_rejection_sticks_for_that_episode_and_survives_a_restart(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    te, core, clock = build(tmp_path)
    core.cycle()
    (task,) = proposals(te)
    core.decide_proposal(task.id, False)
    clock.now = NOW + timedelta(hours=3)
    core.cycle()
    assert len(proposals(te)) == 1 and te.tasks[task.id].status == "Rejected"

    te2, core2, clock2 = build(tmp_path)                       # 再起動(状態ファイルから)
    clock2.now = NOW + timedelta(hours=6)
    core2.cycle()
    assert len(proposals(te2)) == 1


# ===== (4)(5) 直ったら / when it is fixed ========================================================

def test_a_fixed_wbs_withdraws_pending_proposals_but_keeps_decided_ones(tmp_path):
    for name in "ab":
        (tmp_path / f"{name}.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path,
              leaf("1", "未決", evidence=["a.py"]),
              leaf("2", "承認済み", evidence=["b.py"]))
    te, core, clock = build(tmp_path)
    core.cycle()
    by_node = {t.generated_from: t for t in proposals(te)}
    approved = by_node["wbs:maybe_done:2"]
    core.decide_proposal(approved.id, True)

    write_wbs(tmp_path, leaf("1", "未決", status="done", evidence=["a.py"]),
              leaf("2", "承認済み", status="done", evidence=["b.py"]))     # どちらも更新された
    clock.now = NOW + timedelta(hours=2)
    briefing = core.cycle()
    left = {t.generated_from for t in proposals(te)}
    assert left == {"wbs:maybe_done:2"}                          # 未決は取り下げ、承認済みは残る
    assert briefing["wbs_drift"]["withdrawn"] == [by_node["wbs:maybe_done:1"].id]
    assert "proposal_withdrawn" in kinds(core)


def test_a_problem_that_returns_after_a_fix_is_a_new_episode(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    te, core, clock = build(tmp_path)
    core.cycle()
    (first,) = proposals(te)
    core.decide_proposal(first.id, False)                        # 却下した
    write_wbs(tmp_path, leaf("1", "終わっていそう", status="done", evidence=["a.py"]))
    clock.now = NOW + timedelta(hours=2)
    core.cycle()
    (tmp_path / "a.py").unlink()                                 # 証拠が消えて完了が嘘になった
    clock.now = NOW + timedelta(hours=4)
    core.cycle()
    now_ids = {t.id: t.generated_from for t in proposals(te)}
    assert first.id in now_ids                                   # 却下の記録は残る
    assert "wbs:evidence_missing:1" in now_ids.values() and len(now_ids) == 2


# ===== (6) 読めないとき / unreadable ===================================================================

def test_an_unreadable_wbs_neither_adds_nor_withdraws(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    path = write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    te, core, clock = build(tmp_path)
    core.cycle()
    assert len(proposals(te)) == 1
    path.write_text("wbs: [broken", encoding="utf-8")           # 壊れた
    clock.now = NOW + timedelta(hours=2)
    report = core.cycle()["wbs_drift"]
    assert report["error"] and len(proposals(te)) == 1           # 「直った」と誤解して取り下げない
    path.unlink()                                                # 消えた
    clock.now = NOW + timedelta(hours=4)
    assert core.cycle()["wbs_drift"]["error"] and len(proposals(te)) == 1


# ===== (7) 読むだけ・間引き / read-only, throttled =========================================================

def test_the_wbs_file_is_never_written_and_checks_are_throttled(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    path = write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    before = path.read_bytes()
    te, core, clock = build(tmp_path, interval_minutes=60)
    first = core.cycle()["wbs_drift"]
    clock.now = NOW + timedelta(minutes=10)
    again = core.cycle()["wbs_drift"]
    assert again["checked_at"] == first["checked_at"]            # 間引かれた
    clock.now = NOW + timedelta(minutes=61)
    assert core.cycle()["wbs_drift"]["checked_at"] != first["checked_at"]
    assert path.read_bytes() == before


def test_without_the_section_nothing_is_read_or_made(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    briefing = PmoCore(task_engine=te).cycle()
    assert briefing["wbs_drift"] is None and proposals(te) == []


def test_only_the_chosen_codes_are_proposed(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]),
              leaf("2", "期限切れ", due="2026-09-01"))
    te, core, _ = build(tmp_path, codes=("overdue",))
    core.cycle()
    assert [t.generated_from for t in proposals(te)] == ["wbs:overdue:2"]


# ===== (8) 承認後 / after approval ===========================================================================

def test_an_approved_proposal_is_ordinary_work_and_can_be_filed(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    write_wbs(tmp_path, leaf("1", "終わっていそう", evidence=["a.py"]))
    te, core, _ = build(tmp_path)
    core.cycle()
    (task,) = proposals(te)
    cfg = FilingConfig(tracker="github_projects")
    assert not eligible(te.tasks[task.id], cfg)                  # 承認前は起票の対象でもない
    core.decide_proposal(task.id, True)
    assert eligible(te.tasks[task.id], cfg)
    assert core.complete_task(task.id).done is True


def test_the_cli_resolves_the_wbs_relative_to_the_config_directory(tmp_path, capsys):
    (tmp_path / "ev.py").write_text("x", encoding="utf-8")
    (tmp_path / "wbs").mkdir()
    (tmp_path / "wbs" / "p.yaml").write_text(
        HEAD + leaf("1", "終わっていそう", evidence=["ev.py"]), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("pmo_core:\n  generate:\n    wbs: {file: wbs/p.yaml}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "pmo"]) == 0
    out = capsys.readouterr().out
    assert "WBS の更新漏れ" in out and "新たに提案 1 件" in out
    assert cli.main(["--config", str(config), "generated"]) == 0
    assert "WBS を確かめる: 1 終わっていそう" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("pmo_core:\n  generate:\n    wbs: {codes: [nope]}\n", encoding="utf-8")
    assert cli.main(["--config", str(bad), "pmo"]) == 1


def test_the_same_problem_returning_is_proposed_again_even_after_a_rejection(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    todo = leaf("1", "終わっていそう", evidence=["a.py"])
    write_wbs(tmp_path, todo)
    te, core, clock = build(tmp_path)
    core.cycle()
    (first,) = proposals(te)
    core.decide_proposal(first.id, False)
    write_wbs(tmp_path, leaf("1", "終わっていそう", status="done", evidence=["a.py"]))
    clock.now = NOW + timedelta(hours=2)
    core.cycle()                                                 # 直った
    write_wbs(tmp_path, todo)                                    # また未更新に戻った
    clock.now = NOW + timedelta(hours=4)
    core.cycle()
    found = proposals(te)
    assert len(found) == 2 and sum(1 for t in found if t.proposed) == 1
