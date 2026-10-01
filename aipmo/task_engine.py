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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .trackers import TRACKERS, key_id
from .ledger_store import (  # noqa: F401  (LedgerTenantError は従来の場所からも使える)
    LedgerConfigError,
    LedgerStore,
    LedgerTenantError,
    Snapshot,
    SqliteStore,
    WriteTx,
)

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
# 「着手した」とみなす状態。着手の時刻は、見積りの実日数（ペース）の起点になる。
# States that count as started; the moment is the origin of the actual duration
# the pace is learned from.
IN_PROGRESS_STATUSES = {"in progress", "doing", "in review", "進行中", "レビュー中"}

_EFFORT_FIELDS = ("effort", "story_points", "points", "estimate")
_BLOCKED_MARKERS = {"blocked", "block", "ブロック", "ブロック中"}

# 複数のテンプレートが同じタスクを挙げたときの加点（1つ増えるごと）と上限。
# Points per additional template naming the same task, and the cap.
_CORROBORATION_STEP = 5
_CORROBORATION_CAP = 15

# 履歴に残す出どころの数。台帳が際限なく太らないように。
# Sources kept per task, so the ledger does not grow without bound.
_MAX_SOURCES = 10


_KEY_PROJECT = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-\d+$")


def project_of_key(key: str | None) -> str:
    """`PROJ-123` → `PROJ`。課題キーの形でなければ空 / project from an issue key."""
    match = _KEY_PROJECT.match(key or "")
    return match.group(1) if match else ""


def side_path(ledger: Path | str, name: str) -> Path:
    """台帳の隣に置くファイル（ブリーフィング・判断ログなど）の場所。

    既定の台帳 `task-ledger.db` では従来の名前のまま。別名の台帳
    （`acme.db` など）では `acme.` を前に付け、同じディレクトリに置いた
    複数の台帳がお互いのファイルを上書きしないようにする。

    Where the files beside a ledger live. The default ledger keeps the
    traditional names; a ledger named otherwise (`acme.db`) prefixes its own
    stem, so several ledgers in one directory never overwrite each other's.
    """
    db = TaskEngine.resolve_path(ledger)
    return db.parent / (name if db.stem == "task-ledger" else f"{db.stem}.{name}")


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
    # 所属プロジェクト。課題キーの接頭辞、または出力の project、なければ
    # 実行パラメータ（jira_project / project）から決まる。
    # The project it belongs to: the issue-key prefix, an explicit `project`
    # in the output, else the run's jira_project / project parameter.
    project: str = ""
    # どのトラッカー（アダプタ名）の課題か、そこでの識別子は何か。担当を
    # 書き戻すときの宛先になる。以前の行は空で、`JIRA:` で始まる id は Jira。
    # Which tracker (adapter name) owns it and its identifier there: the
    # destination of an assignee write-back. Older rows are empty; an id
    # starting `JIRA:` is Jira.
    tracker: str = ""
    external_id: str = ""
    # どこから生まれたタスクか。空は、課題管理ツールかテンプレートの出力から来たもの。
    # `recurring`（運用者が設定した定期タスク）と `followup`（PMO Core が警告から
    # 起こした提案）は、PMO Core が自分で作ったもの — 課題管理ツールには存在しない、
    # 台帳だけのタスクで、完了も人がここで記録する。`proposed` は人の承認待ち。
    # Where the task came from. Empty: a tracker or a template's output. `recurring`
    # (set up by the operator) and `followup` (proposed by the PMO Core from an
    # alert) were made by the PMO Core itself — ledger-only tasks that exist in no
    # tracker, closed by a person here. `proposed` means awaiting approval.
    origin: str = ""
    proposed: bool = False
    generated_from: str = ""
    # 役割AIに仕事を任せた記録（新しいものが後ろ。直近のぶんだけ残す）。
    # id / agent / template / at / status(running|done|failed|skipped|abandoned) /
    # run_id / finished_at / error / excerpt。
    # What was handed to a role AI for this task (newest last; only recent ones kept).
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    # 見積り（点）と、最初に着手を観測した時刻。完了のとき「見積りと実日数」を
    # 実績に残し、1 点あたりの日数（ペース）を学習する材料にする。
    # The estimate (points) and when work was first seen to start; at completion
    # they become "estimate vs actual days", the material the pace is learned from.
    effort: float | None = None
    started_at: str | None = None
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
               label_bonus: dict[str, int] | None = None,
               priority_delta: dict[str, int] | None = None,
               pace: dict[str, Any] | None = None) -> tuple[int, list[str]]:
    """1件を採点する。戻り値は (点数, 内訳) / score one task with its breakdown.

    `label_bonus` は過去の実績から学習した「遅れやすいラベル」への加点
    （aipmo/pmo_learning.py）。複数該当しても最大の1つだけ。
    `label_bonus` is the learned extra weight for labels that tend to finish
    late; only the largest match applies.

    `priority_delta` は優先度ごとの重みの補正（実績から）。`pace` は学習した
    1 点あたりの日数で、見積りが当たっていると確かめられたとき
    （`reliable`）だけ、「見積りとペースでは期限に間に合わない」タスクに加点する。
    `priority_delta` corrects each priority's weight from track record; `pace`
    (days per point) raises tasks the estimate cannot fit before their due date,
    only when the estimates have proved reliable.
    """
    base = _priority_points(task.priority)
    shift = (priority_delta or {}).get((task.priority or "").strip().lower(), 0)
    points = max(0, base + shift)
    if shift:
        reasons = [f"優先度 {task.priority} +{points}（実績による補正 {shift:+d}）"]
    else:
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

    if pace and pace.get("reliable") and due is not None and task.effort:
        days_left = (due - today).days
        who = (task.assignee or "").strip().lower()
        per_point = (pace.get("members") or {}).get(who) or pace.get("team")
        if per_point and days_left >= 0:
            expected = task.effort * per_point
            began = _parse_date(task.started_at)
            elapsed = (today - began).days if began else 0
            still = max(0.0, expected - elapsed)
            if still > days_left:
                extra = 10 if still - days_left <= 3 else 20
                points += extra
                reasons.append(
                    f"見積り {task.effort:g} 点 × 実績ペース {per_point:.1f} 日/点 "
                    f"→ あと約 {still:.0f} 日、期限まで {days_left} 日 +{extra}")

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


def _number(value: Any) -> float | None:
    """見積りとして使える正の数(文字列の数字も)。それ以外は None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_candidates(output: Any, adapter: str | None = None) -> list[dict[str, Any]]:
    """ステップ出力からタスク候補を拾う / pull task candidates from an output.

    `adapter` は、その出力を作ったアダプタの名前。トラッカー（GitHub・Plane・
    OpenProject・Azure DevOps）の出力は、課題の番号を `key` ではなく
    `number` / `id` で返すので、対応表（aipmo/trackers.py）で読み替え、
    どのトラッカーの課題かを候補に残す。

    `adapter` names what produced the output. Trackers other than Jira return
    the number as `number` / `id` rather than `key`, so it is read through the
    table in aipmo/trackers.py and the candidate remembers its tracker.
    """
    if isinstance(output, dict):
        items = output.get("items")
    else:
        items = output
    if not isinstance(items, list):
        return []

    tracker = TRACKERS.get(adapter or "")
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tracker_name, external_id = "", ""
        if tracker is not None:
            ref = next((_text(item.get(f)) for f in tracker.ref_fields
                        if _text(item.get(f))), None)
            title = next((_text(item.get(f)) for f in tracker.title_fields
                          if _text(item.get(f))), None)
            if ref and tracker.adapter == "jira":
                key = ref.upper()
            elif ref:
                key = f"{tracker.prefix}:{ref}"
            else:
                key = None
            if key:
                tracker_name = tracker.adapter
                external_id = key if tracker.adapter == "jira" else (ref or "")
            title = title or key
        else:
            title = _text(item.get("summary") or item.get("title"))
            key = _text(item.get("key"))
            key = key.upper() if key else None
        if not title and not key:
            continue
        labels = item.get("labels") or []
        status = _text(item.get("status"))
        due_fields = tracker.due_fields if tracker is not None else ("due_date", "duedate")
        candidates.append({
            "key": key,
            "tracker": tracker_name,
            "external_id": external_id,
            "title": title or key,
            "assignee": _text(item.get("assignee")),
            "due_date": next((_text(item.get(f)) for f in due_fields
                              if _text(item.get(f))), None),
            "priority": _text(item.get("priority")),
            "status": status,
            "labels": [str(label) for label in labels],
            "project": _text(item.get("project")) or project_of_key(key),
            "effort": next((n for f in _EFFORT_FIELDS
                            if (n := _number(item.get(f))) is not None), None),
            "blocked": (
                any(str(label).lower() in _BLOCKED_MARKERS for label in labels)
                or bool(status and status.lower() in _BLOCKED_MARKERS)
                or bool(item.get("blocked"))
            ),
            "done": bool(item.get("done")) or bool(item.get("completed")) or bool(
                status and status.lower() in _DONE_STATUSES),
        })
    return candidates


def _dump(task: Task) -> str:
    return json.dumps(asdict(task), ensure_ascii=False, sort_keys=True)


class TaskEngine:
    """台帳を持ち、実行の完了ごとに統合し、順位を付ける。

    `attach(engine)` で Engine の実行完了フックに繋ぐ。以降、どのテンプレート
    が走っても（scheduler / run / serve のどこから起動されても）自動で集まる。

    台帳の保存先は SQLite（既定）か PostgreSQL（[aipmo/ledger_store.py]
    (aipmo/ledger_store.py)）。`aipmo schedule`・`aipmo serve`・`aipmo run`
    は別プロセス（PostgreSQL なら別ホスト）で同じ台帳を使うため、全体を丸ごと
    書き戻す方式では、あとから保存した側が先の更新を消してしまう。書き込みは
    `transaction()` の中で行う: **排他的な取引に入り、最新の状態を読み直し、
    変更し、差分だけを書いて確定する**。これで更新が失われない。
    メモリ上の `tasks` は、その時点の写しにすぎない。

    以前の `task-ledger.json` があれば、SQLite の初回の起動で取り込み、
    `.json.migrated` に改名する。

    Holds the ledger, merges after every run, ranks. `attach(engine)` hooks it
    to the engine's run-completion listeners so any template, however it was
    started, feeds it.

    The rows live in SQLite (default) or PostgreSQL. The scheduler, the web
    server and `aipmo run` are separate processes — hosts, with PostgreSQL —
    sharing one ledger, and writing the whole thing back lets whoever saves
    last erase an earlier update. Every write happens in `transaction()`: enter
    an exclusive transaction, **re-read the latest state, change it, write only
    the difference, commit** — no update is lost. The in-memory `tasks` is only
    a copy as of that moment. A legacy `task-ledger.json` is imported on the
    first SQLite start and renamed `.json.migrated`.
    """

    def __init__(
        self,
        path: Path,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stale_days: int = 30,
        tenant: str | None = None,
        store: LedgerStore | None = None,
    ) -> None:
        # `path` は SQLite の台帳ファイルであり、台帳の隣のファイル
        # （ブリーフィング・判断ログ）の置き場所でもある。PostgreSQL では
        # 後者の役割だけが残る。
        # `path` is the SQLite file and also where the files beside the ledger
        # (briefing, decision log) live; with PostgreSQL only the latter is left.
        self.path = self.resolve_path(path)
        self.tenant = tenant or None
        self.now = now
        # 最後に挙がってからこの日数を過ぎた未完了は台帳から外す。
        # どのテンプレートももう挙げないものが、永久に上位を占めないように。
        # Open tasks unseen for this many days are dropped, so something no
        # template names any more cannot sit in the ranking forever.
        self.stale_days = stale_days
        self._lock = threading.RLock()
        self._store: LedgerStore = store if store is not None else SqliteStore(self.path)
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
        self.priority_delta: dict[str, int] = {}
        self.pace: dict[str, Any] = {}
        self._prepare_store()
        self.sync()

    @property
    def backend(self) -> str:
        """保存先の種類（`sqlite` / `postgres`）/ which store holds the rows."""
        return self._store.kind

    def describe(self) -> str:
        return self._store.describe()

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

    def _legacy_snapshot(self) -> Snapshot | None:
        """旧形式 `task-ledger.json` を、保存先の行の形に読み替える。"""
        legacy = self.path.with_suffix(".json")
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Snapshot()
        snapshot = Snapshot()
        for raw in data.get("tasks", []):
            try:
                task = Task(**raw)
            except TypeError:
                continue
            snapshot.tasks[task.id] = _dump(task)
        snapshot.outcomes = [json.dumps(o, ensure_ascii=False)
                             for o in data.get("outcomes") or []]
        return snapshot

    def _prepare_store(self) -> None:
        legacy = self.path.with_suffix(".json")
        sqlite = isinstance(self._store, SqliteStore)
        self._store.prepare(self.tenant, self._legacy_snapshot
                            if sqlite and legacy.exists() else None)
        if sqlite and legacy.exists():
            try:
                legacy.replace(legacy.with_name(legacy.name + ".migrated"))
            except OSError:
                pass    # 別プロセスが先に改名した / another process renamed it first

    def close(self) -> None:
        with self._lock:
            self._store.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _load_snapshot(self, snapshot: Snapshot) -> None:
        tasks: dict[str, Task] = {}
        for data in snapshot.tasks.values():
            try:
                task = Task(**json.loads(data))
            except (TypeError, ValueError):
                continue     # 読めない行は飛ばす / skip an unreadable row
            if not task.project and task.key:
                task.project = project_of_key(task.key)   # 以前の行 / older rows
            tasks[task.id] = task
        self.tasks = tasks
        self._persisted = {tid: _dump(t) for tid, t in tasks.items()}
        self.outcomes = [json.loads(data) for data in snapshot.outcomes]
        self._n_outcomes = len(self.outcomes)

    def _persist(self, tx: WriteTx) -> None:
        """この取引で変わった行だけを書く / write only the rows that changed."""
        current = {tid: _dump(t) for tid, t in self.tasks.items()}
        changed = {tid: data for tid, data in current.items()
                   if self._persisted.get(tid) != data}
        removed = set(self._persisted) - set(current)
        added = [json.dumps(o, ensure_ascii=False)
                 for o in self.outcomes[self._n_outcomes:]]
        tx.apply(changed, removed, added, MAX_OUTCOMES)
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
            self._load_snapshot(self._store.read())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """読み直し → 変更 → 差分を書く、を1つの排他的な取引として行う。

        中で例外が出れば何も書かず、メモリも元に戻す。入れ子にできる
        （外側の取引に加わる）。**中でネットワーク呼び出しをしないこと** —
        排他を握ったまま待つと、ほかのプロセスを止める。

        Reload, change, write the difference — as one exclusive transaction.
        An exception writes nothing and restores memory. Nestable (joins the
        outer one). **No network calls inside**: waiting while holding the
        exclusive lock stalls every other process.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return

            try:
                with self._store.write() as tx:
                    self._load_snapshot(tx.snapshot)
                    self._depth = 1
                    try:
                        yield
                    finally:
                        self._depth = 0
                    self._persist(tx)
            except BaseException:
                # 取り消された。メモリを保存先の状態に戻す。
                # Rolled back: bring memory back to what the store holds.
                try:
                    self._load_snapshot(self._store.read())
                except Exception:       # noqa: BLE001
                    pass
                raise

    def add_outcomes(self, outcomes: list[dict[str, Any]]) -> None:
        """完了実績を直接足す（取り込みや検証用）/ append outcomes directly."""
        with self.transaction():
            self.outcomes.extend(outcomes)

    def find(self, ref: str) -> Task | None:
        """タスク id か Jira キーで引く。最新の台帳を読み直してから。"""
        self.sync()
        return self.tasks.get(ref) or self.tasks.get(key_id(ref.upper()))

    # -- 収集と統合 / ingest and merge -----------------------------------

    def attach(self, engine: Any) -> None:
        engine.run_listeners.append(self.on_run_complete)

    def on_run_complete(self, template_name: str, ctx: Any) -> None:
        """Engine の完了フック。ここで落ちても本来の実行は止めない。"""
        try:
            harvested: list[dict[str, Any]] = []
            sources = getattr(ctx, "step_adapters", None) or {}
            for step_id, result in ctx.results.items():
                if result.status == "success":
                    harvested.extend(extract_candidates(result.output, sources.get(step_id)))
            if harvested:
                params = getattr(ctx, "params", None) or {}
                default = _text(params.get("jira_project") or params.get("project"))
                self.ingest(template_name, ctx.run_id, harvested, project=default)
        except Exception:
            logger.warning("%s: タスクの収集に失敗 / harvest failed",
                           template_name, exc_info=True)

    def ingest(self, template: str, run_id: str,
               candidates: list[dict[str, Any]], project: str | None = None) -> int:
        """候補を台帳へ統合する。戻り値は新規に増えたタスク数。

        `project` は、候補自身が project を持たないときの既定（実行パラメータ由来）。
        `project` is the default for candidates that name none themselves.
        """
        stamp = self.now().isoformat()
        created = 0
        with self.transaction():
            for candidate in candidates:
                if self._merge(candidate, template, run_id, stamp, project):
                    created += 1
            self._rescore_locked()
        return created

    @staticmethod
    def _title_id(project: str, title: str) -> str:
        # キー無しタスクの同一性はプロジェクト内だけ。別プロジェクトの
        # 同名タスク（「議事録を共有する」など）を1件にしてしまわない。
        # Keyless identity holds only within a project: same-titled tasks in
        # different projects must never be merged into one.
        normalized = _normalize_title(title)
        return f"T:{project.lower()}:{normalized}" if project else f"T:{normalized}"

    def _find(self, candidate: dict[str, Any]) -> Task | None:
        key = candidate["key"]
        if key and key_id(key) in self.tasks:
            return self.tasks[key_id(key)]
        # キー無しで拾われていた同名タスクに、キーを結び付ける。プロジェクトが
        # 分かる前に拾われた（project が空の）古いものは、同名なら引き継ぐ。
        # Attach a key to a same-titled task first seen without one. An older
        # task picked up before its project was known (project empty) is
        # adopted when the title matches.
        project = candidate["project"]
        for title_id in dict.fromkeys([self._title_id(project, candidate["title"]),
                                       self._title_id("", candidate["title"])]):
            found = self.tasks.get(title_id)
            if found is not None and (not found.project or not project
                                      or found.project.lower() == project.lower()):
                return found
        return None

    def _merge(self, candidate: dict[str, Any], template: str,
               run_id: str, stamp: str, default_project: str | None = None) -> bool:
        source = {"template": template, "run_id": run_id, "seen_at": stamp}
        candidate = {**candidate,
                     "project": (candidate.get("project") or project_of_key(candidate["key"])
                                or default_project or "")}
        task = self._find(candidate)
        is_new = task is None

        if task is None:
            key = candidate["key"]
            task = Task(
                id=(key_id(key) if key
                    else self._title_id(candidate["project"], candidate["title"])),
                title=candidate["title"], first_seen=stamp, status_since=stamp,
            )
            self.tasks[task.id] = task
        elif candidate["key"] and task.key is None:
            del self.tasks[task.id]
            task.id = key_id(candidate["key"])
            self.tasks[task.id] = task

        task.key = task.key or candidate["key"]
        task.project = task.project or candidate["project"]
        task.tracker = task.tracker or candidate.get("tracker", "")
        task.external_id = task.external_id or candidate.get("external_id", "")
        if candidate["key"]:
            task.title = candidate["title"]  # キー付き（課題管理側）の題名を正とする
        task.assignee = candidate["assignee"] or task.assignee
        if task.assignee:
            task.suggested_assignee = task.suggestion_reason = None
        if candidate["status"] and candidate["status"] != task.status:
            task.status_since = stamp
        task.status = candidate["status"] or task.status
        if candidate.get("effort") is not None:
            task.effort = candidate["effort"]
        if task.started_at is None and (task.status or "").strip().lower() in IN_PROGRESS_STATUSES:
            task.started_at = stamp
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
        started = _parse_date(task.started_at)
        self.outcomes.append({
            "task": task.id,
            "assignee": task.assignee,
            "labels": list(task.labels),
            "priority": task.priority,
            "due_date": task.due_date,
            "first_seen": task.first_seen,
            "done_at": stamp,
            "late_days": (done_on - due).days if due else None,
            # 見積りと、着手の観測から完了の観測までの日数。どちらかが無ければ None。
            # Estimate, and days from the start being observed to the finish being
            # observed; None if either is unknown.
            "effort": task.effort,
            "duration_days": (done_on - started).days if started else None,
        })

    # -- 台帳だけのタスク / ledger-only tasks -----------------------------------

    def create_task(self, task_id: str, title: str, *, origin: str, proposed: bool,
                    project: str = "", priority: str | None = None,
                    due_date: str | None = None, assignee: str | None = None,
                    labels: Iterable[str] = (), generated_from: str = "") -> Task | None:
        """PMO Core が自分でタスクを作る。同じ id が既にあれば何もしない（冪等）。

        id は呼び出し側が決める（`PMO:rec:週次レビュー:2026-W40` のように、
        「どの定期タスクの、どの期間」「どの警告の、どの回」を表す）。決定済みの
        提案（却下を含む）も id が残るので、同じものが何度も出直さない。

        The PMO Core makes a task itself. Idempotent on the id, which the caller
        builds to mean "this recurring task, this period" or "this alert, this
        episode". A decided proposal (a rejected one too) keeps its id, so the
        same thing never comes back.
        """
        stamp = self.now().isoformat()
        with self.transaction():
            if task_id in self.tasks:
                return None
            task = Task(
                id=task_id, key=task_id, title=title, project=project, priority=priority,
                due_date=due_date, assignee=assignee, labels=sorted(set(labels)),
                origin=origin, proposed=proposed, generated_from=generated_from,
                status="Proposed" if proposed else "To Do", status_since=stamp,
                first_seen=stamp, last_seen=stamp,
                sources=[{"template": f"pmo_core:{origin}", "run_id": task_id,
                          "seen_at": stamp}],
            )
            self.tasks[task_id] = task
            self._rescore_locked()
        return task

    def decide_proposal(self, ref: str, approve: bool) -> Task:
        """提案を承認する（仕事になる）か、却下する（記録は残し、順位には入れない）。"""
        stamp = self.now().isoformat()
        found = self.find(ref)
        if found is None:
            raise KeyError(f"タスクが見つかりません / no such task: {ref}")
        with self.transaction():
            task = self.tasks.get(found.id)
            if task is None:
                raise KeyError(f"タスクが見つかりません / no such task: {ref}")
            if not task.proposed:
                raise ValueError(f"{task.id} は承認待ちの提案ではありません "
                                 f"/ not a pending proposal")
            task.proposed = False
            task.status_since = stamp
            task.last_seen = stamp
            if approve:
                task.status = "To Do"
            else:
                # 却下も記録に残す（同じ提案を出し直さないため）。完了実績にはしない。
                # A rejection is kept (so it is not proposed again) but is not an outcome.
                task.status, task.done = "Rejected", True
            self._rescore_locked()
        return task

    def complete(self, ref: str) -> Task:
        """台帳だけのタスクを完了にする。課題管理ツールのタスクは、そちらで閉じる。

        Closes a ledger-only task. A tracker's task is closed in the tracker — the
        ledger would only be contradicted by the next sync.
        """
        stamp = self.now().isoformat()
        found = self.find(ref)
        if found is None:
            raise KeyError(f"タスクが見つかりません / no such task: {ref}")
        with self.transaction():
            task = self.tasks.get(found.id)
            if task is None:
                raise KeyError(f"タスクが見つかりません / no such task: {ref}")
            if not task.origin:
                raise ValueError(f"{task.id} は課題管理ツール側のタスクです。そちらで閉じて"
                                 f"ください / close it in its tracker")
            if task.proposed:
                raise ValueError(f"{task.id} は承認待ちです。先に承認してください "
                                 f"/ approve the proposal first")
            if task.done:
                raise ValueError(f"{task.id} はすでに完了です / already done")
            self._record_outcome(task, stamp)
            task.done, task.status, task.last_seen = True, "Done", stamp
            self._rescore_locked()
        return task

    def proposals(self) -> list[Task]:
        """承認待ちの提案（新しい順）/ pending proposals, newest first."""
        self.sync()
        with self._lock:
            pending = [t for t in self.tasks.values() if t.proposed]
        return sorted(pending, key=lambda t: t.first_seen, reverse=True)

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
            # 台帳だけのタスクは、どのテンプレートも挙げないのが普通。開いている間は
            # 消さない（承認待ちの提案と、終わったものだけが期限で消える）。
            # Ledger-only tasks are never named by a template; while open they are kept
            # (only unanswered proposals and finished ones age out).
            if (cutoff - seen).days > self.stale_days and not (
                    task.origin and not task.done and not task.proposed):
                del self.tasks[task_id]
                continue
            if task.done:
                # 完了したものは台帳に残すが順位には入れない（履歴として）。
                task.score, task.reasons = 0, ["完了"]
                continue
            task.score, task.reasons = score_task(task, today, self.label_bonus, self.priority_delta, self.pace)

    def projects(self) -> list[str]:
        """未完了タスクがあるプロジェクト名（重複なし・並び順固定）。"""
        self.sync()
        with self._lock:
            names = {t.project for t in self.tasks.values()
                     if not t.done and not t.proposed and t.project}
        return sorted(names, key=str.lower)

    def ranked(self, assignee: str | None = None, limit: int | None = None,
               project: str | None = None,
               projects: Iterable[str] | None = None) -> list[Task]:
        """未完了を優先順に。同点は期限が早い方、それも同じなら id で安定させる。

        取引の外では、先に最新の台帳を読み直す。
        Outside a transaction it first re-reads the latest ledger.
        """
        self.sync()
        with self._lock:
            # 承認待ちの提案は、まだ仕事ではない。順位にも担当提案にも入れない。
            # An unapproved proposal is not work yet: not ranked, not assigned.
            active = [t for t in self.tasks.values() if not t.done and not t.proposed]
        if assignee is not None:
            active = [t for t in active if t.assignee == assignee]
        allowed = {p.lower() for p in projects} if projects is not None else None
        if project is not None:
            allowed = {project.lower()} if allowed is None else allowed & {project.lower()}
        if allowed is not None:
            # project の無いタスクは、絞り込みの対象外（見せない）。
            # A task with no project is never shown through a project filter.
            active = [t for t in active if t.project.lower() in allowed]
        active.sort(key=lambda t: (-t.score, t.due_date or "9999-12-31", t.id))
        return active[:limit] if limit else active
