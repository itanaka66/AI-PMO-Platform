"""進捗の自動収集 — 課題管理ツールの今の状態を、台帳へ集める。

これまで台帳が知るのは、**他のテンプレートが出力した** items だけだった。
どのテンプレートも一覧に出さなくなった課題 — 典型的には、完了して検索条件
（`statusCategory != Done` など）から外れた課題 — は、台帳の中では「未完了のまま
止まっている」ことになり、完了も、そこから得られる実績（遅れ・実日数）も観測できない。

収集役はそれを埋める。定期的に（常駐の周に乗せて）次の二つを行う。

  1. **収集元の走査** … `pmo_core.collect.sources` に書いた課題管理ツールの検索を
     走らせ、見つけた課題を台帳に取り込む（新しい課題、状態・担当・期限の変化）。
  2. **既知タスクの再読み込み** … 台帳にある未完了のタスクのうち、1 で現れなかった
     ものを、課題管理ツールから 1 件ずつ読み直す。閉じられた課題はここで「完了」と
     分かり、実績として残る。

**読み取り専用。** 呼べるのは読み取りのアクションだけで、書き込み系（`writes=True`）は
設定に書かれていても拒否する。課題管理ツールを変えるのは、これまでどおり人が確定した
操作（担当の書き戻しなど）だけ。

失敗は止まる理由にしない。収集元が 1 つ落ちても、ほかの収集元と再読み込みは続け、
結果に理由を残す。読み直せなかったタスクは `last_seen` が更新されないので、
長く読めないままなら、既存の期限切れ（30 日）で台帳から外れる。

Automatic progress collection. The ledger used to know only what *other
templates* output; an issue no template lists any more — typically one that
finished and left a `statusCategory != Done` query — stayed "open forever" in
the ledger, and its outcome (lateness, actual days) was never observed. The
collector, run periodically from the resident cycle, (1) scans the configured
sources and ingests what it finds, and (2) re-reads each open ledger task the
scan did not return, one by one, so a closed issue is seen to be done.

Read-only: a write action is refused even if the config names it. A failing
source never stops the rest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import AdapterRegistry
from .task_engine import Task, TaskEngine, extract_candidates
from .trackers import TRACKERS, tracker_of

logger = logging.getLogger("aipmo.collector")

# Jira の「key in (...)」に一度に入れる件数 / keys per Jira `key in (...)` query
JIRA_BATCH = 40


class CollectError(ValueError):
    """収集の設定が不正 / a malformed collect config."""


@dataclass(frozen=True)
class Source:
    id: str
    adapter: str
    action: str = "search"
    params: dict[str, Any] = field(default_factory=dict)
    project: str | None = None


def load_sources(raw: list[dict[str, Any]] | None) -> list[Source]:
    sources, seen = [], set()
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("adapter"):
            raise CollectError("収集元には id と adapter が必要です "
                               "/ each source needs an id and an adapter")
        source_id = str(item["id"])
        if source_id in seen:
            raise CollectError(f"収集元の id が重複しています: {source_id}")
        seen.add(source_id)
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise CollectError(f"収集元 '{source_id}': params はマッピング")
        sources.append(Source(
            id=source_id, adapter=str(item["adapter"]),
            action=str(item.get("action") or "search"), params=dict(params),
            project=str(item["project"]) if item.get("project") else None))
    return sources


@dataclass
class Collector:
    task_engine: TaskEngine
    adapters: AdapterRegistry
    sources: list[Source] = field(default_factory=list)
    refresh_known: bool = True
    max_refresh: int = 50
    interval_minutes: int = 30

    # -- 読み取り専用の門番 / the read-only gate ----------------------------------

    def _read(self, adapter_name: str, action: str, params: dict[str, Any]) -> Any:
        if not self.adapters.has(adapter_name):
            raise CollectError(f"{adapter_name} アダプタが設定されていません "
                               f"/ the {adapter_name} adapter is not configured")
        adapter = self.adapters.get(adapter_name)
        if action not in adapter.actions():
            raise CollectError(f"{adapter_name} に {action} がありません "
                               f"/ {adapter_name} has no action {action!r}")
        if adapter.writes(action):
            raise CollectError(
                f"{adapter_name}.{action} は書き込みを行うため、収集には使えません "
                f"/ {adapter_name}.{action} writes, so it cannot be used to collect")
        return adapter.invoke(action, params)

    # -- 1 回の収集 / one collection ----------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        engine = self.task_engine
        now = engine.now()
        run_id = f"collect-{now:%Y%m%dT%H%M%S}"
        engine.sync()
        outcomes_before = len(engine.outcomes)

        seen: set[str] = set()
        report: dict[str, Any] = {"at": now.isoformat(), "sources": [], "refreshed": 0,
                                  "failed": 0, "missing": [], "completed": 0}

        for source in self.sources:
            entry: dict[str, Any] = {"id": source.id, "adapter": source.adapter,
                                     "items": 0, "new": 0, "error": None}
            try:
                output = self._read(source.adapter, source.action, source.params)
                candidates = extract_candidates(output, source.adapter)
                entry["items"] = len(candidates)
                seen.update(c["key"] for c in candidates if c["key"])
                if candidates:
                    entry["new"] = engine.ingest(f"collector:{source.id}", run_id,
                                                 candidates, project=source.project)
            except Exception as exc:                       # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("収集元 %s に失敗 / source %s failed: %s",
                               source.id, source.id, exc)
            report["sources"].append(entry)

        if self.refresh_known:
            self._refresh(seen, run_id, report)

        engine.sync()
        report["completed"] = len(engine.outcomes) - outcomes_before
        return report

    # -- 既知タスクの再読み込み / re-reading known tasks ---------------------------------------

    def _refresh(self, seen: set[str], run_id: str, report: dict[str, Any]) -> None:
        engine = self.task_engine
        # 課題管理ツールのタスクだけ(台帳だけのタスクと、WBS ファイルは対象外)。
        # 走査で今回見つかったものは読み直さない。長く見ていないものから。
        # Tracker tasks only (not ledger-only tasks, not the WBS file); skip what the
        # scan just returned; the longest-unseen first.
        stale = sorted(
            (t for t in engine.ranked()
             if not t.origin and t.key and t.key not in seen
             and tracker_of(t) in TRACKERS and tracker_of(t) != "wbs_file"),
            key=lambda t: t.last_seen)[: self.max_refresh]

        by_tracker: dict[str, list[Task]] = {}
        for task in stale:
            by_tracker.setdefault(tracker_of(task), []).append(task)

        for tracker, tasks in by_tracker.items():
            try:
                if tracker == "jira":
                    self._refresh_jira(tasks, run_id, report)
                else:
                    self._refresh_by_id(tracker, tasks, run_id, report)
            except Exception as exc:                       # noqa: BLE001
                report["failed"] += len(tasks)
                report.setdefault("errors", []).append(
                    f"{tracker}: {type(exc).__name__}: {exc}")
                logger.warning("%s の再読み込みに失敗 / refresh of %s failed: %s",
                               tracker, tracker, exc)

    def _refresh_jira(self, tasks: list[Task], run_id: str, report: dict[str, Any]) -> None:
        for start in range(0, len(tasks), JIRA_BATCH):
            chunk = tasks[start:start + JIRA_BATCH]
            keys = [t.external_id or t.key or "" for t in chunk]
            output = self._read("jira", "search", {
                "jql": f"key in ({', '.join(keys)})", "limit": len(keys)})
            candidates = extract_candidates(output, "jira")
            found = {c["key"] for c in candidates}
            if candidates:
                self.task_engine.ingest("collector:refresh:jira", run_id, candidates)
            report["refreshed"] += len(found)
            # 返ってこなかった（削除された・見えなくなった）ものは、状態を変えずに記録する。
            report["missing"].extend(t.id for t in chunk if t.key not in found)

    def _refresh_by_id(self, tracker: str, tasks: list[Task], run_id: str,
                       report: dict[str, Any]) -> None:
        spec = TRACKERS[tracker]
        for task in tasks:
            try:
                ident: Any = spec.id_type(task.external_id)
                item = self._read(tracker, "get_issue", {spec.id_param: ident})
                candidates = extract_candidates({"items": [item]}, tracker)
                if candidates:
                    self.task_engine.ingest(f"collector:refresh:{tracker}", run_id,
                                            candidates, project=task.project or None)
                    report["refreshed"] += 1
                else:
                    report["missing"].append(task.id)
            except Exception as exc:                       # noqa: BLE001
                report["failed"] += 1
                report.setdefault("errors", []).append(
                    f"{task.id}: {type(exc).__name__}: {exc}")
                logger.warning("%s を読み直せません / cannot re-read %s: %s",
                               task.id, task.id, exc)
