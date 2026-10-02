"""Web の台帳の接続プール。

画面（`aipmo serve`）は、リクエストのたびに台帳を開いて閉じていた。PostgreSQL では、そのたびに
新しい接続を張り（TLS・認証を含む）、表の存在確認（DDL）を走らせ、閉じる。多数が同時にアクセスすると、

- 同時のリクエストの数だけ接続が増える（PostgreSQL の `max_connections` を使い切って、画面だけでなく
  常駐の `aipmo schedule` まで接続できなくなる）、
- 1 回ごとの接続の張り直しで遅くなる、

という問題があった。SQLite でも、閉じるたびに WAL のチェックポイント（実測で約 100ms）が走った。

このプールは、台帳（`TaskEngine`）を**上限つきで使い回す**：

- **上限**（`size`）を超えて接続しない。全部使用中なら、`acquire_timeout` 秒まで順番を待つ。
  それでも空かなければ 503（`Retry-After` つき）で断る。待ち続けて固まることはない。
- **借りるたびに最新を読み直す**（`sync()`）。使い回しても、古い台帳を見せない。
- **借りた間だけ自分のもの。** 同時に 2 つのリクエストが同じものを使うことは無い（台帳は 1 スレッド 1 本で使う）。
  返すとき、学習の補正など書き換えられる状態を空に戻す。次のリクエストに前の状態を持ち越さない。
- **壊れたものは捨てる。** リクエストが例外で終わったとき、台帳を読み直せなければ（接続が切れたなど）
  そのものを捨てて、次は新しく作る。借りるときに読み直しに失敗した使い回しのものも、一度だけ作り直す。
- **使われないものは閉じる。** `max_idle` 秒使われなかったものは閉じる。
- `size: 0` でプールを使わない（従来どおり、毎回開いて閉じる）。

Web ledger connection pool. The screen used to open and close the ledger on every request: with
PostgreSQL a fresh connection (auth, TLS) plus DDL each time, one connection per concurrent request
(enough to exhaust `max_connections` and lock the resident `aipmo schedule` out), and with SQLite a WAL
checkpoint on each close. This pool reuses ledgers up to a bound: never more than `size`; the rest wait
up to `acquire_timeout`, then get a 503; every lease re-reads the ledger; each lease is exclusive and
returned clean; a broken one is dropped; idle ones are closed; `size: 0` disables pooling.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from ..task_engine import TaskEngine


class PoolExhausted(RuntimeError):
    """上限まで使われていて、待っても空かなかった。"""


class LedgerPool:
    def __init__(self, factory: Callable[[], TaskEngine], *, size: int = 8,
                 acquire_timeout: float = 10.0, max_idle: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if size < 0:
            raise ValueError("size は 0 以上 / size must be >= 0")
        self.factory = factory
        self.size = size
        self.acquire_timeout = acquire_timeout
        self.max_idle = max_idle
        self.clock = clock
        self._cond = threading.Condition()
        self._idle: list[tuple[TaskEngine, float]] = []
        self._live = 0                # 作ってあるもの（使用中 + 待機中 + 作成中）
        self._waiting = 0
        self._closed = False
        self._stats = {"created": 0, "reused": 0, "discarded": 0, "timeouts": 0, "leases": 0}

    # -- 借りる / acquire -----------------------------------------------------------------

    def acquire(self, sync: bool = True) -> TaskEngine:
        """借りる。`sync=False` なら、使い回したものの読み直しを省く（台帳の中身を使わないとき）。"""
        if self.size == 0:                               # プールを使わない
            with self._cond:
                self._stats["leases"] += 1
                self._stats["created"] += 1
            return self.factory()
        deadline = self.clock() + self.acquire_timeout
        stale: list[TaskEngine] = []
        try:
            with self._cond:
                while True:
                    if self._closed:
                        raise PoolExhausted("プールは閉じています / the pool is closed")
                    stale += self._expire_locked()
                    if self._idle:
                        engine, _ = self._idle.pop()
                        self._stats["reused"] += 1
                        reuse = True
                        break
                    if self._live < self.size:
                        self._live += 1                  # 作る枠を先に確保（作成は錠の外で）
                        reuse = False
                        break
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        self._stats["timeouts"] += 1
                        raise PoolExhausted(
                            f"台帳の接続が上限（{self.size}）まで使われています "
                            f"/ all {self.size} ledger connections are in use")
                    self._waiting += 1
                    try:
                        self._cond.wait(remaining)
                    finally:
                        self._waiting -= 1
        finally:
            for old in stale:
                self._close(old)

        if reuse:
            try:
                if sync:
                    engine.sync()                        # 使い回しでも、最新を見せる
            except Exception:                            # noqa: BLE001 — 切れていたら作り直す
                self._discard(engine)
                return self._create()
            with self._cond:
                self._stats["leases"] += 1
            return engine
        return self._create(reserved=True)

    def _create(self, reserved: bool = False) -> TaskEngine:
        if not reserved:
            with self._cond:
                self._live += 1
        try:
            engine = self.factory()
        except BaseException:
            with self._cond:
                self._live -= 1
                self._cond.notify()
            raise
        with self._cond:
            self._stats["created"] += 1
            self._stats["leases"] += 1
        return engine

    # -- 返す / release -----------------------------------------------------------------------

    def release(self, engine: TaskEngine, *, failed: bool = False) -> None:
        if self.size == 0:
            self._close(engine)
            return
        healthy = True
        if failed:
            try:
                engine.sync()                            # 読み直せなければ、壊れている
            except Exception:                            # noqa: BLE001
                healthy = False
        if healthy:
            # 前のリクエストの状態を持ち越さない。
            engine.label_bonus, engine.priority_delta, engine.pace = {}, {}, {}
        with self._cond:
            if healthy and not self._closed:
                self._idle.append((engine, self.clock()))
                self._cond.notify()
                return
        self._discard(engine)

    def _discard(self, engine: TaskEngine) -> None:
        self._close(engine)
        with self._cond:
            self._live -= 1
            self._stats["discarded"] += 1
            self._cond.notify()

    @staticmethod
    def _close(engine: TaskEngine) -> None:
        try:
            engine.close()
        except Exception:                                # noqa: BLE001
            pass

    def _expire_locked(self) -> list[TaskEngine]:
        """使われないまま長く待っているものを外す（閉じるのは錠の外で）。"""
        now = self.clock()
        keep: list[tuple[TaskEngine, float]] = []
        drop: list[tuple[TaskEngine, float]] = []
        for engine, since in self._idle:
            (drop if now - since > self.max_idle else keep).append((engine, since))
        if drop:
            self._idle = keep
            self._live -= len(drop)
            self._stats["discarded"] += len(drop)
        return [engine for engine, _ in drop]

    # -- 使いやすく / convenience -----------------------------------------------------------------

    @contextmanager
    def lease(self) -> Any:
        engine = self.acquire()
        failed = False
        try:
            yield engine
        except BaseException:
            failed = True
            raise
        finally:
            self.release(engine, failed=failed)

    def stats(self) -> dict[str, Any]:
        with self._cond:
            idle = len(self._idle)
            return {"size": self.size, "idle": idle, "in_use": max(0, self._live - idle),
                    "waiting": self._waiting, **self._stats}

    def close(self) -> None:
        with self._cond:
            self._closed = True
            engines = [engine for engine, _ in self._idle]
            self._live -= len(engines)
            self._idle = []
            self._cond.notify_all()
        for engine in engines:
            self._close(engine)
