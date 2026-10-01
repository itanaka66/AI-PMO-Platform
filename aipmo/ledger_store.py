"""台帳の保存先 — SQLite と PostgreSQL。

[aipmo/task_engine.py](aipmo/task_engine.py) の `TaskEngine` は、保存先の
違いを知らない。ここが担うのは「行の読み書きと排他」だけで、タスクの
意味（統合・採点）には関与しない。行は `id → JSON 文字列` として扱う。

どちらの保存先も同じ約束を守る:

  - `read()`     … 取引の途中の状態が見えない、一貫した写しを返す
  - `write()`    … 排他的な書き込み取引。入った時点の**最新**の写しを渡し、
                   `apply()` で差分だけを書き、正常に抜ければ確定、
                   例外なら何も書かない

この約束があるので、別プロセス（あるいは別ホスト）が同時に書いても、
「あとから保存した側が先の更新を消す」ことが起きない。

SQLite（既定）… 1台のホストで動かすとき。ファイル1つ、追加の導入なし。
ネットワークドライブ上では使えない（WAL の制約）。

PostgreSQL … 複数ホストから同じ台帳を使うとき、またはネットワーク越しに
共有したいとき。テナントを**行**で分ける（`tenant` 列）ので、1つの DB に
複数テナントを同居させても、行は交わらない。書き込みの直列化は
`pg_advisory_xact_lock`（テナントごと）で行う — 別テナントの書き込みは
互いを待たない。

Where the ledger's rows live — SQLite or PostgreSQL. `TaskEngine` does not
know which; this module only reads, writes and serialises rows (`id → JSON
text`), and has no idea what a task means. Both honour the same contract:
`read()` returns a consistent copy, and `write()` is an exclusive transaction
handed the *latest* copy at entry, committing only the difference on a normal
exit and nothing on an exception. That is what keeps a concurrent writer — on
another process or another host — from erasing an earlier update.
"""
from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LedgerTenantError(Exception):
    """台帳の持ち主と違うテナントで開こうとした / opened by the wrong tenant."""


class LedgerConfigError(Exception):
    """保存先の設定が足りない・誤っている / the storage is misconfigured."""


@dataclass
class Snapshot:
    """ある時点の台帳。行は JSON 文字列 / the ledger at one moment."""

    tasks: dict[str, str] = field(default_factory=dict)
    outcomes: list[str] = field(default_factory=list)


class WriteTx(ABC):
    """書き込み取引。`snapshot` は入った時点の最新の写し。"""

    snapshot: Snapshot

    @abstractmethod
    def apply(self, upserts: dict[str, str], deletes: Iterable[str],
              outcomes: list[str], keep: int) -> None:
        """差分だけを書く。`keep` は残す完了実績の件数 / write only the difference."""


class LedgerStore(ABC):
    kind: str = "base"

    @abstractmethod
    def prepare(self, tenant: str | None,
                legacy: Callable[[], Snapshot | None] | None) -> None:
        """表を用意し、持ち主を確かめる。旧形式の台帳があれば一度だけ取り込む。"""

    @abstractmethod
    def read(self) -> Snapshot: ...

    @abstractmethod
    def write(self) -> Any:
        """`with store.write() as tx:` で使う排他的な書き込み取引。"""

    @abstractmethod
    def close(self) -> None: ...

    def describe(self) -> str:
        return self.kind


# =============================================================================
# SQLite
# =============================================================================

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcomes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class _SqliteTx(WriteTx):
    def __init__(self, conn: sqlite3.Connection, snapshot: Snapshot) -> None:
        self._conn = conn
        self.snapshot = snapshot

    def apply(self, upserts: dict[str, str], deletes: Iterable[str],
              outcomes: list[str], keep: int) -> None:
        conn = self._conn
        for task_id, data in upserts.items():
            conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (task_id, data))
        for task_id in deletes:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        for data in outcomes:
            conn.execute("INSERT INTO outcomes (data) VALUES (?)", (data,))
        if outcomes:
            conn.execute("DELETE FROM outcomes WHERE seq <= "
                         "(SELECT MAX(seq) FROM outcomes) - ?", (keep,))


class SqliteStore(LedgerStore):
    """SQLite（WAL）。接続はインスタンスごとに1本を使い回す。

    操作のたびに開いて閉じると、最後の接続を閉じるたびに WAL の
    チェックポイント（fsync）が走り、実測で 1 回 約 100ms かかった。

    One connection per instance: opening and closing per operation made every
    close checkpoint the WAL (an fsync) — about 100 ms each when measured.
    """

    kind = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def describe(self) -> str:
        return f"sqlite:{self.path}"

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: BEGIN/COMMIT は自分で書く。
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                   check_same_thread=False)
            # WAL では NORMAL で破損しない。電源断で直近のコミットを失う
            # ことはあるが、台帳は次の周で各テンプレートの出力から作り直せる。
            # In WAL, NORMAL cannot corrupt the file; a power cut may lose the
            # last commit, which the next cycle rebuilds from the templates.
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def prepare(self, tenant: str | None,
                legacy: Callable[[], Snapshot | None] | None) -> None:
        with self._lock:
            conn = self._db()
            self._enable_wal(conn)
            conn.executescript(_SQLITE_SCHEMA)
            if legacy is not None:
                self._import_legacy(conn, legacy)
            self._claim_tenant(conn, tenant)

    @staticmethod
    def _enable_wal(conn: sqlite3.Connection) -> None:
        """WAL に切り替える。すでに WAL なら何もしない。

        `PRAGMA journal_mode=WAL` は、ほかの接続が開いている最初の一回だけ
        busy タイムアウトが効かず、即座に「database is locked」で失敗する。
        複数プロセスを同時に起動すると実際に起きる（実測で 5 並列の起動 200 回中
        10 回）。すでに WAL か調べてから、競合したときだけ短い待ちで再試行する。

        `PRAGMA journal_mode=WAL` ignores the busy timeout the first time and
        fails at once with "database is locked" if another connection is open.
        Starting several processes together hits this (10 of 200 five-way
        starts when measured). So: check first, and retry briefly only on
        contention.
        """
        import time

        deadline = time.monotonic() + 30
        while True:
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def _import_legacy(self, conn: sqlite3.Connection,
                       legacy: Callable[[], Snapshot | None]) -> None:
        # 複数プロセスが同時に起動しても、取り込みは一度だけ。
        # Once only, even if several processes start at the same moment.
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = conn.execute(
                "SELECT 1 FROM meta WHERE key = 'legacy_imported'").fetchone()
            if not done:
                snapshot = legacy() or Snapshot()
                for task_id, data in snapshot.tasks.items():
                    conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)",
                                 (task_id, data))
                for data in snapshot.outcomes:
                    conn.execute("INSERT INTO outcomes (data) VALUES (?)", (data,))
                conn.execute("INSERT INTO meta VALUES ('legacy_imported', 'yes')")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _claim_tenant(self, conn: sqlite3.Connection, tenant: str | None) -> None:
        """台帳にテナントを刻印し、食い違う持ち主での起動を拒否する。

        別テナントの設定で同じ台帳ファイルを指してしまう事故（設定のコピー、
        ボリュームの取り違え）を、データが混ざる前に止める。刻印の無い既存の
        台帳は、最初に開いたテナントのものになる。

        Stamps the ledger with its tenant and refuses a different one, so a
        copied config or a mixed-up volume is stopped before any data mixes.
        """
        if tenant is None:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'tenant'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta VALUES ('tenant', ?)", (tenant,))
            owner = row[0] if row else tenant
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if owner != tenant:
            raise LedgerTenantError(
                f"この台帳はテナント '{owner}' のものです（'{tenant}' では開けません）: "
                f"{self.path} / this ledger belongs to tenant '{owner}', not '{tenant}'")

    @staticmethod
    def _load(conn: sqlite3.Connection) -> Snapshot:
        return Snapshot(
            tasks={task_id: data for task_id, data
                   in conn.execute("SELECT id, data FROM tasks")},
            outcomes=[data for (data,) in conn.execute(
                "SELECT data FROM outcomes ORDER BY seq")],
        )

    def read(self) -> Snapshot:
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN")     # 2つの SELECT を同じ時点で読む
            try:
                return self._load(conn)
            finally:
                conn.execute("COMMIT")

    @contextmanager
    def write(self) -> Iterator[WriteTx]:
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteTx(conn, self._load(conn))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass        # SQLite がすでに取り消していた / already rolled back
                raise


# =============================================================================
# PostgreSQL
# =============================================================================

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_tasks (
    tenant TEXT NOT NULL,
    id     TEXT NOT NULL,
    data   JSONB NOT NULL,
    PRIMARY KEY (tenant, id)
);
CREATE TABLE IF NOT EXISTS ledger_outcomes (
    seq    BIGSERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    data   JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_outcomes_tenant ON ledger_outcomes (tenant, seq);
CREATE TABLE IF NOT EXISTS ledger_meta (
    tenant TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (tenant, key)
);
"""


class _PostgresTx(WriteTx):
    def __init__(self, conn: Any, tenant: str, snapshot: Snapshot) -> None:
        self._conn = conn
        self._tenant = tenant
        self.snapshot = snapshot

    def apply(self, upserts: dict[str, str], deletes: Iterable[str],
              outcomes: list[str], keep: int) -> None:
        conn, tenant = self._conn, self._tenant
        with conn.cursor() as cur:
            if upserts:
                cur.executemany(
                    "INSERT INTO ledger_tasks (tenant, id, data) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (tenant, id) DO UPDATE SET data = EXCLUDED.data",
                    [(tenant, task_id, data) for task_id, data in upserts.items()])
            gone = list(deletes)
            if gone:
                cur.execute("DELETE FROM ledger_tasks WHERE tenant = %s AND id = ANY(%s)",
                            (tenant, gone))
            if outcomes:
                cur.executemany(
                    "INSERT INTO ledger_outcomes (tenant, data) VALUES (%s, %s::jsonb)",
                    [(tenant, data) for data in outcomes])
                # seq は全テナント共通の連番。このテナントの新しい keep 件を残す。
                # `seq` is shared by all tenants; keep this tenant's newest `keep`.
                cur.execute(
                    "DELETE FROM ledger_outcomes WHERE tenant = %s AND seq NOT IN "
                    "(SELECT seq FROM ledger_outcomes WHERE tenant = %s "
                    " ORDER BY seq DESC LIMIT %s)", (tenant, tenant, keep))


class PostgresStore(LedgerStore):
    """PostgreSQL。テナントは行で分ける。

    - 書き込み取引はテナントごとの `pg_advisory_xact_lock` で直列化する。
      別テナントは待たない。ロックは取引の終わりで自動的に外れる。
    - 読み取りは REPEATABLE READ で、2つの SELECT を同じ時点で読む。
    - 接続はインスタンスごとに1本を使い回し、切れていたら張り直す
      （PostgreSQL の再起動やネットワーク断のあとも続けられるように）。

    Tenants are separated by row. Writers are serialised per tenant with
    `pg_advisory_xact_lock` (another tenant never waits; the lock is released
    when the transaction ends). Reads run at REPEATABLE READ so the two SELECTs
    see one moment. One connection per instance, redialled if it has died.
    """

    kind = "postgres"

    def __init__(self, dsn: str, tenant: str | None, connection: Any = None) -> None:
        if not tenant:
            # 空のテナントで行を書くと、あとで誰のものか分からなくなる。
            raise LedgerConfigError(
                "台帳を PostgreSQL に置くには tenant の設定が必要です "
                "/ the PostgreSQL ledger requires a tenant in config.yaml")
        if not dsn and connection is None:
            raise LedgerConfigError(
                "PostgreSQL の接続先がありません。task_engine.dsn か "
                "adapters.postgres.dsn を設定してください "
                "/ no PostgreSQL DSN: set task_engine.dsn or adapters.postgres.dsn")
        self.dsn = dsn
        self.tenant = tenant
        self._conn = connection          # テストで注入できる / injectable in tests
        self._lock = threading.RLock()

    def describe(self) -> str:
        return f"postgres (tenant {self.tenant})"

    # -- 接続 / connection ------------------------------------------------

    def _connect(self) -> Any:
        import psycopg      # 遅延 import / lazy import

        conn = psycopg.connect(self.dsn, autocommit=True)
        return conn

    def _live(self) -> Any:
        """生きている接続を返す。切れていれば張り直す。"""
        import psycopg

        with self._lock:
            if self._conn is not None and not getattr(self._conn, "closed", False):
                try:
                    self._conn.execute("SELECT 1")
                    return self._conn
                except psycopg.Error:
                    self.close()
            try:
                self._conn = self._connect()
            except psycopg.Error as exc:
                raise LedgerConfigError(
                    f"PostgreSQL に接続できません / cannot connect to PostgreSQL: {exc}"
                ) from exc
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:           # noqa: BLE001
                    pass
                self._conn = None

    # -- 準備 / prepare ---------------------------------------------------

    def prepare(self, tenant: str | None,
                legacy: Callable[[], Snapshot | None] | None) -> None:
        # 旧 JSON の取り込みは SQLite の台帳だけが対象（PostgreSQL へは
        # `aipmo ledger migrate` で明示的に移す）。
        # Legacy JSON import is SQLite-only; PostgreSQL gets its rows through
        # the explicit `aipmo ledger migrate`.
        with self._lock:
            conn = self._live()
            with conn.transaction():
                # 複数プロセスの同時起動で CREATE が衝突しないよう直列化する。
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                             ("aipmo-ledger-ddl",))
                conn.execute(POSTGRES_SCHEMA)

    # -- 読み書き / read and write -----------------------------------------

    def _load(self, conn: Any) -> Snapshot:
        with conn.cursor() as cur:
            cur.execute("SELECT id, data::text FROM ledger_tasks WHERE tenant = %s",
                        (self.tenant,))
            tasks = {task_id: data for task_id, data in cur.fetchall()}
            cur.execute("SELECT data::text FROM ledger_outcomes WHERE tenant = %s "
                        "ORDER BY seq", (self.tenant,))
            outcomes = [data for (data,) in cur.fetchall()]
        return Snapshot(tasks=tasks, outcomes=outcomes)

    def read(self) -> Snapshot:
        with self._lock:
            conn = self._live()
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                return self._load(conn)

    @contextmanager
    def write(self) -> Iterator[WriteTx]:
        with self._lock:
            conn = self._live()
            # `transaction()` は正常に抜ければ COMMIT、例外なら ROLLBACK。
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                             (f"aipmo-ledger:{self.tenant}",))
                yield _PostgresTx(conn, self.tenant, self._load(conn))
