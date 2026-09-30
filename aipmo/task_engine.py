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
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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


# 保持する完了実績の上限 / how many completion outcomes are kept
MAX_OUTCOMES = 500


def score_task(task: Task, today: date,
               label_bonus: dict[str, int] | None = None) -> tuple[int, list[str]]:
    """1件を採点する。戻り値は (点数, 内訳) / score one task with its breakdown.

    `label_bonus` は過去の実績から学習した「遅れやすいラベル」への加点
    （aipmo/pmo_learning.py）。複数該当しても最大の1つだけ。
    `label_bonus` is the learned extra weight for labels that tend to finish
    late; only the largest match applies.
    """
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

    if label_bonus:
        hits = [(label_bonus[label.lower()], label) for label in task.labels
                if label_bonus.get(label.lower())]
        if hits:
            extra, label = max(hits)
            points += extra
            reasons.append(f"過去実績で遅れやすいラベル「{label}」 +{extra}")

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcomes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _dump(task: Task) -> str:
    return json.dumps(asdict(task), ensure_ascii=False, sort_keys=True)


class TaskEngine:
    """台帳を持ち、実行の完了ごとに統合し、順位を付ける。

    `attach(engine)` で Engine の実行完了フックに繋ぐ。以降、どのテンプレート
    が走っても（scheduler / run / serve のどこから起動されても）自動で集まる。

    台帳は SQLite（WAL）に置く。`aipmo schedule`・`aipmo serve`・`aipmo run`
    は別プロセスで同じ台帳を使うため、ファイルを丸ごと書き戻す方式では、
    あとから保存した側が先の更新を消してしまう。書き込みは
    `transaction()` の中で行う: **書き込みロックを取り、最新の状態を読み直し、
    変更し、差分だけを書いて確定する**。これで更新が失われない。
    メモリ上の `tasks` は、その時点の写しにすぎない。

    以前の `task-ledger.json` があれば、初回の起動で取り込み、
    `.json.migrated` に改名する。

    Holds the ledger, merges after every run, ranks. `attach(engine)` hooks it
    to the engine's run-completion listeners so any template, however it was
    started, feeds it.

    The ledger lives in SQLite (WAL). The scheduler, the web server and
    `aipmo run` are separate processes sharing one ledger, and writing a whole
    file back lets whoever saves last erase an earlier update. Every write
    happens in `transaction()`: take the write lock, **re-read the latest
    state, change it, write only the difference, commit** — no update is
    lost. The in-memory `tasks` is only a copy as of that moment. A legacy
    `task-ledger.json` is imported on first start and renamed `.json.migrated`.
    """

    def __init__(
        self,
        path: Path,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stale_days: int = 30,
    ) -> None:
        self.path = self.resolve_path(path)
        self.now = now
        # 最後に挙がってからこの日数を過ぎた未完了は台帳から外す。
        # どのテンプレートももう挙げないものが、永久に上位を占めないように。
        # Open tasks unseen for this many days are dropped, so something no
        # template names any more cannot sit in the ranking forever.
        self.stale_days = stale_days
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._depth = 0
        self._persisted: dict[str, str] = {}
        self._n_outcomes = 0
        self.tasks: dict[str, Task] = {}
        # 完了の実績（学習の材料）。タスクが台帳から消えた後も残す。
        # Completion outcomes (learning material), kept after the task itself
        # has left the ledger.
        self.outcomes: list[dict[str, Any]] = []
        # 学習で得た、ラベルごとの加点。PMO Core が設定する。
        # Learned per-label bonus; set by the PMO Core.
        self.label_bonus: dict[str, int] = {}
        self._init_db()
        self.sync()

    # -- 場所 / location ---------------------------------------------------

    @staticmethod
    def resolve_path(path: Path | str) -> Path:
        """`*.json` が指定されたら、隣の `*.db` を台帳の実体とする。

        設定ファイルに古い `task-ledger.json` の名前が残っていても動く。
        A leftover `*.json` name in config maps to the `*.db` beside it.
        """
        p = Path(path)
        return p.with_suffix(".db") if p.suffix == ".json" else p

    @classmethod
    def exists(cls, path: Path | str) -> bool:
        db = cls.resolve_path(path)
        return db.exists() or db.with_suffix(".json").exists()

    # -- 永続化 / persistence -------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: BEGIN/COMMIT は自分で書く。
        # Autocommit mode; BEGIN/COMMIT are explicit.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                               check_same_thread=False)
        # WAL では NORMAL で破損しない。電源断で直近のコミットを失うことは
        # あるが、台帳は次の周で各テンプレートの出力から作り直せる。
        # FULL はコミットごとの fsync で、周ごとの書き込みには重い。
        # In WAL, NORMAL cannot corrupt the file; a power cut may lose the last
        # commit, which the next cycle rebuilds from the templates' output.
        # FULL fsyncs on every commit, too heavy for a write every cycle.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _db(self) -> sqlite3.Connection:
        """インスタンスごとに1本の接続を使い回す。

        操作のたびに開いて閉じると、最後の接続を閉じるたびに WAL の
        チェックポイント（fsync）が走り、実測で 1 回 約 100ms かかった。
        使い終えたら `close()` を呼ぶ。

        One connection per instance. Opening and closing per operation made
        every close checkpoint the WAL (an fsync) — about 100 ms each when
        measured. Call `close()` when done.
        """
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_db(self) -> None:
        conn = self._db()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        self._import_legacy(conn)

    def _import_legacy(self, conn: sqlite3.Connection) -> None:
        legacy = self.path.with_suffix(".json")
        if not legacy.exists():
            return
        # 複数プロセスが同時に起動しても、取り込みは一度だけ。
        # Once only, even if several processes start at the same moment.
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = conn.execute(
                "SELECT 1 FROM meta WHERE key = 'legacy_imported'").fetchone()
            if not done:
                try:
                    data = json.loads(legacy.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                for raw in data.get("tasks", []):
                    try:
                        task = Task(**raw)
                    except TypeError:
                        continue
                    conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                                 (task.id, _dump(task)))
                for outcome in data.get("outcomes") or []:
                    conn.execute("INSERT INTO outcomes (data) VALUES (?)",
                                 (json.dumps(outcome, ensure_ascii=False),))
                conn.execute("INSERT INTO meta VALUES ('legacy_imported', ?)",
                             (self.now().isoformat(),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        try:
            legacy.replace(legacy.with_name(legacy.name + ".migrated"))
        except OSError:
            pass    # 別プロセスが先に改名した / another process renamed it first

    def _load_from(self, conn: sqlite3.Connection) -> None:
        tasks: dict[str, Task] = {}
        for (data,) in conn.execute("SELECT data FROM tasks"):
            try:
                task = Task(**json.loads(data))
            except (TypeError, ValueError):
                continue     # 読めない行は飛ばす / skip an unreadable row
            tasks[task.id] = task
        self.tasks = tasks
        self._persisted = {tid: _dump(t) for tid, t in tasks.items()}
        self.outcomes = [json.loads(data) for (data,) in conn.execute(
            "SELECT data FROM outcomes ORDER BY seq")]
        self._n_outcomes = len(self.outcomes)

    def _persist(self, conn: sqlite3.Connection) -> None:
        """この取引で変わった行だけを書く / write only the rows that changed."""
        current = {tid: _dump(t) for tid, t in self.tasks.items()}
        for tid, data in current.items():
            if self._persisted.get(tid) != data:
                conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (tid, data))
        for tid in set(self._persisted) - set(current):
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))

        added = self.outcomes[self._n_outcomes:]
        for outcome in added:
            conn.execute("INSERT INTO outcomes (data) VALUES (?)",
                         (json.dumps(outcome, ensure_ascii=False),))
        if added:
            conn.execute(
                "DELETE FROM outcomes WHERE seq <= "
                "(SELECT MAX(seq) FROM outcomes) - ?", (MAX_OUTCOMES,))
        self._persisted = current
        self._n_outcomes = len(self.outcomes)

    def sync(self) -> None:
        """最新の台帳を読み直す。取引の中では何もしない（すでに最新）。

        Re-read the latest ledger. A no-op inside a transaction, where the
        state was just read under the write lock.
        """
        with self._lock:
            if self._depth:
                return
            conn = self._db()
            conn.execute("BEGIN")     # 2つの SELECT を同じ時点で読む
            try:
                self._load_from(conn)
            finally:
                conn.execute("COMMIT")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """読み直し → 変更 → 差分を書く、を1つの排他的な取引として行う。

        中で例外が出れば何も書かず、メモリも元に戻す。入れ子にできる
        （外側の取引に加わる）。**中でネットワーク呼び出しをしないこと** —
        書き込みロックを握ったまま待つと、ほかのプロセスを止める。

        Reload, change, write the difference — as one exclusive transaction.
        An exception writes nothing and restores memory. Nestable (joins the
        outer one). **No network calls inside**: waiting while holding the
        write lock stalls every other process.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return

            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._load_from(conn)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                self._persist(conn)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass        # SQLite がすでに取り消していた / already rolled back
                self._load_from(conn)
                raise

    def add_outcomes(self, outcomes: list[dict[str, Any]]) -> None:
        """完了実績を直接足す（取り込みや検証用）/ append outcomes directly."""
        with self.transaction():
            self.outcomes.extend(outcomes)

    def find(self, ref: str) -> Task | None:
        """タスク id か Jira キーで引く。最新の台帳を読み直してから。"""
        self.sync()
        return self.tasks.get(ref) or self.tasks.get(f"JIRA:{ref.upper()}")

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
        with self.transaction():
            for candidate in candidates:
                if self._merge(candidate, template, run_id, stamp):
                    created += 1
            self._rescore_locked()
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
        if candidate["done"] and not task.done and not is_new:
            self._record_outcome(task, stamp)
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

    def _record_outcome(self, task: Task, stamp: str) -> None:
        """完了を観測した瞬間に実績を残す。最初から完了で現れたものは、
        いつ終わったか分からないので残さない。

        Recorded when completion is *observed*. A task first seen already done
        is skipped: when it finished is unknown.
        """
        done_on = datetime.fromisoformat(stamp).date()
        due = _parse_date(task.due_date)
        self.outcomes.append({
            "task": task.id,
            "assignee": task.assignee,
            "labels": list(task.labels),
            "priority": task.priority,
            "due_date": task.due_date,
            "first_seen": task.first_seen,
            "done_at": stamp,
            "late_days": (done_on - due).days if due else None,
        })

    # -- 順位付け / ranking ---------------------------------------------

    def refresh(self) -> None:
        """時間経過での再採点。期限が迫る・超過するほど順位が動くため、
        テンプレートが走らなくても定期的に呼ぶ。

        Re-score with the passing of time: a deadline drawing closer changes
        the ranking even when no template has run.
        """
        with self.transaction():
            self._rescore_locked()

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
            task.score, task.reasons = score_task(task, today, self.label_bonus)

    def ranked(self, assignee: str | None = None, limit: int | None = None) -> list[Task]:
        """未完了を優先順に。同点は期限が早い方、それも同じなら id で安定させる。

        取引の外では、先に最新の台帳を読み直す。
        Outside a transaction it first re-reads the latest ledger.
        """
        self.sync()
        with self._lock:
            active = [t for t in self.tasks.values() if not t.done]
        if assignee is not None:
            active = [t for t in active if t.assignee == assignee]
        active.sort(key=lambda t: (-t.score, t.due_date or "9999-12-31", t.id))
        return active[:limit] if limit else active
