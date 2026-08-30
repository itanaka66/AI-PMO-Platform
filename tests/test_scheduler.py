"""cron とスケジューラのテスト / cron and scheduler tests.

外部に依存しないので、時計を差し替えて実際の挙動を確かめられる。
Nothing external is involved, so the clock is swapped out and the real
behaviour is exercised.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.mock import MockJiraAdapter, MockSlackAdapter
from aipmo.engine.cron import CronError, next_fire, parse
from aipmo.engine.runner import Engine
from aipmo.engine.scheduler import Job, Scheduler, State, discover_jobs
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry

TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")


def utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def fires(expression: str, after: datetime, tz=TOKYO) -> datetime:
    result = next_fire(parse(expression), after, tz)
    assert result is not None, f"{expression} が起動しません"
    return result.astimezone(tz)


# ===== cron の解釈 / parsing ================================================

def test_field_count_is_enforced():
    with pytest.raises(CronError, match="5"):
        parse("0 9 * *")


@pytest.mark.parametrize("expression", [
    "60 * * * *",      # 分は 0-59
    "* 24 * * *",      # 時は 0-23
    "* * 32 * *",      # 日は 1-31
    "* * * 13 *",      # 月は 1-12
    "* * * * 8",       # 曜日は 0-7
])
def test_out_of_range_values_are_refused(expression):
    with pytest.raises(CronError):
        parse(expression)


def test_unreadable_token_is_refused():
    with pytest.raises(CronError):
        parse("0 9 * * FUNDAY")


def test_weekday_names_are_accepted():
    assert parse("0 9 * * MON-FRI").weekdays == frozenset({1, 2, 3, 4, 5})


def test_month_names_are_accepted():
    assert parse("0 0 1 JAN,JUL *").months == frozenset({1, 7})


def test_both_zero_and_seven_mean_sunday():
    assert parse("0 9 * * 0").weekdays == parse("0 9 * * 7").weekdays


def test_steps_are_expanded():
    assert parse("*/15 * * * *").minutes == frozenset({0, 15, 30, 45})


def test_ranges_with_steps():
    assert parse("0 9-17/4 * * *").hours == frozenset({9, 13, 17})


# ===== 次回時刻 / next occurrence ===========================================

def test_weekday_schedule_skips_the_weekend():
    # 2026-08-28 は金曜 / a Friday
    assert fires("0 9 * * MON-FRI", utc(2026, 8, 28, 3)) .strftime("%Y-%m-%d") \
        == "2026-08-31"     # 次は月曜 / the following Monday


def test_next_occurrence_is_strictly_after():
    """同じ分に二度当たらないこと。二重実行の元になる。"""
    now = datetime(2026, 8, 28, 9, 0, tzinfo=TOKYO)
    assert fires("0 9 * * *", now).day == 29


def test_day_and_weekday_together_means_either():
    """cron の歴史的な挙動。直感に反するが、揃えてある。

    「毎月1日と、毎週月曜」を1行で書けるのはこの規則のため。
    """
    cron = parse("0 9 1 * MON")
    # 2026-09-01 は火曜。曜日は外れるが日付が当たるので起動する。
    assert cron.matches(datetime(2026, 9, 1, 9, 0, tzinfo=TOKYO))
    # 2026-09-07 は月曜。日付は外れるが曜日が当たる。
    assert cron.matches(datetime(2026, 9, 7, 9, 0, tzinfo=TOKYO))
    # どちらも外れる日は起動しない。
    assert not cron.matches(datetime(2026, 9, 2, 9, 0, tzinfo=TOKYO))


def test_day_only_does_not_become_or():
    cron = parse("0 9 1 * *")
    assert not cron.matches(datetime(2026, 9, 7, 9, 0, tzinfo=TOKYO))


def test_leap_day_resolves_years_ahead():
    """4年に一度しか来ない。1年で探索を打ち切ると永久に起動しない。

    Cutting the search off at a year would allow a schedule that can be
    written but never fires.
    """
    assert fires("0 3 29 2 *", utc(2026, 8, 27)).strftime("%Y-%m-%d") == "2028-02-29"


@pytest.mark.parametrize("zone", ["Asia/Kolkata", "Asia/Kathmandu"])
def test_zones_offset_by_partial_hours_do_not_skip_midnight(zone):
    """UTC との差が 30 分や 45 分の地域で、現地 0 時台を飛ばさないこと。"""
    tz = ZoneInfo(zone)
    assert fires("15 0 * * *", utc(2026, 8, 27, 12), tz).strftime("%H:%M") == "00:15"


def test_spring_forward_skips_a_nonexistent_local_time():
    """夏時間で飛ばされた時刻は、その日は起動しない。

    2026-03-08 の米東部は 02:00 が存在しない。
    """
    result = next_fire(parse("30 2 * * *"), utc(2026, 3, 8, 0), NEW_YORK)
    local = result.astimezone(NEW_YORK)
    assert local.strftime("%Y-%m-%d") == "2026-03-09"


def test_search_is_fast_enough_for_rare_schedules():
    """1分刻みのままだと4年先の探索で数百万回になる。"""
    import time as clock

    started = clock.monotonic()
    fires("0 3 29 2 *", utc(2026, 8, 27))
    assert clock.monotonic() - started < 0.5


# ===== ジョブの収集 / discovery =============================================

SCHEDULED = """
name: daily_report
trigger: "schedule:0 9 * * MON-FRI"
steps:
  - id: look
    adapter: jira
    action: find_overdue
    inputs: { project: PROJ }
"""

MANUAL = """
name: on_demand
steps:
  - id: look
    adapter: jira
    action: find_overdue
    inputs: { project: PROJ }
"""

BAD_CRON = """
name: broken_schedule
trigger: "schedule:0 99 * * *"
steps:
  - id: look
    adapter: jira
    action: find_overdue
    inputs: { project: PROJ }
"""


@pytest.fixture
def templates(tmp_path):
    root = tmp_path / "templates"
    root.mkdir()
    (root / "daily.yaml").write_text(SCHEDULED, encoding="utf-8")
    (root / "manual.yaml").write_text(MANUAL, encoding="utf-8")
    (root / "broken.yaml").write_text(BAD_CRON, encoding="utf-8")
    return root


def test_only_scheduled_templates_become_jobs(templates):
    jobs, _ = discover_jobs(templates)
    assert [job.name for job in jobs] == ["daily_report"]


def test_an_invalid_cron_is_reported_not_silently_dropped(templates):
    """起動しない理由が分からないのが一番困る。"""
    _, problems = discover_jobs(templates)
    assert any("broken.yaml" in problem for problem in problems)


# ===== スケジューラ / the scheduler =========================================

class Clock:
    def __init__(self, start: datetime):
        self.now = start
        self.slept = 0.0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def build(tmp_path, jobs, start, llm=None):
    adapters = AdapterRegistry()
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", llm or EchoProvider())

    clock = Clock(start)
    scheduler = Scheduler(
        Engine(adapters, llms), jobs, State(path=tmp_path / "state.json"),
        now=clock, sleep=clock.sleep, jitter=False,
    )
    return scheduler, clock


def job_from(templates: str, path) -> Job:
    from aipmo.dsl import loader

    (path / "j.yaml").write_text(templates, encoding="utf-8")
    jobs, _ = discover_jobs(path)
    return jobs[0]


def test_nothing_runs_before_its_time(tmp_path, templates):
    jobs, _ = discover_jobs(templates)
    scheduler, _ = build(tmp_path, jobs, utc(2026, 8, 28, 0, 0))   # 09:00 JST
    assert scheduler.tick() == []


def test_a_job_runs_when_due(tmp_path, templates):
    jobs, _ = discover_jobs(templates)
    scheduler, clock = build(tmp_path, jobs, utc(2026, 8, 27, 23, 0))
    clock.advance(hours=2)                                          # past 09:00 JST

    results = scheduler.tick()
    assert results == [{"job": "daily_report", "status": "success"}]


def test_a_job_does_not_run_twice_for_one_occurrence(tmp_path, templates):
    jobs, _ = discover_jobs(templates)
    scheduler, clock = build(tmp_path, jobs, utc(2026, 8, 27, 23, 0))
    clock.advance(hours=2)

    scheduler.tick()
    assert scheduler.tick() == []


def test_missed_runs_are_not_replayed(tmp_path, templates):
    """5日分の報告を正午にまとめて送っても、それは通知の洪水であって報告ではない。

    Five days of a 9am report delivered at noon is a flood, not a report.
    """
    jobs, _ = discover_jobs(templates)
    scheduler, clock = build(tmp_path, jobs, utc(2026, 8, 27, 23, 0))

    clock.advance(days=5)          # 止まっていた / the machine was off
    results = scheduler.tick()

    assert len(results) == 1       # まとめ流しをしない / not five runs


def test_state_survives_a_restart(tmp_path, templates):
    jobs, _ = discover_jobs(templates)
    first, clock = build(tmp_path, jobs, utc(2026, 8, 27, 23, 0))
    clock.advance(hours=2)
    first.tick()

    # 再起動 / restart with a fresh scheduler over the same state file
    reloaded, _ = discover_jobs(templates)
    second = Scheduler(first.engine, reloaded,
                       State.load(tmp_path / "state.json"),
                       now=clock, sleep=clock.sleep, jitter=False)

    assert second.jobs[0].last_run is not None
    assert second.tick() == []     # 直近の予定を繰り返さない


def test_unreadable_state_does_not_prevent_startup(tmp_path, templates):
    (tmp_path / "state.json").write_text("{ not json", encoding="utf-8")
    jobs, _ = discover_jobs(templates)
    scheduler = Scheduler(
        build(tmp_path, jobs, utc(2026, 8, 27))[0].engine, jobs,
        State.load(tmp_path / "state.json"),
        now=lambda: utc(2026, 8, 27), jitter=False)
    assert scheduler.jobs[0].next_run is not None


def test_an_overlapping_run_is_skipped(tmp_path, templates):
    """処理が遅れているだけで、同じ課題が二重に起票されては困る。"""
    jobs, _ = discover_jobs(templates)
    scheduler, clock = build(tmp_path, jobs, utc(2026, 8, 27, 23, 0))
    clock.advance(hours=2)

    scheduler.jobs[0].running = True
    result = scheduler.tick()

    assert result[0]["status"] == "skipped_overlap"


def test_one_failing_template_does_not_stop_the_others(tmp_path):
    """1つの失敗でスケジューラ全体が止まると、他が全部動かなくなる。"""
    from aipmo.dsl import loader

    failing = loader.load_dict({
        "name": "will_fail", "trigger": "schedule:* * * * *",
        "steps": [{"id": "boom", "adapter": "jira", "action": "no_such_action"}],
    })
    healthy = loader.load_dict({
        "name": "is_fine", "trigger": "schedule:* * * * *",
        "steps": [{"id": "ok", "adapter": "jira", "action": "find_overdue",
                   "inputs": {"project": "PROJ"}}],
    })

    jobs = [
        Job(template=failing, path=tmp_path / "a.yaml",
            cron_expression="* * * * *", timezone_name="Asia/Tokyo"),
        Job(template=healthy, path=tmp_path / "b.yaml",
            cron_expression="* * * * *", timezone_name="Asia/Tokyo"),
    ]
    scheduler, clock = build(tmp_path, jobs, utc(2026, 8, 27, 0, 0))
    clock.advance(minutes=2)

    results = scheduler.tick()
    statuses = {item["job"]: item["status"] for item in results}

    assert statuses["will_fail"] == "failed"
    assert statuses["is_fine"] == "success"


def test_a_failed_job_is_rescheduled(tmp_path):
    from aipmo.dsl import loader

    failing = loader.load_dict({
        "name": "will_fail", "trigger": "schedule:* * * * *",
        "steps": [{"id": "boom", "adapter": "jira", "action": "no_such_action"}],
    })
    job = Job(template=failing, path=tmp_path / "a.yaml",
              cron_expression="* * * * *", timezone_name="Asia/Tokyo")
    scheduler, clock = build(tmp_path, [job], utc(2026, 8, 27, 0, 0))
    clock.advance(minutes=2)
    scheduler.tick()

    assert job.next_run is not None
    assert job.consecutive_failures == 1


def test_due_jobs_in_one_tick_actually_overlap_in_time(tmp_path):
    """1本が長く塞がっていても、他が待たされないこと。

    Slack 承認待ちのような長い待ちが1本にあっても、同じ tick の他のジョブは
    それを待たずに走る、という改善のコア部分。実際に重なって走ることを
    タイミングで確かめる（似た趣旨のテストが並列ステップ実行にもある）。
    """
    import threading
    import time

    from aipmo.dsl import loader

    overlapped = threading.Event()
    entered = threading.Barrier(2, timeout=2)

    class SlowEngine(Engine):
        def run(self, template, params=None, trigger=None):
            try:
                entered.wait()
                overlapped.set()
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.05)
            return super().run(template, params, trigger)

    slow = loader.load_dict({
        "name": "slow_job", "trigger": "schedule:* * * * *",
        "steps": [{"id": "ok", "adapter": "jira", "action": "find_overdue",
                   "inputs": {"project": "PROJ"}}],
    })
    fast = loader.load_dict({
        "name": "fast_job", "trigger": "schedule:* * * * *",
        "steps": [{"id": "ok", "adapter": "jira", "action": "find_overdue",
                   "inputs": {"project": "PROJ"}}],
    })
    jobs = [
        Job(template=slow, path=tmp_path / "a.yaml",
            cron_expression="* * * * *", timezone_name="Asia/Tokyo"),
        Job(template=fast, path=tmp_path / "b.yaml",
            cron_expression="* * * * *", timezone_name="Asia/Tokyo"),
    ]

    adapters = AdapterRegistry()
    adapters.register(MockJiraAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    clock = Clock(utc(2026, 8, 27, 0, 0))
    scheduler = Scheduler(SlowEngine(adapters, llms), jobs,
                          State(path=tmp_path / "s.json"),
                          now=clock, sleep=clock.sleep, jitter=False)
    clock.advance(minutes=2)

    results = scheduler.tick()

    assert overlapped.is_set()
    statuses = {item["job"]: item["status"] for item in results}
    assert statuses == {"slow_job": "success", "fast_job": "success"}


def test_concurrent_completions_do_not_corrupt_state(tmp_path):
    """複数ジョブが同時に終わって state を書いても、両方とも記録されること。"""
    from aipmo.dsl import loader

    jobs = []
    for i in range(6):
        template = loader.load_dict({
            "name": f"job_{i}", "trigger": "schedule:* * * * *",
            "steps": [{"id": "ok", "adapter": "jira", "action": "find_overdue",
                       "inputs": {"project": "PROJ"}}],
        })
        jobs.append(Job(template=template, path=tmp_path / f"{i}.yaml",
                        cron_expression="* * * * *", timezone_name="Asia/Tokyo"))

    scheduler, clock = build(tmp_path, jobs, utc(2026, 8, 27, 0, 0))
    clock.advance(minutes=2)

    results = scheduler.tick()

    assert {item["status"] for item in results} == {"success"}
    assert len(scheduler.state.last_runs) == 6
    reloaded = State.load(tmp_path / "state.json")
    assert len(reloaded.last_runs) == 6


def test_the_trigger_carries_the_scheduled_time(tmp_path, templates):
    """テンプレートから「いつの予定か」を参照できること。"""
    from aipmo.dsl import loader

    seen = {}

    class Recording(Engine):
        def run(self, template, params=None, trigger=None):
            seen.update(trigger or {})
            return super().run(template, params, trigger)

    jobs, _ = discover_jobs(templates)
    adapters = AdapterRegistry()
    adapters.register(MockJiraAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    clock = Clock(utc(2026, 8, 27, 23, 0))
    scheduler = Scheduler(Recording(adapters, llms), jobs,
                          State(path=tmp_path / "s.json"),
                          now=clock, sleep=clock.sleep, jitter=False)
    clock.advance(hours=2)
    scheduler.tick()

    assert seen["type"] == "schedule"
    assert seen["scheduled_for"]
