"""Task Engine — 複数テンプレートを横断するタスクの統合と優先順位付け。

個々のテンプレート（会議の TODO 抽出、期限超過の洗い出し、スプリント
健全性など）は、それぞれ別々にタスクの一覧を出力する。同じ課題が
複数のテンプレートに現れ、しかも「どれから手を付けるか」はテンプレート
どうしでは決められない。このエンジンは、実行が終わるたびにその出力から
タスク候補を集め、1つの台帳に統合し、順位を付ける。

順位は数えれば決まる材料だけで付ける。言語モデルは使わない —
[aipmo/portfolio.py](aipmo/portfolio.py) と同じ理由で、同じ入力から毎回
同じ順位が出ることが、後から「なぜこれが1位か」を説明できる前提だから。
点数の内訳は `reasons` に残る。

入力の規約 / what counts as a task
-----------------------------------
成功したステップの出力が、次のどちらかの形をしていれば、その要素を
タスク候補として拾う（jira.search / find_overdue / todo 抽出などが
既にこの形）。

  {"items": [{"key"?, "summary"|"title", "assignee"?, "due_date"?,
              "priority"?, "status"?, "labels"?}, ...]}
  [{...}, ...]

key も summary も無い要素は拾わない。

統合 / merging
--------------
同じ Jira キー、またはキーが無ければ正規化したタイトルが同じものは
1件にまとめ、出どころ（テンプレート）を全部残す。あるテンプレートが
キー無しで拾った TODO を、後から別のテンプレートがキー付きで拾った
ときは、同じタスクとして結び付ける。統合後は、優先度は高い方、期限は
早い方、担当者は空でない方を採る。複数のテンプレートが同じものを
挙げるほど、注意が要る兆候として加点する。

Task Engine — merges and ranks tasks across templates. Each template lists
tasks on its own; none of them can decide what to do first. After every run
this engine harvests task candidates from step outputs, merges them into one
ledger, and ranks it. Ranking is deterministic (no language model): a
reproducible number is what lets anyone ask "why is this first" later, and
the breakdown is kept in `reasons`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("aipmo.task_engine")

# 優先度 → 加点。Jira の標準名と日本語表記の両方を受ける。
# Priority → points; Jira's standard names and Japanese labels both work.
_PRIORITY_POINTS = {
    "highest": 40, "blocker": 40, "critical": 40, "最高": 40, "緊急": 40,
    "high": 30, "major": 30, "高": 30,
    "medium": 15, "normal": 15, "中": 15,
    "low": 5, "minor": 5, "低": 5,
    "lowest": 0, "trivial": 0, "最低": 0,
}
_DEFAULT_PRIORITY_POINTS = 15  # 不明は「中」扱い / unknown counts as medium

_DONE_STATUSES = {"done", "closed", "resolved", "complete", "completed",
                  "完了", "クローズ", "解決済み"}
_BLOCKED_MARKERS = {"blocked", "block", "ブロック", "ブロック中"}

# 複数のテンプレートが同じタスクを挙げたときの加点（1つ増えるごと）と上限。
# Points per additional template naming the same task, and the cap.
_CORROBORATION_STEP = 5
_CORROBORATION_CAP = 15

# 履歴に残す出どころの数。台帳が際限なく太らないように。
# Sources kept per task, so the ledger does not grow without bound.
_MAX_SOURCES = 10


@dataclass
class Task:
    id: str                       # 統合キー / merge key ("JIRA:PROJ-1" or "T:<title>")
    title: str
    key: str | None = None        # Jira などの課題キー
    assignee: str | None = None
    due_date: str | None = None   # YYYY-MM-DD
    priority: str | None = None
    status: str | None = None
    blocked: bool = False
    done: bool = False
    labels: list[str] = field(default_factory=list)
    # 現在の状態／ブロックになってからの時刻。進捗ルール（停滞・長期ブロック）
    # が「どれだけ続いているか」を数えるのに使う。
    # When the current status / blocked state began; progress rules use these
    # to count how long something has been stuck.
    status_since: str | None = None
    blocked_since: str | None = None
    # PMO Core が出した担当者の提案。確定は人（assign --apply）。
    # The PMO Core's assignee proposal; a human confirms it (assign --apply).
    suggested_assignee: str | None = None
    suggestion_reason: str | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def templates(self) -> list[str]:
        seen: list[str] = []
        for source in self.sources:
            if source["template"] not in seen:
                seen.append(source["template"])
        return seen


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s　]+", " ", title).strip().lower()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _priority_points(priority: str | None) -> int:
    if not priority:
        return _DEFAULT_PRIORITY_POINTS
    return _PRIORITY_POINTS.get(priority.strip().lower(), _DEFAULT_PRIORITY_POINTS)


def score_task(task: Task, today: date) -> tuple[int, list[str]]:
    """1件を採点する。戻り値は (点数, 内訳) / score one task with its breakdown."""
    points = _priority_points(task.priority)
    reasons = [f"優先度 {task.priority or '未設定'} +{points}"]

    due = _parse_date(task.due_date)
    if due is not None:
        remaining = (due - today).days
        if remaining < 0:
            extra = 30 + min(-remaining, 30)
            reasons.append(f"期限を {-remaining} 日超過 +{extra}")
        elif remaining <= 3:
            extra = 20
            reasons.append(f"期限まで残り {remaining} 日 +{extra}")
        elif remaining <= 7:
            extra = 10
            reasons.append(f"期限まで残り {remaining} 日 +{extra}")
        else:
            extra = 0
        points += extra

    if task.blocked:
        points += 15
        reasons.append("ブロック中 +15")

    others = len(task.templates) - 1
    if others > 0:
        extra = min(others * _CORROBORATION_STEP, _CORROBORATION_CAP)
        points += extra
        reasons.append(f"他 {others} 件のテンプレートも指摘 +{extra}")

    if not task.assignee:
        points += 5
        reasons.append("担当者未定 +5")

    return points, reasons


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_candidates(output: Any) -> list[dict[str, Any]]:
    """ステップ出力からタスク候補を拾う / pull task candidates from an output."""
    if isinstance(output, dict):
        items = output.get("items")
    else:
        items = output
    if not isinstance(items, list):
        return []

    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("summary") or item.get("title"))
        key = _text(item.get("key"))
        if not title and not key:
            continue
        labels = item.get("labels") or []
        status = _text(item.get("status"))
        candidates.append({
            "key": key.upper() if key else None,
            "title": title or key,
            "assignee": _text(item.get("assignee")),
            "due_date": _text(item.get("due_date") or item.get("duedate")),
            "priority": _text(item.get("priority")),
            "status": status,
            "labels": [str(label) for label in labels],
            "blocked": (
                any(str(label).lower() in _BLOCKED_MARKERS for label in labels)
                or bool(status and status.lower() in _BLOCKED_MARKERS)
                or bool(item.get("blocked"))
            ),
            "done": bool(item.get("done")) or bool(
                status and status.lower() in _DONE_STATUSES),
        })
    return candidates


class TaskEngine:
    """台帳を持ち、実行の完了ごとに統合し、順位を付ける。

    `attach(engine)` で Engine の実行完了フックに繋ぐ。以降、どのテンプレート
    が走っても（scheduler / run / serve のどこから起動されても）自動で集まる。
    台帳はファイルに残るので、再起動しても引き継がれる。

    Holds the ledger, merges after every run, ranks. `attach(engine)` hooks it
    to the engine's run-completion listeners so any template, however it was
    started, feeds it. The ledger is a file and survives restarts.
    """

    def __init__(
        self,
        path: Path,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stale_days: int = 30,
    ) -> None:
        self.path = path
        self.now = now
        # 最後に挙がってからこの日数を過ぎた未完了は台帳から外す。
        # どのテンプレートももう挙げないものが、永久に上位を占めないように。
        # Open tasks unseen for this many days are dropped, so something no
        # template names any more cannot sit in the ranking forever.
        self.stale_days = stale_days
        self._lock = threading.Lock()
        self.tasks: dict[str, Task] = {}
        self._load()

    # -- 永続化 / persistence -------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # 読めなくても止めない / an unreadable ledger is not fatal
        for raw in data.get("tasks", []):
            try:
                task = Task(**raw)
            except TypeError:
                continue
            self.tasks[task.id] = task

    def save(self) -> None:
        """外部（PMO Core）が台帳を書き換えたあとに保存する。"""
        with self._lock:
            self._save()

    def _save(self) -> None:
        payload = {"updated_at": self.now().isoformat(),
                   "tasks": [asdict(t) for t in self.tasks.values()]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            logger.warning("台帳を保存できません / cannot save ledger: %s", exc)

    # -- 収集と統合 / ingest and merge -----------------------------------

    def attach(self, engine: Any) -> None:
        engine.run_listeners.append(self.on_run_complete)

    def on_run_complete(self, template_name: str, ctx: Any) -> None:
        """Engine の完了フック。ここで落ちても本来の実行は止めない。"""
        try:
            harvested: list[dict[str, Any]] = []
            for result in ctx.results.values():
                if result.status == "success":
                    harvested.extend(extract_candidates(result.output))
            if harvested:
                self.ingest(template_name, ctx.run_id, harvested)
        except Exception:
            logger.warning("%s: タスクの収集に失敗 / harvest failed",
                           template_name, exc_info=True)

    def ingest(self, template: str, run_id: str,
               candidates: list[dict[str, Any]]) -> int:
        """候補を台帳へ統合する。戻り値は新規に増えたタスク数。"""
        stamp = self.now().isoformat()
        created = 0
        with self._lock:
            for candidate in candidates:
                if self._merge(candidate, template, run_id, stamp):
                    created += 1
            self._rescore_locked()
            self._save()
        return created

    def _find(self, candidate: dict[str, Any]) -> Task | None:
        key = candidate["key"]
        if key and f"JIRA:{key}" in self.tasks:
            return self.tasks[f"JIRA:{key}"]
        # キー無しで拾われていた同名タスクに、キーを結び付ける。
        # Attach a key to a same-titled task first seen without one.
        title_id = f"T:{_normalize_title(candidate['title'])}"
        return self.tasks.get(title_id)

    def _merge(self, candidate: dict[str, Any], template: str,
               run_id: str, stamp: str) -> bool:
        source = {"template": template, "run_id": run_id, "seen_at": stamp}
        task = self._find(candidate)
        is_new = task is None

        if task is None:
            key = candidate["key"]
            task = Task(
                id=f"JIRA:{key}" if key else f"T:{_normalize_title(candidate['title'])}",
                title=candidate["title"], first_seen=stamp, status_since=stamp,
            )
            self.tasks[task.id] = task
        elif candidate["key"] and task.key is None:
            del self.tasks[task.id]
            task.id = f"JIRA:{candidate['key']}"
            self.tasks[task.id] = task

        task.key = task.key or candidate["key"]
        if candidate["key"]:
            task.title = candidate["title"]  # キー付き（課題管理側）の題名を正とする
        task.assignee = candidate["assignee"] or task.assignee
        if task.assignee:
            task.suggested_assignee = task.suggestion_reason = None
        if candidate["status"] and candidate["status"] != task.status:
            task.status_since = stamp
        task.status = candidate["status"] or task.status
        task.labels = sorted(set(task.labels) | set(candidate.get("labels") or []))

        new_due, old_due = _parse_date(candidate["due_date"]), _parse_date(task.due_date)
        if new_due and (old_due is None or new_due < old_due):
            task.due_date = new_due.isoformat()

        if _priority_points(candidate["priority"]) > _priority_points(task.priority) \
                or task.priority is None:
            task.priority = candidate["priority"] or task.priority

        # 最新の観測を正とする。あるテンプレートが「もう完了」と言えば完了、
        # 「ブロック中でない」と言えば解除。
        # The latest observation wins: a template reporting it done, or no
        # longer blocked, clears the earlier state.
        task.done = candidate["done"]
        if candidate["blocked"] and not task.blocked:
            task.blocked_since = stamp
        elif not candidate["blocked"]:
            task.blocked_since = None
        task.blocked = candidate["blocked"]

        task.last_seen = stamp
        if not any(s["template"] == template and s["run_id"] == run_id
                   for s in task.sources):
            task.sources.append(source)
            del task.sources[:-_MAX_SOURCES]
        return is_new

    # -- 順位付け / ranking ---------------------------------------------

    def refresh(self) -> None:
        """時間経過での再採点。期限が迫る・超過するほど順位が動くため、
        テンプレートが走らなくても定期的に呼ぶ。

        Re-score with the passing of time: a deadline drawing closer changes
        the ranking even when no template has run.
        """
        with self._lock:
            self._rescore_locked()
            self._save()

    def _rescore_locked(self) -> None:
        today = self.now().date()
        cutoff = self.now()
        for task_id in list(self.tasks):
            task = self.tasks[task_id]
            seen = datetime.fromisoformat(task.last_seen)
            if (cutoff - seen).days > self.stale_days:
                del self.tasks[task_id]
                continue
            if task.done:
                # 完了したものは台帳に残すが順位には入れない（履歴として）。
                task.score, task.reasons = 0, ["完了"]
                continue
            task.score, task.reasons = score_task(task, today)

    def ranked(self, assignee: str | None = None, limit: int | None = None) -> list[Task]:
        """未完了を優先順に。同点は期限が早い方、それも同じなら id で安定させる。"""
        with self._lock:
            active = [t for t in self.tasks.values() if not t.done]
        if assignee is not None:
            active = [t for t in active if t.assignee == assignee]
        active.sort(key=lambda t: (-t.score, t.due_date or "9999-12-31", t.id))
        return active[:limit] if limit else active
