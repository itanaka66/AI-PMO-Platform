"""PMO AI 自身の開発を WBS で管理する運用のテスト / self-managed WBS tests.

確かめるのは、(1) 構造の誤りが error になること、(2) 「完了」が証拠で裏付けられる
こと（申告だけでは通らない）、(3) 証拠のパスでリポジトリの外を読めないこと、
(4) 速度・予測・クリティカルパスが決定論的で、根拠が無いときは予測しないこと、
(5) 出力が Task Engine に取り込まれること、(6) このリポジトリ自身の WBS が有効なこと。

(1) structural mistakes are errors; (2) "done" rests on evidence, not a claim;
(3) evidence paths cannot read outside the repository; (4) speed, forecast and
critical path are deterministic, and nothing is forecast without a basis;
(5) the output reaches the Task Engine; (6) this repository's own WBS is valid.
"""
from __future__ import annotations

import json
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.adapters.base import AdapterError, AdapterRegistry
from aipmo.adapters.mock import MockSlackAdapter
from aipmo.adapters.wbs_file import WbsFileAdapter
from aipmo.dsl import loader
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import Member, PmoCore
from aipmo.task_engine import TaskEngine, extract_candidates
from aipmo.wbs import WbsError, analyse, check_evidence, load_wbs, velocity

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 1)


def write_wbs(tmp_path: Path, body: str, name: str = "wbs.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def codes(problems, level=None):
    return sorted(p.code for p in problems if level is None or p.level == level)


def result_codes(analysis, level=None):
    return sorted(p["code"] for p in analysis["problems"]
                  if level is None or p["level"] == level)


def run(tmp_path: Path, body: str, as_of: date = TODAY):
    path = write_wbs(tmp_path, body)
    wbs, problems = load_wbs(path)
    return wbs, analyse(wbs, tmp_path, as_of, problems)


HEAD = "wbs:\n  id: demo\n  name: Demo\n  nodes:\n"


# ===== (1) 構造 / structure ====================================================

def test_a_valid_wbs_loads_without_problems(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    _, a = run(tmp_path, HEAD + """\
    - id: "1"
      name: Group
      children:
        - id: "1.1"
          name: One
          status: done
          effort: 2
          evidence: [a.py]
        - id: "1.2"
          name: Two
          effort: 3
          depends_on: ["1.1"]
""")
    assert a["problems"] == [] and a["summary"]["leaves"] == 2


@pytest.mark.parametrize("body,code", [
    ('- {id: "1", name: A}\n    - {id: "1", name: B}', "duplicate_id"),
    ('- {id: "1", name: A, depends_on: ["9"]}', "unknown_dependency"),
    ('- {id: "1", name: A, depends_on: ["1"]}', "self_dependency"),
    ('- {id: "1", name: A, status: finished}', "bad_status"),
    ('- {id: "1", name: A, effort: -1}', "bad_effort"),
    ('- {id: "1", name: A, effort: true}', "bad_effort"),
    ('- {id: "1", name: A, due: "not-a-date"}', "bad_date"),
    ('- {id: "1", name: ""}', "empty_name"),
    ("- {id: 1.10, name: A}", "bad_id"),
])
def test_structural_mistakes_are_errors(tmp_path, body, code):
    _, a = run(tmp_path, HEAD + "    " + body + "\n")
    assert code in result_codes(a, "error")


def test_dependency_cycles_and_dependencies_on_parents_are_errors(tmp_path):
    _, a = run(tmp_path, HEAD + """\
    - id: "1"
      name: P
      children:
        - {id: "1.1", name: A, depends_on: ["1.2"]}
        - {id: "1.2", name: B, depends_on: ["1.1"]}
    - {id: "2", name: C, depends_on: ["1"]}
""")
    errors = result_codes(a, "error")
    assert "dependency_cycle" in errors and "dependency_on_parent_node" in errors


def test_structural_errors_stop_the_forecast_rather_than_invent_one(tmp_path):
    # 速度があるので、構造に誤りが無ければ予測は出るはず
    body = HEAD.replace("name: Demo", "name: Demo\n  velocity_per_day: 1") + """\
    - {id: "1", name: A, effort: 2, depends_on: ["1"]}
    - {id: "2", name: B, effort: 3}
"""
    _, a = run(tmp_path, body)
    assert a["error_count"] >= 1 and a["forecast"] is None and a["velocity"]["per_day"] == 1
    assert a["critical_path"] == []


def test_unreadable_files_are_a_clear_error(tmp_path):
    with pytest.raises(WbsError, match="読めません"):
        load_wbs(tmp_path / "missing.yaml")
    with pytest.raises(WbsError, match="YAML"):
        load_wbs(write_wbs(tmp_path, "wbs: [unclosed"))
    with pytest.raises(WbsError, match="wbs"):
        load_wbs(write_wbs(tmp_path, "other: 1"))


def test_parent_nodes_fields_are_flagged_as_ignored(tmp_path):
    _, a = run(tmp_path, HEAD + """\
    - id: "1"
      name: P
      status: done
      effort: 5
      children:
        - {id: "1.1", name: A}
""")
    assert "parent_has_leaf_fields" in result_codes(a, "warning")


# ===== (2) 完了は証拠で裏付ける / done rests on evidence ===========================

def test_a_done_node_needs_its_evidence(tmp_path):
    (tmp_path / "present.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
    _, a = run(tmp_path, HEAD + """\
    - {id: "1", name: Claimed only, status: done}
    - {id: "2", name: File missing, status: done, evidence: [gone.py]}
    - {id: "3", name: File present, status: done, evidence: [present.py]}
    - {id: "4", name: Phrase present, status: done, evidence: ["present.py::def feature"]}
    - {id: "5", name: Phrase absent, status: done, evidence: ["present.py::def other"]}
""")
    by_node = {(p["node"], p["code"]) for p in a["problems"]}
    assert ("1", "done_without_evidence") in by_node            # 警告: 確かめられない
    assert ("2", "evidence_missing") in by_node                 # error
    assert ("5", "evidence_missing") in by_node                 # error（語句が無い）
    assert not any(n in ("3", "4") for n, _ in by_node)
    assert a["error_count"] == 2


def test_evidence_that_is_all_there_but_not_marked_done_is_pointed_out(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    _, a = run(tmp_path, HEAD + """\
    - {id: "1", name: Maybe done, status: in_progress, effort: 1, evidence: [a.py]}
    - {id: "2", name: Not yet, effort: 1, evidence: [a.py, b.py]}
""")
    flagged = {(p["node"], p["code"]) for p in a["problems"]}
    assert ("1", "maybe_done") in flagged and ("2", "maybe_done") not in flagged


def test_other_warnings(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    wbs, a = run(tmp_path, """\
wbs:
  id: demo
  name: Demo
  deadline: 2026-09-01
  nodes:
    - {id: "1", name: Open, effort: 1, due: 2026-09-15}
    - {id: "2", name: No estimate}
    - {id: "3", name: Done early, status: done, evidence: [a.py], depends_on: ["1"]}
""")
    warnings = result_codes(a, "warning")
    assert {"overdue", "unestimated", "done_before_dependency", "deadline_passed"} <= set(warnings)
    assert a["summary"]["unestimated"] == ["2"]


# ===== (3) 証拠のパスは外へ出られない / evidence cannot escape the root ============

@pytest.mark.parametrize("spec", [
    "../secret.txt", "a/../../secret.txt", "/etc/passwd", "C:\\Windows\\win.ini",
    "\\\\server\\share\\x", "..\\secret.txt",
])
def test_evidence_cannot_point_outside_the_repository(tmp_path, spec):
    (tmp_path.parent / "secret.txt").write_text("top secret", encoding="utf-8")
    ok, why = check_evidence(spec, tmp_path)
    assert ok is False and why
    _, a = run(tmp_path, HEAD + f'    - {{id: "1", name: A, status: done, evidence: ["{spec}"]}}\n'
               .replace("\\", "\\\\"))
    assert "bad_evidence_path" in result_codes(a, "error")


def test_a_phrase_check_reads_inside_the_root_only_and_globs_stay_inside(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "one.py").write_text("alpha", encoding="utf-8")
    (tmp_path.parent / "outside.py").write_text("alpha", encoding="utf-8")
    assert check_evidence("pkg/*.py::alpha", tmp_path)[0] is True
    assert check_evidence("pkg/*.py::beta", tmp_path)[0] is False
    assert check_evidence("../*.py", tmp_path)[0] is False


# ===== (4) 速度・予測・クリティカルパス / speed, forecast, critical path ===========

SPEED = HEAD.replace("id: demo", "id: demo\n  velocity_window_days: 10") + """\
    - {id: "1", name: Old, status: done, effort: 8, done_on: 2026-09-01, evidence: []}
    - {id: "2", name: Recent A, status: done, effort: 3, done_on: 2026-09-25, evidence: []}
    - {id: "3", name: Recent B, status: done, effort: 2, done_on: 2026-10-01, evidence: []}
    - {id: "4", name: Open, effort: 10}
"""


def test_speed_comes_from_recent_completions_only(tmp_path):
    wbs, _ = run(tmp_path, SPEED)
    speed = velocity(wbs, TODAY)
    assert speed["basis"] == "history" and speed["samples"] == 2
    assert speed["per_day"] == pytest.approx(0.5)          # (3+2) / 10 日。古い 8 は数えない


def test_the_window_edge_is_exclusive_at_the_start_and_inclusive_at_today(tmp_path):
    wbs, _ = run(tmp_path, SPEED.replace("2026-09-25", "2026-09-21"))      # ちょうど 10 日前
    assert velocity(wbs, TODAY)["samples"] == 1                             # 10 日前は含めない


def test_a_declared_speed_is_used_only_without_history(tmp_path):
    body = """\
wbs:
  id: demo
  name: Demo
  velocity_per_day: 2
  nodes:
    - {id: "1", name: Open, effort: 10}
"""
    wbs, a = run(tmp_path, body)
    assert a["velocity"]["basis"] == "declared" and a["velocity"]["per_day"] == 2
    assert a["forecast"]["projected_days_needed"] == 5 and a["forecast"]["drift_days"] is None


def test_without_any_basis_nothing_is_forecast(tmp_path):
    _, a = run(tmp_path, HEAD + '    - {id: "1", name: Open, effort: 5}\n')
    assert a["velocity"]["per_day"] is None and a["forecast"] is None
    assert "予測はしません" in a["summary_text"]


def test_drift_against_a_deadline_is_signed(tmp_path):
    body = SPEED.replace("nodes:", "deadline: 2026-10-11\n  nodes:", 1)
    _, a = run(tmp_path, body)
    forecast = a["forecast"]
    assert forecast["days_to_deadline"] == 10
    assert forecast["projected_days_needed"] == pytest.approx(20)
    assert forecast["drift_days"] == pytest.approx(10)       # 正 = 遅れ
    assert "+10 日" in a["summary_text"]


def test_dependencies_decide_what_is_ready_and_the_critical_path(tmp_path):
    _, a = run(tmp_path, HEAD + """\
    - {id: "1", name: Base, status: done, effort: 1}
    - {id: "2", name: Ready low, effort: 2, depends_on: ["1"], priority: Low}
    - {id: "3", name: Ready high, effort: 3, depends_on: ["1"], priority: High}
    - {id: "4", name: Waits, effort: 5, depends_on: ["2", "3"]}
    - {id: "5", name: Stuck, effort: 1, status: blocked}
""")
    assert [r["id"] for r in a["ready"]] == ["3", "2"]                # 優先度順。待ち・ブロックは除く
    assert a["blocked_by_dependency"] == ["4"]
    assert a["critical_path"][-1] == "4" and a["critical_path_effort"] == 8
    assert "1" not in a["critical_path"]                              # 完了は出さない


def test_rollups_count_by_effort(tmp_path):
    _, a = run(tmp_path, HEAD + """\
    - id: "1"
      name: Group
      children:
        - {id: "1.1", name: Big done, status: done, effort: 9}
        - {id: "1.2", name: Small open, effort: 1}
""")
    (group,) = a["nodes"]
    assert group["percent"] == 90 and group["status"] == "in_progress"
    assert a["summary"]["percent_by_effort"] == 90 and a["summary"]["percent_by_count"] == 50


# ===== (5) アダプタ・テンプレート・Task Engine / adapter, template, ledger ========

def seed_repo(tmp_path: Path) -> Path:
    (tmp_path / "src.py").write_text("ok", encoding="utf-8")
    (tmp_path / "wbs").mkdir()
    path = tmp_path / "wbs" / "aipmo.yaml"
    path.write_text(textwrap.dedent("""\
        wbs:
          id: aipmo
          name: Demo project
          nodes:
            - id: "1"
              name: Core
              children:
                - {id: "1.1", name: Built, status: done, effort: 3, done_on: 2026-09-30, evidence: [src.py]}
                - {id: "1.2", name: Next, effort: 2, depends_on: ["1.1"], priority: High, due: 2026-10-10}
                - {id: "1.3", name: Later, effort: 5, owner: ann}
        """), encoding="utf-8")
    return path


def test_the_adapter_reports_status_and_check_and_is_read_only(tmp_path):
    seed_repo(tmp_path)
    adapter = WbsFileAdapter(root=str(tmp_path))
    status = adapter.invoke("status", {"as_of": "2026-10-01"})
    assert status["summary"]["done"] == 1 and status["error_count"] == 0
    assert status["summary_text"].startswith("*Demo project")
    assert adapter.invoke("check", {})["ok"] is True
    assert not any(adapter.writes(name) for name in adapter.actions())
    assert adapter.health_check() is True


@pytest.mark.parametrize("path", ["../outside.yaml", "/etc/hosts", "wbs/aipmo.txt",
                                  "src.py"])
def test_the_adapter_reads_only_yaml_under_its_root(tmp_path, path):
    seed_repo(tmp_path)
    (tmp_path.parent / "outside.yaml").write_text("wbs: {id: x, nodes: []}", encoding="utf-8")
    with pytest.raises(AdapterError):
        WbsFileAdapter(root=str(tmp_path)).invoke("status", {"path": path})


def test_the_adapter_reports_a_broken_file_and_a_bad_date_clearly(tmp_path):
    adapter = WbsFileAdapter(root=str(tmp_path), file="x.yaml")
    (tmp_path / "x.yaml").write_text("wbs: [", encoding="utf-8")
    with pytest.raises(AdapterError, match="YAML"):
        adapter.invoke("status", {})
    seed_repo(tmp_path)
    with pytest.raises(AdapterError, match="as_of"):
        WbsFileAdapter(root=str(tmp_path)).invoke("status", {"as_of": "tomorrow"})


def test_wbs_nodes_enter_the_task_engine_with_their_project_and_status(tmp_path):
    seed_repo(tmp_path)
    status = WbsFileAdapter(root=str(tmp_path)).invoke("status", {"as_of": "2026-10-01"})
    candidates = {c["key"]: c for c in extract_candidates(status, "wbs_file")}
    assert set(candidates) == {"WBS:1.1", "WBS:1.2", "WBS:1.3"}
    nxt = candidates["WBS:1.2"]
    assert (nxt["tracker"], nxt["external_id"], nxt["project"]) == ("wbs_file", "1.2", "aipmo")
    assert nxt["priority"] == "High" and nxt["due_date"] == "2026-10-10"
    assert nxt["status"] == "To Do" and nxt["done"] is False
    assert candidates["WBS:1.1"]["done"] is True
    assert candidates["WBS:1.3"]["assignee"] == "ann"


def test_the_weekly_template_reports_and_feeds_the_ledger(tmp_path):
    seed_repo(tmp_path)
    adapters = AdapterRegistry()
    adapters.register(WbsFileAdapter(root=str(tmp_path)))
    slack = MockSlackAdapter()
    adapters.register(slack)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    engine = Engine(adapters, llms)
    ledger = TaskEngine(tmp_path / "task-ledger.db", now=lambda: datetime(
        2026, 10, 1, 9, tzinfo=timezone.utc))
    ledger.attach(engine)

    template = loader.load_file(ROOT / "templates" / "examples" / "self_development.yaml")
    assert template.trigger.type == "schedule" and template.trigger.cron == "0 9 * * MON"
    engine.run(template)

    assert slack.posted[0]["channel"] == "#aipmo-dev"
    assert "Demo project" in slack.posted[0]["text"] and "1/3 件完了" in slack.posted[0]["text"]
    assert sorted(ledger.tasks) == ["WBS:1.1", "WBS:1.2", "WBS:1.3"]
    assert {t.project for t in ledger.tasks.values()} == {"aipmo"}

    # 未完了の作業は、他のタスクと同じ台帳で順位付けされ、担当の提案が出る
    core = PmoCore(task_engine=ledger, members=[Member("ann"), Member("bob")])
    proposals = core.cycle()["assignment_proposals"]
    assert {p["task"] for p in proposals} == {"WBS:1.2"}              # 1.3 は担当済み
    assert [t.id for t in ledger.ranked()][0] == "WBS:1.2"            # 優先度 High・期限あり


def test_a_node_finishing_between_weeks_is_recorded_as_an_outcome(tmp_path):
    path = seed_repo(tmp_path)
    adapter = WbsFileAdapter(root=str(tmp_path))
    clock = {"now": datetime(2026, 10, 1, 9, tzinfo=timezone.utc)}
    ledger = TaskEngine(tmp_path / "task-ledger.db", now=lambda: clock["now"])
    ledger.ingest("self_development", "w1", extract_candidates(
        adapter.invoke("status", {"as_of": "2026-10-01"}), "wbs_file"))

    (tmp_path / "next.py").write_text("ok", encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8").replace(
        '{id: "1.2", name: Next, effort: 2,',
        '{id: "1.2", name: Next, status: done, evidence: [next.py], effort: 2,'),
        encoding="utf-8")
    clock["now"] = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)     # 期限 10/10 を 4 日過ぎて
    ledger.ingest("self_development", "w2", extract_candidates(
        adapter.invoke("status", {"as_of": "2026-10-14"}), "wbs_file"))
    (outcome,) = ledger.outcomes
    assert outcome["task"] == "WBS:1.2" and outcome["late_days"] == 4


# ===== CLI =========================================================================

def test_cli_check_status_and_exit_codes(tmp_path, capsys, monkeypatch):
    path = seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["wbs", "check"]) == 0
    assert "1/3 件完了" in capsys.readouterr().out

    assert cli.main(["wbs", "status", "--as-of", "2026-10-01"]) == 0
    out = capsys.readouterr().out
    assert "Demo project" in out and "*" not in out                  # 端末向けに装飾を除く
    assert cli.main(["wbs", "status", "--json", "--as-of", "2026-10-01"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["leaves"] == 3 and "items" not in data

    # 証拠を壊すと error → 終了コード 1
    path.write_text(path.read_text(encoding="utf-8").replace("src.py", "vanished.py"),
                    encoding="utf-8")
    assert cli.main(["wbs", "check"]) == 1
    assert "evidence_missing" in capsys.readouterr().out


def test_cli_strict_turns_warnings_into_failures_and_bad_input_is_reported(
        tmp_path, capsys, monkeypatch):
    path = seed_repo(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace(", priority: High", "")
                    + '        - {id: "1.4", name: No estimate}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["wbs", "check"]) == 0                           # warning だけなら成功
    assert cli.main(["wbs", "check", "--strict"]) == 1
    capsys.readouterr()
    assert cli.main(["wbs", "check", "nope.yaml"]) == 1
    assert "nope.yaml" in capsys.readouterr().err
    assert cli.main(["wbs", "status", "--as-of", "yesterday"]) == 1


# ===== (6) このリポジトリ自身の WBS / this repository's own WBS ======================

def test_this_projects_own_wbs_is_valid():
    """PMO AI 自身の WBS。error が無く、すべての完了に証拠がある。

    CI の `aipmo wbs check` と同じ検証をテストでも行う。完了と書いた作業の
    証拠が消えた（ファイルを消した・名前を変えた）とき、ここで落ちる。
    """
    wbs, problems = load_wbs(ROOT / "wbs" / "aipmo.yaml")
    analysis = analyse(wbs, ROOT, TODAY, problems)
    errors = [p for p in analysis["problems"] if p["level"] == "error"]
    assert errors == [], errors

    done = [leaf for leaf in wbs.leaves() if leaf.done]
    assert done, "完了した作業が一件も無い"
    unproven = [leaf.id for leaf in done if not leaf.evidence]
    assert unproven == [], f"証拠の無い完了: {unproven}"
    assert not [p for p in analysis["problems"] if p["code"] == "done_before_dependency"]


def test_this_projects_own_wbs_tells_the_truth_about_what_is_left():
    wbs, problems = load_wbs(ROOT / "wbs" / "aipmo.yaml")
    analysis = analyse(wbs, ROOT, TODAY, problems)
    open_ids = {leaf.id for leaf in wbs.leaves() if not leaf.done}
    # 既知の未実装・未検証が WBS に載っている（READMEの「未実装」と食い違わない）
    assert {"6.1", "6.3", "6.10"} <= open_ids
    assert analysis["summary"]["leaves"] >= 30 and analysis["ready"]
    # 実績から速度が出る（根拠のある予測）
    assert analysis["velocity"]["basis"] in ("history", "declared", "none")
