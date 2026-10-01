"""承認したタスクを課題管理ツールにも起票する、のテスト / filing approved tasks into trackers.

確かめること:
  (1) 承認前・却下・見送りは起票の対象にならない（外の世界を変えるので、承認が先）
  (2) 起票で課題が作られ、台帳のタスクに結び付く（id は変わらない）
  (3) 冪等：途中で落ちてやり直しても、課題は二重にならない
  (4) 起票後の収集は、同じ課題を別のタスクとして増やさず、完了も台帳に反映する
  (5) 失敗は台帳に残り、課題は作られず、やり直せる
  (6) 担当は推測しない（アカウントが無ければ担当なしで起票）
  (7) 常駐が承認なしで起票するのは、運用者が auto と書いた由来だけ
  (8) CLI・Web は人の操作として起票できる（閲覧者は不可）

What matters: approval comes first; the issue is created and tied to the ledger task;
filing is idempotent; collection afterwards updates the same task instead of adding a
duplicate; failures are recorded and retryable; no assignee is guessed; the resident files
only origins listed under `auto`; CLI and web are human actions.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from aipmo import cli
from aipmo.adapters.base import Adapter, AdapterRegistry, action
from aipmo.collector import Collector
from aipmo.engine.runner import Engine
from aipmo.filing import (
    FilingConfig,
    FilingConfigError,
    FilingError,
    eligible,
    filing_state,
    idempotency_key,
    load_filing,
    make_filer,
)
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import Member, PmoCore
from aipmo.task_engine import TaskEngine, extract_candidates

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeTracker(Adapter):
    """GitHub 風の、状態を持つ偽トラッカー（冪等キーを守る）。"""
    name = "github_projects"

    def __init__(self) -> None:
        super().__init__()
        self.issues: dict[int, dict[str, Any]] = {}
        self.by_key: dict[str, list[int]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        self.refuse = False                     # 呼びは成功するが、課題を作らない

    @staticmethod
    def shape(issue: dict[str, Any]) -> dict[str, Any]:
        return {"number": issue["number"], "title": issue["title"], "status": issue["state"],
                "assignee": issue.get("assignee"), "labels": issue.get("labels", []),
                "due_date": issue.get("due_date")}

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]],
                      idempotency_key: str | None = None, **params: Any) -> dict[str, Any]:
        self.create_calls.append({"issues": issues, "key": idempotency_key, "params": params})
        if self.fail is not None:
            raise self.fail
        if self.refuse:
            return {"created": [], "count": 0, "failed": [{"issue": "x", "error": "422"}]}
        if idempotency_key and self.by_key.get(idempotency_key):
            return {"created": list(self.by_key[idempotency_key]), "count": 1,
                    "skipped": "already created for this idempotency key"}
        created = []
        for issue in issues:
            number = len(self.issues) + 101
            self.issues[number] = {"number": number, "title": issue["title"], "state": "open",
                                   "assignee": issue.get("assignee"),
                                   "due_date": issue.get("due_date"), "raw": issue}
            created.append(number)
            if idempotency_key:
                self.by_key.setdefault(idempotency_key, []).append(number)
        return {"created": created, "count": len(created)}

    @action()
    def search(self, query: str = "", limit: int = 50) -> dict[str, Any]:
        items = [self.shape(i) for i in self.issues.values() if i["state"] == "open"]
        return {"items": items, "count": len(items)}

    @action()
    def get_issue(self, issue_number: int) -> dict[str, Any]:
        return self.shape(self.issues[issue_number])

    @action(writes=True)
    def update_issue(self, issue_number: int, assignee: str | None = None) -> dict[str, Any]:
        self.updates.append({"issue_number": issue_number, "assignee": assignee})
        return {"issue_number": issue_number}


class FakeJira(Adapter):
    name = "jira"

    def __init__(self) -> None:
        super().__init__()
        self.last: dict[str, Any] = {}

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]], project: str | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        self.last = {"issues": issues, "project": project, "key": idempotency_key}
        return {"created": ["proj-7"], "count": 1}


def registry(*adapters: Adapter) -> AdapterRegistry:
    reg = AdapterRegistry()
    for adapter in adapters:
        reg.register(adapter)
    return reg


CFG = FilingConfig(tracker="github_projects", labels=("pmo-ai",))


def build(tmp_path: Path, tracker: FakeTracker | None = None, *, cfg: FilingConfig | None = CFG,
          members: list[Member] | None = None, acting: bool = False, clock: Clock | None = None):
    clock = clock or Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    tracker = tracker or FakeTracker()
    members = members if members is not None else [Member("ann", accounts=(("github_projects", "ann-gh"),))]
    filer = make_filer(registry(tracker), members, cfg) if cfg is not None else None
    core = PmoCore(task_engine=te, members=members, filing=cfg, filer=filer, acting=acting)
    return te, core, tracker, clock


def make_followup(te: TaskEngine, name="fu1", approved=True, **kw):
    task = te.create_task(f"PMO:fu:{name}", f"対応を決める: {name}", origin="followup",
                          proposed=True, project=kw.pop("project", "aipmo"),
                          priority=kw.pop("priority", "High"), due_date=kw.pop("due_date", "2026-10-05"),
                          generated_from="alert:overdue_severe:A-1", **kw)
    if approved:
        te.decide_proposal(task.id, True)
    return task


def make_recurring(te: TaskEngine, name="weekly", **kw):
    return te.create_task(f"PMO:rec:{name}:2026-W40", f"週次レビュー {name}", origin="recurring",
                          proposed=False, project="aipmo", **kw)


# ===== 設定 / config ==========================================================================

def test_no_section_means_no_filing():
    assert load_filing(None) is None


def test_the_config_is_validated_and_conservative_by_default():
    cfg = load_filing({"tracker": "jira", "params": {"project": "PROJ"}})
    assert cfg is not None and cfg.auto == () and cfg.origins == ("followup", "recurring")
    for bad in ("x", {}, {"tracker": "wbs_file"}, {"tracker": "nope"},
                {"tracker": "jira", "origins": ["judgment"]},
                {"tracker": "jira", "origins": ["followup"], "auto": ["recurring"]},
                {"tracker": "jira", "auto": ["alien"]},
                {"tracker": "jira", "params": {"issues": []}},
                {"tracker": "jira", "params": {"idempotency_key": "x"}},
                {"tracker": "jira", "params": "x"}):
        with pytest.raises(FilingConfigError):
            load_filing(bad)


# ===== (1) 承認が先 / approval comes first =====================================================

def test_only_approved_open_pmo_made_tasks_are_eligible(tmp_path):
    te, core, _, _ = build(tmp_path)
    proposed = make_followup(te, "p", approved=False)
    approved = make_followup(te, "a")
    rejected = make_followup(te, "r", approved=False)
    te.decide_proposal(rejected.id, False)
    recurring = make_recurring(te)
    judgment = te.create_task("PMO:jd:x", "判断", origin="judgment", proposed=False)
    plain = te.ingest("t", "r", [{"key": "A-1", "title": "外から", "assignee": None,
                                  "due_date": None, "priority": None, "status": None,
                                  "blocked": False, "done": False, "labels": []}])
    waiting = {t.id for t in core.filing_candidates()}
    assert waiting == {approved.id, recurring.id}
    assert proposed.id not in waiting and rejected.id not in waiting
    assert judgment.id not in waiting and plain == 1
    assert not eligible(te.tasks[proposed.id], CFG)


def test_filing_a_proposal_that_is_not_yet_approved_is_refused(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te, approved=False)
    with pytest.raises(ValueError):
        core.file_task(task.id)
    assert tracker.create_calls == []


def test_the_filer_itself_refuses_what_is_not_approved_or_not_pmo_made(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    filer = make_filer(registry(tracker), core.members, CFG)
    with pytest.raises(FilingError, match="承認待ち"):
        filer(make_followup(te, approved=False))
    te.ingest("t", "r", [{"key": "A-1", "title": "外から", "assignee": None, "due_date": None,
                          "priority": None, "status": None, "blocked": False, "done": False,
                          "labels": []}])
    with pytest.raises(FilingError, match="PMO Core が作った"):
        filer(te.tasks["JIRA:A-1"])
    assert tracker.create_calls == []


def test_without_the_section_or_a_filer_nothing_can_be_filed(tmp_path):
    te, core, tracker, _ = build(tmp_path, cfg=None)
    task = make_followup(te)
    with pytest.raises(ValueError):
        core.file_task(task.id)
    te2, core2, tracker2, _ = build(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    core2.filer = None                                   # 表示専用の Core
    task2 = make_followup(te2)
    with pytest.raises(FilingError) as err:
        core2.file_task(task2.id)
    assert err.value.kind == "adapter" and tracker2.create_calls == []


# ===== (2) 起票して結び付ける / file and tie ===================================================

def test_filing_creates_the_issue_and_ties_it_to_the_ledger_task(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te, assignee="ann")
    done = core.file_task(task.id)
    (issue,) = tracker.issues.values()
    assert issue["title"] == task.title and issue["due_date"] == "2026-10-05"
    assert issue["assignee"] == "ann-gh"                       # メンバーのアカウント
    raw = issue["raw"]
    assert raw["labels"] == ["pmo-ai"] and task.id in raw["description"]
    assert "alert:overdue_severe:A-1" in raw["description"]
    assert done.id == task.id                                   # 台帳の id は変わらない
    assert (done.tracker, done.external_id) == ("github_projects", "101")
    state = filing_state(done)
    assert state["state"] == "filed" and state["key"] == "GH:101" and state["account"] == "ann-gh"
    kinds = [json.loads(line)["kind"] for line in
             core.decisions_path.read_text(encoding="utf-8").splitlines()]
    assert "task_filed" in kinds
    assert core.filing_candidates() == []                       # 一覧から消える


def test_jira_uses_its_key_and_passes_the_configured_params(tmp_path):
    jira = FakeJira()
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    cfg = FilingConfig(tracker="jira", params={"project": "PROJ"})
    core = PmoCore(task_engine=te, members=[Member("ann")], filing=cfg,
                   filer=make_filer(registry(jira), [Member("ann")], cfg))
    task = make_followup(te, assignee="ann")
    core.file_task(task.id)
    assert jira.last["project"] == "PROJ" and jira.last["key"] == idempotency_key(task)
    assert jira.last["issues"][0]["assignee"] == "ann"          # Jira は名前をアダプタが解決
    assert jira.last["issues"][0]["priority"] == "High"
    done = te.tasks[task.id]
    assert filing_state(done)["key"] == "PROJ-7" and done.external_id == "PROJ-7"


# ===== (3) 冪等 / idempotent ====================================================================

def test_a_retry_after_a_crash_between_create_and_record_does_not_duplicate(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te)
    make_filer(registry(tracker), core.members, CFG)(te.tasks[task.id])   # 作ったが台帳には書けなかった
    assert len(tracker.issues) == 1
    core.file_task(task.id)                                                 # やり直し
    assert len(tracker.issues) == 1
    assert core.last_filing["reused"] is True
    assert filing_state(te.tasks[task.id])["key"] == "GH:101"


def test_filing_twice_is_refused_and_distinct_tasks_get_distinct_keys(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    a, b = make_followup(te, "a"), make_followup(te, "b")
    core.file_task(a.id)
    with pytest.raises(ValueError, match="起票済み"):
        core.file_task(a.id)
    core.file_task(b.id)
    assert idempotency_key(te.tasks[a.id]) != idempotency_key(te.tasks[b.id])
    assert len(tracker.issues) == 2


# ===== (4) 収集との関係 / collection =================================================================

def test_the_scan_updates_the_filed_task_instead_of_adding_a_duplicate(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te, assignee="ann")
    core.file_task(task.id)
    tracker.issues[101]["assignee"] = "ann-gh"
    te.ingest("collector:gh", "r", extract_candidates(
        {"items": [tracker.shape(tracker.issues[101])]}, "github_projects"))
    assert "GH:101" not in te.tasks and len(te.tasks) == 1
    merged = te.tasks[task.id]
    assert merged.assignee == "ann"             # 台帳のメンバー名を、アカウント名で上書きしない
    assert merged.id == task.id and filing_state(merged)["key"] == "GH:101"


def test_an_issue_collection_saw_before_the_record_is_folded_into_the_task(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te)
    filer = make_filer(registry(tracker), core.members, CFG)
    info = filer(te.tasks[task.id])
    te.ingest("collector:gh", "r", extract_candidates(
        {"items": [tracker.shape(tracker.issues[101])]}, "github_projects"))
    assert "GH:101" in te.tasks                                # 先に拾われて別のタスクになった
    te.record_filing(task.id, info)
    assert "GH:101" not in te.tasks and filing_state(te.tasks[task.id])["key"] == "GH:101"


def test_an_issue_closed_in_the_tracker_completes_the_task_through_the_refresh(tmp_path):
    te, core, tracker, clock = build(tmp_path)
    task = make_followup(te)
    core.file_task(task.id)
    collector = Collector(task_engine=te, adapters=registry(tracker), sources=[])
    tracker.issues[101]["state"] = "closed"                     # 閉じられて検索から外れた
    clock.now = NOW + timedelta(days=2)
    report = collector.run_once()
    assert report["refreshed"] == 1 and report["completed"] == 1
    done = te.tasks[task.id]
    assert done.done is True and len(te.tasks) == 1


def test_a_filed_task_cannot_be_completed_in_the_ledger_but_an_unfiled_one_can(tmp_path):
    te, core, _, _ = build(tmp_path)
    filed, plain = make_followup(te, "f"), make_followup(te, "p")
    core.file_task(filed.id)
    with pytest.raises(ValueError, match="GH:101"):
        core.complete_task(filed.id)
    assert core.complete_task(plain.id).done is True


# ===== (5) 失敗 / failure ========================================================================

def test_a_failure_is_recorded_creates_nothing_and_can_be_retried(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te)
    tracker.fail = RuntimeError("503 service unavailable")
    with pytest.raises(FilingError) as err:
        core.file_task(task.id)
    assert err.value.kind == "remote" and not tracker.issues
    state = filing_state(te.tasks[task.id])
    assert state["state"] == "failed" and "503" in state["error"]
    assert [t.id for t in core.filing_candidates()] == [task.id]      # 一覧に残る
    tracker.fail = None
    core.file_task(task.id)
    assert filing_state(te.tasks[task.id])["state"] == "filed"


def test_a_call_that_creates_no_issue_is_a_failure_not_a_success(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te)
    tracker.refuse = True
    with pytest.raises(FilingError, match="作られませんでした"):
        core.file_task(task.id)
    assert filing_state(te.tasks[task.id])["state"] == "failed"
    assert te.tasks[task.id].tracker == ""


def test_a_missing_adapter_is_reported_as_a_configuration_problem(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    core = PmoCore(task_engine=te, filing=CFG, filer=make_filer(registry(), [], CFG))
    task = make_followup(te)
    with pytest.raises(FilingError) as err:
        core.file_task(task.id)
    assert err.value.kind == "adapter"


# ===== (6) 担当は推測しない / no guessed assignee ===================================================

def test_without_an_account_the_issue_is_filed_unassigned_never_to_a_guessed_person(tmp_path):
    members = [Member("bob")]                                  # アカウント未設定
    te, core, tracker, _ = build(tmp_path, members=members)
    task = make_followup(te, assignee="bob")
    core.file_task(task.id)
    assert tracker.issues[101]["assignee"] is None
    assert core.last_filing["unassigned"] == "bob"
    assert te.tasks[task.id].assignee == "bob"                  # 台帳の担当は残る


def test_a_role_ai_is_never_made_the_trackers_assignee(tmp_path):
    members = [Member("dev-ai", kind="agent")]
    te, core, tracker, _ = build(tmp_path, members=members)
    task = make_followup(te, assignee="dev-ai")
    core.file_task(task.id)
    assert tracker.issues[101]["assignee"] is None
    assert core.last_filing["unassigned"] is None


def test_a_filed_task_can_have_its_assignee_written_back_afterwards(tmp_path):
    from aipmo.writeback import make_writer

    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te)
    with te.transaction():
        te.tasks[task.id].suggested_assignee = "ann"
    unfiled = make_writer(registry(tracker), core.members)(te.tasks[task.id], "ann")
    assert unfiled == {"tracker": None, "skipped": "ledger-only"}     # 起票前は台帳だけ
    core.file_task(task.id)
    result = make_writer(registry(tracker), core.members)(te.tasks[task.id], "ann")
    assert result["tracker"] == "github_projects" and tracker.updates[0]["assignee"] == "ann-gh"


# ===== 見送り / declining ===========================================================================

def test_declining_removes_it_from_the_list_and_a_human_can_still_file_it_later(tmp_path):
    te, core, tracker, _ = build(tmp_path)
    task = make_followup(te)
    core.decline_filing(task.id)
    assert core.filing_candidates() == []
    core.file_task(task.id)                                      # 気が変わった
    assert filing_state(te.tasks[task.id])["state"] == "filed"
    with pytest.raises(ValueError):
        core.decline_filing(task.id)                             # 起票済みは見送れない


# ===== (7) 常駐の自動起票 / the resident's auto filing ==============================================

AUTO = FilingConfig(tracker="github_projects", auto=("recurring",), max_auto_per_cycle=2)


def test_the_resident_files_only_origins_listed_under_auto(tmp_path):
    te, core, tracker, _ = build(tmp_path, cfg=AUTO, acting=True)
    rec = make_recurring(te)
    fu = make_followup(te)
    briefing = core.cycle()
    assert filing_state(te.tasks[rec.id])["state"] == "filed"
    assert filing_state(te.tasks[fu.id]) == {}                 # 対応タスクは人の起票待ちのまま
    assert [x["id"] for x in briefing["filing"]["filed_now"]] == [rec.id]
    assert [x["id"] for x in briefing["filing"]["pending"]] == [fu.id]
    assert len(tracker.issues) == 1


def test_nothing_is_auto_filed_by_default_or_by_a_display_only_core(tmp_path):
    te, core, tracker, _ = build(tmp_path, acting=True)          # auto なし
    make_recurring(te)
    briefing = core.cycle()
    assert not tracker.issues and len(briefing["filing"]["pending"]) == 1

    te2, core2, tracker2, _ = build(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path,
                                    cfg=AUTO, acting=False)
    make_recurring(te2)
    core2.cycle()
    assert not tracker2.issues


def test_auto_filing_is_capped_per_cycle_and_a_fresh_failure_waits_an_hour(tmp_path):
    te, core, tracker, clock = build(tmp_path, cfg=AUTO, acting=True)
    for name in "abc":
        make_recurring(te, name)
    core.cycle()
    assert len(tracker.issues) == 2                              # 上限 2
    core.cycle()
    assert len(tracker.issues) == 3

    te2, core2, tracker2, clock2 = build(tmp_path / "f" if (tmp_path / "f").mkdir() is None else tmp_path,
                                         cfg=AUTO, acting=True)
    make_recurring(te2, "z")
    tracker2.fail = RuntimeError("down")
    briefing = core2.cycle()
    assert len(briefing["filing"]["failed_now"]) == 1 and len(tracker2.create_calls) == 1
    core2.cycle()
    assert len(tracker2.create_calls) == 1                       # すぐには再試行しない
    clock2.now = NOW + timedelta(hours=2)
    tracker2.fail = None
    core2.cycle()
    assert len(tracker2.issues) == 1


# ===== 表示 / scoping =======================================================================================

def test_scoping_a_briefing_narrows_filing_and_hides_it_from_confined_viewers(tmp_path):
    from aipmo.pmo_core import scope_briefing

    te, core, _, _ = build(tmp_path)
    make_followup(te, "a", project="alpha")
    make_followup(te, "b", project="beta")
    briefing = core.cycle()
    assert len(briefing["filing"]["pending"]) == 2
    only = scope_briefing(briefing, [], {"alpha"}, redact_org=False)
    assert [p["project"] for p in only["filing"]["pending"]] == ["alpha"]
    assert scope_briefing(briefing, [], {"alpha"}, redact_org=True)["filing"] is None


# ===== (8) CLI と Web / human actions ===========================================================================

def cli_setup(tmp_path, monkeypatch, tracker: FakeTracker, members_yaml="") -> Path:
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry(tracker), llms))
    config = tmp_path / "config.yaml"
    config.write_text(
        "pmo_core:\n  members:\n    - name: ann\n      accounts: {github_projects: ann-gh}\n"
        "  filing:\n    tracker: github_projects\n    labels: [pmo-ai]\n", encoding="utf-8")
    return config


def test_cli_lists_by_default_files_only_with_apply_and_can_skip(tmp_path, monkeypatch, capsys):
    tracker = FakeTracker()
    config = cli_setup(tmp_path, monkeypatch, tracker)
    te = TaskEngine(tmp_path / "task-ledger.db")
    a, b = make_followup(te, "a", assignee="ann"), make_followup(te, "b")
    te.close()

    assert cli.main(["--config", str(config), "file"]) == 0
    out = capsys.readouterr().out
    assert a.id in out and b.id in out and not tracker.issues         # 一覧だけ。何も作らない

    assert cli.main(["--config", str(config), "file", a.id]) == 1       # --apply なしでは作らない
    assert not tracker.issues
    assert cli.main(["--config", str(config), "file", a.id, "--apply"]) == 0
    assert "GH:101" in capsys.readouterr().out and len(tracker.issues) == 1
    assert cli.main(["--config", str(config), "file", b.id, "--skip"]) == 0
    assert cli.main(["--config", str(config), "file", "--all", "--apply"]) == 0
    assert len(tracker.issues) == 1                                     # 見送ったものは作らない
    assert cli.main(["--config", str(config), "file", a.id, "--apply"]) == 1   # 二重にしない
    assert len(tracker.issues) == 1


def test_cli_all_reports_failures_and_keeps_going(tmp_path, monkeypatch, capsys):
    tracker = FakeTracker()
    config = cli_setup(tmp_path, monkeypatch, tracker)
    te = TaskEngine(tmp_path / "task-ledger.db")
    make_followup(te, "a")
    te.close()
    tracker.fail = RuntimeError("down")
    assert cli.main(["--config", str(config), "file", "--all", "--apply"]) == 1
    assert "✗" in capsys.readouterr().err
    tracker.fail = None
    assert cli.main(["--config", str(config), "file", "--all", "--apply"]) == 0
    assert len(tracker.issues) == 1


def test_cli_without_the_section_says_so(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("pmo_core:\n  members: [ann]\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "file"]) == 0
    assert "pmo_core.filing" in capsys.readouterr().out


def test_cli_rejects_a_bad_section(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("pmo_core:\n  filing: {tracker: nope}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "file"]) == 1
    assert "pmo_core.filing" in capsys.readouterr().err


def test_a_resident_core_requires_the_trackers_adapter_to_exist(tmp_path):
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    te = TaskEngine(tmp_path / "task-ledger.db")
    config = {"pmo_core": {"filing": {"tracker": "github_projects"}}}
    engine = Engine(registry(), llms)
    with pytest.raises(cli.ConfigError, match="github_projects"):
        cli.build_pmo_core(config, te, engine, launch_base=tmp_path)
    shown = cli.build_pmo_core(config, te)                              # 表示専用は読めればよい
    assert shown.filing is not None and shown.filer is None


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "operator-token-1", "viewer-token-2"


def web(tmp_path: Path, tracker: FakeTracker):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    a = make_followup(te, "a", assignee="ann", project="alpha")
    b = make_followup(te, "b", project="beta")
    te.close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(registry(tracker), llms), tmp_path / "t", OPERATOR,
                     viewer_token=VIEWER, lang="en", store=RunStore(),
                     pmo_ledger=tmp_path / "task-ledger.db", filing=CFG,
                     members=[Member("ann", accounts=(("github_projects", "ann-gh"),))],
                     viewer_projects=["alpha"])
    return TestClient(app), a, b


def headers(token):
    return {"x-aipmo-token": token}


def test_web_lists_files_and_skips_for_the_operator_only(tmp_path):
    tracker = FakeTracker()
    client, a, b = web(tmp_path, tracker)
    view = client.get("/api/pmo", headers=headers(OPERATOR)).json()
    assert {p["id"] for p in view["filing"]["pending"]} == {a.id, b.id}
    assert view["filing"]["can_file"] is True and view["filing"]["tracker"] == "github_projects"

    # 閲覧者は見ることも、起票することもできない
    assert client.get("/api/pmo", headers=headers(VIEWER)).json()["filing"] is None
    denied = client.post("/api/pmo/filing", json={"ref": a.id}, headers=headers(VIEWER))
    assert denied.status_code in (401, 403) and not tracker.issues

    ok = client.post("/api/pmo/filing", json={"ref": a.id, "decision": "file"},
                     headers=headers(OPERATOR))
    assert ok.status_code == 200 and ok.json()["key"] == "GH:101"
    assert len(tracker.issues) == 1 and tracker.issues[101]["assignee"] == "ann-gh"
    assert client.post("/api/pmo/filing", json={"ref": a.id}, headers=headers(OPERATOR)
                       ).status_code == 409                          # 二重にしない
    assert client.post("/api/pmo/filing", json={"ref": b.id, "decision": "skip"},
                       headers=headers(OPERATOR)).status_code == 200
    after = client.get("/api/pmo", headers=headers(OPERATOR)).json()
    assert after["filing"]["pending"] == []                          # 押した直後に消える
    assert client.post("/api/pmo/filing", json={"ref": "nope"}, headers=headers(OPERATOR)
                       ).status_code == 404
    assert client.post("/api/pmo/filing", json={"ref": a.id, "decision": "x"},
                       headers=headers(OPERATOR)).status_code == 422


def test_web_reports_tracker_failures_without_creating_anything(tmp_path):
    tracker = FakeTracker()
    client, a, _ = web(tmp_path, tracker)
    tracker.fail = RuntimeError("503")
    response = client.post("/api/pmo/filing", json={"ref": a.id}, headers=headers(OPERATOR))
    assert response.status_code == 502 and not tracker.issues
    pending = client.get("/api/pmo", headers=headers(OPERATOR)).json()["filing"]["pending"]
    assert any(p["id"] == a.id and p["state"] == "failed" for p in pending)
