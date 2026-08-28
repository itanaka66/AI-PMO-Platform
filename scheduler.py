"""定時実行 / the scheduler.

テンプレートの `trigger: "schedule:..."` を実際に起動させる。
これが無いと、定時起動のテンプレートは書けるのに動きません。

Runs templates that declare `trigger: "schedule:..."`. Without this they can be
written but never fire.

止まっていた間の扱い / What happens to missed runs
--------------------------------------------------
機械が止まる。ノート PC が閉じられる。コンテナが再起動する。
そのとき、逃した実行をあとから流すべきか。

**流しません。** 毎朝9時の報告を、正午に5日分まとめて送っても意味がない。
それは通知の洪水であって、報告ではない。逃したことは記録し、次の予定から
再開します。

Machines stop; laptops close; containers restart. Should the runs that were
missed be replayed?

**They are not.** Five days of a 9am report delivered at noon is a flood of
notifications, not a report. The misses are recorded and the schedule resumes
at its next occurrence.

同時実行の扱い / Overlap
-------------------------
前回がまだ終わっていないうちに次の時刻が来ることがある。
このとき二重に走らせません。会議記録の処理が遅れているだけなのに、
同じ課題が二重に起票されては困る。

If the previous run has not finished when the next time arrives, it is not
started again. A transcript that is merely slow should not produce the same
Jira issues twice.
"""
from __future__ import annotations

import json
import logging
import random
import signal
import threading
import time as clock
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..dsl import loader
from ..dsl.schema import Template
from .cron import CronError, next_fire, parse
from .runner import Engine, StepFailure

logger = logging.getLogger("aipmo.scheduler")

# 同時刻に並ぶテンプレートを少しずらす。9:00 に5本あると、
# 同じ瞬間に外部 API へ集中して絞られる。
# Templates sharing a time are staggered: five at 9:00 would hit the same
# external API in the same instant and get throttled.
MAX_JITTER_SECONDS = 20


@dataclass
class Job:
    template: Template
    path: Path
    cron_expression: str
    timezone_name: str
    next_run: datetime | None = None
    last_run: datetime | None = None
    running: bool = False
    consecutive_failures: int = 0

    @property
    def name(self) -> str:
        return self.template.name

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


@dataclass
class State:
    """最後に走った時刻の記録 / when each job last ran.

    再起動しても同じ実行を繰り返さないために要る。持たないと、
    コンテナが再起動するたびに直近の予定をもう一度走らせてしまう。

    Needed so a restart does not repeat work: without it, every container
    restart would run the most recent occurrence again.
    """

    path: Path
    last_runs: dict[str, str] = field(default_factory=dict)
    missed: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 状態が読めなくても止めない。次の予定から始めればよい。
            # An unreadable state file is not fatal: resume from the next
            # occurrence rather than refusing to start.
            return cls(path=path)
        return cls(path=path,
                   last_runs=dict(data.get("last_runs") or {}),
                   missed=dict(data.get("missed") or {}))

    def save(self) -> None:
        payload = {"last_runs": self.last_runs, "missed": self.missed}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 書き途中で落ちても壊れないよう、置き換えで書く。
            # Written by replacement so a crash mid-write cannot corrupt it.
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            logger.warning("状態を保存できません / cannot save state: %s", exc)

    def get(self, name: str) -> datetime | None:
        stamp = self.last_runs.get(name)
        return datetime.fromisoformat(stamp) if stamp else None

    def record(self, name: str, moment: datetime) -> None:
        self.last_runs[name] = moment.isoformat()
        self.save()


def discover_jobs(root: Path) -> tuple[list[Job], list[str]]:
    """定時起動のテンプレートを集める / collect scheduled templates.

    読めなかったものは黙って捨てず、理由とともに返す。
    起動しない理由が分からないのが一番困る。

    Unreadable templates are returned with their reason rather than dropped:
    not knowing why something never runs is the worst outcome.
    """
    jobs: list[Job] = []
    problems: list[str] = []

    for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
        try:
            template = loader.load_file(path)
        except loader.TemplateError as exc:
            problems.append(f"{path.name}: {exc}")
            continue

        if template.trigger.type != "schedule" or not template.trigger.cron:
            continue

        try:
            parse(template.trigger.cron)
        except CronError as exc:
            problems.append(f"{path.name}: {exc}")
            continue

        jobs.append(Job(template=template, path=path,
                        cron_expression=template.trigger.cron,
                        timezone_name=template.trigger.timezone))

    return jobs, problems


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        jobs: list[Job],
        state: State,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = clock.sleep,
        jitter: bool = True,
    ) -> None:
        self.engine = engine
        self.jobs = jobs
        self.state = state
        self.now = now
        self.sleep = sleep
        self.jitter = jitter
        self._stopping = threading.Event()

        start = self.now()
        for job in self.jobs:
            job.last_run = self.state.get(job.name)
            job.next_run = self._next(job, start)

    # -- 予定の計算 / scheduling ------------------------------------------

    def _next(self, job: Job, after: datetime) -> datetime | None:
        try:
            return next_fire(parse(job.cron_expression), after, job.tz())
        except CronError as exc:
            logger.error("%s: %s", job.name, exc)
            return None

    def due(self, moment: datetime) -> list[Job]:
        return [
            job for job in self.jobs
            if job.next_run is not None and job.next_run <= moment
        ]

    # -- 実行 / running -----------------------------------------------------

    def run_job(self, job: Job, moment: datetime) -> dict[str, Any]:
        if job.running:
            # 前回がまだ終わっていない。重ねて走らせない。
            # The previous run has not finished; do not overlap.
            logger.warning("%s: 前回が実行中のため見送り / still running, skipped",
                           job.name)
            self.state.missed[job.name] = self.state.missed.get(job.name, 0) + 1
            job.next_run = self._next(job, moment)
            return {"job": job.name, "status": "skipped_overlap"}

        job.running = True
        started = self.now()
        try:
            self.engine.run(job.template, trigger={
                "type": "schedule",
                "scheduled_for": job.next_run.isoformat() if job.next_run else None,
            })
            job.consecutive_failures = 0
            outcome = {"job": job.name, "status": "success"}
        except StepFailure as exc:
            # 1つのテンプレートの失敗で、スケジューラ全体を止めない。
            # 他のテンプレートは動き続ける必要がある。
            # One template's failure must not stop the scheduler; the others
            # still need to run.
            job.consecutive_failures += 1
            logger.error("%s: 失敗 / failed (%d回連続): %s",
                         job.name, job.consecutive_failures, exc)
            outcome = {"job": job.name, "status": "failed", "error": str(exc)}
        except Exception as exc:
            job.consecutive_failures += 1
            logger.exception("%s: 予期しないエラー / unexpected error", job.name)
            outcome = {"job": job.name, "status": "failed",
                       "error": f"{type(exc).__name__}: {exc}"}
        finally:
            job.running = False
            job.last_run = started
            self.state.record(job.name, started)
            job.next_run = self._next(job, self.now())

        return outcome

    # -- 常駐 / the loop ----------------------------------------------------

    def tick(self) -> list[dict[str, Any]]:
        """1回ぶんの判定と実行 / one pass of checking and running."""
        moment = self.now()
        results = []
        for job in self.due(moment):
            if self.jitter and len(self.due(moment)) > 1:
                self.sleep(random.uniform(0, MAX_JITTER_SECONDS))
            results.append(self.run_job(job, moment))
        return results

    def run_forever(self, interval: float = 20.0) -> None:
        self._install_signals()
        self._report_plan()

        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                # ループ自体は決して落とさない。落ちたら誰も何も動かない。
                # The loop itself never dies: if it does, nothing runs at all.
                logger.exception("スケジューラのループでエラー / error in the loop")
            self._stopping.wait(interval)

        logger.info("停止しました / stopped")

    def stop(self) -> None:
        self._stopping.set()

    def _install_signals(self) -> None:
        def handle(signum, frame):
            # 実行中のテンプレートは最後まで走らせる。途中で切ると、
            # 課題は起票されたのに通知されていない、といった半端が残る。
            # A running template finishes: cutting it short leaves half-states,
            # such as issues filed but nobody told.
            logger.info("停止要求を受けました。実行中の処理を待ちます "
                        "/ shutting down after the current run")
            self.stop()

        for name in ("SIGTERM", "SIGINT"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), handle)
                except ValueError:
                    pass  # 別スレッドでは設定できない / not the main thread

    def _report_plan(self) -> None:
        logger.info("%d 件の定時テンプレート / %d scheduled templates",
                    len(self.jobs), len(self.jobs))
        for job in self.jobs:
            when = (job.next_run.astimezone(job.tz()).strftime("%Y-%m-%d %H:%M %Z")
                    if job.next_run else "なし / never")
            logger.info("  %-28s %s  (%s)", job.name, when, job.cron_expression)
