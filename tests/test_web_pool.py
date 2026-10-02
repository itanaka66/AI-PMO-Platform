"""Web の台帳の接続プール（aipmo/web/pool.py）のテスト。

確かめること:
  (1) 使い回す：順番に使えば作るのは 1 つ。借りるたびに最新を読み直す。返すとき状態を空に戻す
  (2) 上限：多数が同時に来ても、作る数は上限を超えない。同時に同じものを 2 人が使わない
  (3) 待つ・断る：全部使用中なら順番を待ち、待っても空かなければ 503（Retry-After つき）で断る。固まらない
  (4) 壊れたものは捨てる／作成に失敗しても枠を失わない／使われないものは閉じる／閉じたら使えない
  (5) Web につないでも動く：多数の同時アクセスが全部成功し、接続数が上限内。health に状況が出る
  (6) 設定（web.pool）の検証。size: 0 でプールを使わない

What matters: reuse and re-reading; never more than `size`; exclusive leases; waiting then a prompt 503
instead of hanging; broken ones dropped and a failed creation never leaks a slot; idle ones closed; a
real concurrent burst on the web all succeeds within the bound; config validated; size 0 disables.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.pmo_core import Member, PmoCore
from aipmo.task_engine import TaskEngine
from aipmo.web.pool import LedgerPool, PoolExhausted

PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")


class FakeEngine:
    """TaskEngine の代わり。使われ方だけを数える。"""
    created = 0
    live = 0
    peak = 0
    lock = threading.Lock()

    def __init__(self, broken: bool = False) -> None:
        with FakeEngine.lock:
            FakeEngine.created += 1
            FakeEngine.live += 1
            FakeEngine.peak = max(FakeEngine.peak, FakeEngine.live)
        self.syncs = 0
        self.closed = False
        self.broken = broken
        self.users = 0
        self.label_bonus = {"x": 1}
        self.priority_delta = {"High": 5}
        self.pace = {"team": 1.0}

    def sync(self) -> None:
        self.syncs += 1
        if self.broken:
            raise ConnectionError("connection is dead")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with FakeEngine.lock:
                FakeEngine.live -= 1


@pytest.fixture(autouse=True)
def reset():
    FakeEngine.created = FakeEngine.live = FakeEngine.peak = 0


def pool(**kw) -> LedgerPool:
    return LedgerPool(FakeEngine, **kw)        # type: ignore[arg-type]


# ===== (1) 使い回す / reuse ===========================================================================

def test_sequential_leases_reuse_one_engine_and_re_read_each_time():
    p = pool(size=4)
    seen = []
    for _ in range(5):
        with p.lease() as engine:
            seen.append(engine)
    assert FakeEngine.created == 1 and len({id(e) for e in seen}) == 1
    assert seen[0].syncs == 4                                     # 2 回目から借りるたびに読み直す
    stats = p.stats()
    assert stats["created"] == 1 and stats["reused"] == 4 and stats["leases"] == 5
    assert stats["idle"] == 1 and stats["in_use"] == 0


def test_an_engine_is_returned_clean_so_one_request_cannot_leak_state_into_the_next():
    p = pool(size=2)
    with p.lease() as engine:
        engine.label_bonus = {"bug": 9}
        engine.priority_delta = {"High": 3}
        engine.pace = {"team": 2}
    with p.lease() as again:
        assert again is engine
        assert (again.label_bonus, again.priority_delta, again.pace) == ({}, {}, {})


# ===== (2) 上限 / the bound =============================================================================

def test_a_burst_never_creates_more_than_the_bound_and_no_engine_is_shared():
    p = pool(size=4, acquire_timeout=30)
    errors: list[str] = []

    def work():
        for _ in range(20):
            with p.lease() as engine:
                engine.users += 1
                if engine.users != 1:
                    errors.append("shared")
                time.sleep(0.001)
                engine.users -= 1

    threads = [threading.Thread(target=work) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []                                           # 同時に 2 人が使わない
    assert FakeEngine.peak <= 4 and FakeEngine.created <= 4
    stats = p.stats()
    assert stats["leases"] == 24 * 20 and stats["in_use"] == 0 and stats["timeouts"] == 0


# ===== (3) 待つ・断る / waiting and refusing =============================================================

def test_when_everything_is_in_use_the_next_caller_waits_and_is_served_on_release():
    p = pool(size=1, acquire_timeout=5)
    first = p.acquire()
    got: list[object] = []
    waiter = threading.Thread(target=lambda: got.append(p.acquire()))
    waiter.start()
    time.sleep(0.1)
    assert p.stats()["waiting"] == 1 and not got
    p.release(first)
    waiter.join(timeout=5)
    assert got and got[0] is first                                # 待っていた人が受け取る
    assert FakeEngine.created == 1


def test_a_caller_that_waits_too_long_is_refused_promptly_not_hung():
    p = pool(size=1, acquire_timeout=0.2)
    held = p.acquire()
    started = time.monotonic()
    with pytest.raises(PoolExhausted, match="1"):
        p.acquire()
    assert time.monotonic() - started < 2
    assert p.stats()["timeouts"] == 1
    p.release(held)
    with p.lease():                                               # 空けば、また使える
        pass


# ===== (4) 壊れる・失敗する・古くなる / failures ===========================================================

def test_a_request_that_failed_on_a_dead_connection_discards_the_engine():
    p = pool(size=2)
    with pytest.raises(RuntimeError):
        with p.lease() as engine:
            engine.broken = True                                  # この接続はもう使えない
            raise RuntimeError("boom")
    assert engine.closed and p.stats()["discarded"] == 1 and p.stats()["idle"] == 0
    with p.lease() as fresh:
        assert fresh is not engine                                # 次は新しく作る
    assert FakeEngine.created == 2


def test_a_request_that_failed_on_a_healthy_engine_keeps_it():
    p = pool(size=2)
    with pytest.raises(KeyError):
        with p.lease() as engine:
            raise KeyError("404")
    assert not engine.closed and p.stats()["idle"] == 1
    with p.lease() as again:
        assert again is engine


def test_a_pooled_engine_that_died_while_idle_is_replaced_transparently():
    p = pool(size=2)
    with p.lease() as engine:
        pass
    engine.broken = True                                          # 待機中に切れた
    with p.lease() as replacement:
        assert replacement is not engine and not replacement.broken
    assert engine.closed and p.stats()["discarded"] == 1
    assert p.stats()["in_use"] == 0


def test_a_failing_factory_does_not_leak_a_slot():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise OSError("cannot connect")
        return FakeEngine()

    p = LedgerPool(flaky, size=1, acquire_timeout=0.2)             # type: ignore[arg-type]
    for _ in range(2):
        with pytest.raises(OSError):
            p.acquire()
    with p.lease():                                               # 枠は失われていない
        pass
    assert p.stats()["in_use"] == 0 and p.stats()["timeouts"] == 0


def test_idle_engines_are_closed_after_the_idle_limit():
    now = {"t": 0.0}
    p = pool(size=4, max_idle=60, clock=lambda: now["t"])
    a = p.acquire()
    b = p.acquire()
    p.release(a)
    now["t"] = 30
    p.release(b)
    now["t"] = 70                                                 # a は 70 秒、b は 40 秒
    with p.lease() as engine:
        assert engine is b
    assert a.closed and not b.closed and p.stats()["discarded"] == 1


def test_closing_the_pool_closes_idle_engines_and_refuses_new_leases():
    p = pool(size=3)
    held = p.acquire()
    with p.lease() as idle:
        pass
    p.close()
    assert idle.closed and not held.closed
    with pytest.raises(PoolExhausted, match="閉じて"):
        p.acquire()
    p.release(held)
    assert held.closed                                            # 借りていたものは、返った時に閉じる


def test_size_zero_disables_pooling_and_a_negative_size_is_invalid():
    p = pool(size=0)
    with p.lease() as engine:
        pass
    with p.lease() as other:
        assert other is not engine
    assert engine.closed and other.closed and FakeEngine.created == 2
    with pytest.raises(ValueError):
        pool(size=-1)


# ===== (5) Web につないで / on the web ==================================================================

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.adapters.base import AdapterRegistry  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR = "operator-token-1"


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None, "priority": None,
            "status": None, "blocked": False, "done": False, "labels": []}
    return {**base, **kw}


def seeded_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "task-ledger.db"
    te = TaskEngine(path)
    te.ingest("t", "r", [cand(key=f"P-{i}", title=f"課題{i}", assignee="ann", project="P")
                         for i in range(1, 6)])
    PmoCore(task_engine=te, members=[Member("ann")]).cycle()
    te.close()
    return path


def make_client(ledger: Path, tmp_path: Path, **kw) -> TestClient:
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", OPERATOR, lang="en",
                     store=RunStore(), pmo_ledger=ledger, **kw)
    return TestClient(app)


def headers():
    return {"x-aipmo-token": OPERATOR}


def test_a_concurrent_burst_all_succeeds_within_the_bound_and_health_shows_it(tmp_path):
    client = make_client(seeded_ledger(tmp_path), tmp_path, pool_size=3, pool_timeout=30)
    statuses: list[int] = []
    bodies: list[int] = []

    def hit():
        for _ in range(8):
            r = client.get("/api/pmo", headers=headers())
            statuses.append(r.status_code)
            bodies.append(len(r.json()["tasks"]))

    threads = [threading.Thread(target=hit) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(statuses) == {200} and set(bodies) == {5}
    pools = client.get("/api/health", headers=headers()).json()["ledger_pool"]
    assert len(pools) == 1
    stats = pools[0]
    assert stats["created"] <= 3 and stats["in_use"] == 0 and stats["timeouts"] == 0
    assert stats["leases"] >= 96 and stats["reused"] >= stats["leases"] - 3


def test_a_pooled_ledger_still_sees_what_another_process_wrote(tmp_path):
    ledger = seeded_ledger(tmp_path)
    client = make_client(ledger, tmp_path, pool_size=2)
    assert len(client.get("/api/pmo", headers=headers()).json()["tasks"]) == 5
    other = TaskEngine(ledger)                                  # 別のプロセス（常駐など）が書く
    other.ingest("t", "r2", [cand(key="P-99", title="あとから来た", project="P")])
    other.close()
    after = client.get("/api/pmo", headers=headers()).json()["tasks"]
    assert len(after) == 6 and any(t["title"] == "あとから来た" for t in after)


def test_when_the_pool_is_exhausted_the_web_answers_503_with_retry_after_without_hanging(
        tmp_path, monkeypatch):
    client = make_client(seeded_ledger(tmp_path), tmp_path, pool_size=1, pool_timeout=0.3)
    entered = threading.Event()
    real = TaskEngine.ranked

    def slow(self, *a, **k):
        entered.set()
        time.sleep(1.2)
        return real(self, *a, **k)

    monkeypatch.setattr(TaskEngine, "ranked", slow)
    first: list[int] = []
    t = threading.Thread(target=lambda: first.append(
        client.get("/api/pmo", headers=headers()).status_code))
    t.start()
    assert entered.wait(5)
    started = time.monotonic()
    refused = client.get("/api/pmo/decisions?limit=5", headers=headers())
    assert refused.status_code == 503 and refused.headers.get("retry-after") == "2"
    assert "1" in refused.json()["detail"] and time.monotonic() - started < 1.0
    t.join(timeout=10)
    assert first == [200]                                         # 先の人は最後まで処理される
    monkeypatch.undo()
    assert client.get("/api/pmo", headers=headers()).status_code == 200      # 空けば、また通る
    stats = client.get("/api/health", headers=headers()).json()["ledger_pool"][0]
    assert stats["timeouts"] >= 1


def test_size_zero_on_the_web_opens_and_closes_every_time(tmp_path):
    client = make_client(seeded_ledger(tmp_path), tmp_path, pool_size=0)
    for _ in range(3):
        assert client.get("/api/pmo", headers=headers()).status_code == 200
    stats = client.get("/api/health", headers=headers()).json()["ledger_pool"][0]
    assert stats["size"] == 0 and stats["created"] >= 3 and stats["idle"] == 0


def test_a_tenant_mismatch_is_still_a_clear_503_and_does_not_leak_a_slot(tmp_path):
    path = tmp_path / "task-ledger.db"
    TaskEngine(path, tenant="someone-else").close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", OPERATOR, lang="en",
                     store=RunStore(), pmo_ledger=path, tenant="acme", pool_size=1, pool_timeout=0.3)
    client = TestClient(app)
    for _ in range(3):                                           # 失敗を重ねても枠は減らない
        r = client.get("/api/pmo", headers=headers())
        assert r.status_code == 503 and "someone-else" in r.json()["detail"]
    assert client.get("/api/health", headers=headers()).json()["ledger_pool"][0]["in_use"] == 0


# ===== (6) 設定 / config ================================================================================

def test_web_pool_settings_have_defaults_and_are_validated():
    assert cli.web_pool_settings({}) == {"pool_size": 8, "pool_timeout": 10.0, "pool_idle": 300.0}
    custom = cli.web_pool_settings({"pool": {"size": 4, "timeout_seconds": 2.5, "idle_seconds": 60}})
    assert custom == {"pool_size": 4, "pool_timeout": 2.5, "pool_idle": 60.0}
    assert cli.web_pool_settings({"pool": {"size": 0}})["pool_size"] == 0
    for bad in ({"pool": "x"}, {"pool": {"size": -1}}, {"pool": {"size": 1000}},
                {"pool": {"size": True}}, {"pool": {"size": "8"}}, {"pool": {"timeout_seconds": 0}},
                {"pool": {"idle_seconds": 0}}):
        with pytest.raises(cli.ConfigError):
            cli.web_pool_settings(bad)


# ===== 実 PostgreSQL / a real PostgreSQL ==================================================================

needs_pg = pytest.mark.skipif(not PG_DSN, reason="AIPMO_TEST_PG_DSN が未設定")


def pg_world(tmp_path: Path, tenant: str, **kw):
    from aipmo.ledger_store import PostgresStore

    def factory():
        return PostgresStore(PG_DSN, tenant)

    te = TaskEngine(tmp_path / "task-ledger.db", tenant=tenant, store=factory())
    te.ingest("t", "r", [cand(key=f"P-{i}", title=f"課題{i}", assignee="ann", project="P")
                         for i in range(1, 4)])
    PmoCore(task_engine=te, members=[Member("ann")]).cycle()
    te.close()
    return make_client(tmp_path / "task-ledger.db", tmp_path, ledger_store_factory=factory,
                       tenant=tenant, **kw)


def cleanup_pg(tenant: str) -> None:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta", "ledger_side_docs",
                      "ledger_side_log"):
            conn.execute(f"DELETE FROM {table} WHERE tenant = %s", (tenant,))


def connection_count() -> int:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        return conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid()").fetchone()[0]


@needs_pg
def test_postgres_connections_stay_within_the_bound_under_a_burst(tmp_path):
    tenant = f"t{uuid.uuid4().hex[:12]}"
    try:
        baseline = connection_count()
        client = pg_world(tmp_path, tenant, pool_size=3, pool_timeout=30)
        peak = {"n": 0}
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                peak["n"] = max(peak["n"], connection_count())
                time.sleep(0.02)

        watcher = threading.Thread(target=watch)
        watcher.start()
        statuses: list[int] = []

        def hit():
            for _ in range(5):
                statuses.append(client.get("/api/pmo", headers=headers()).status_code)

        threads = [threading.Thread(target=hit) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        watcher.join()
        assert set(statuses) == {200}
        assert peak["n"] - baseline <= 3 + 2        # プール 3 + 監視と準備の分（余裕 2）
    finally:
        cleanup_pg(tenant)


@needs_pg
def test_postgres_pool_recovers_after_the_server_kills_its_connections(tmp_path):
    import psycopg

    tenant = f"t{uuid.uuid4().hex[:12]}"
    try:
        client = pg_world(tmp_path, tenant, pool_size=2)
        assert client.get("/api/pmo", headers=headers()).status_code == 200
        with psycopg.connect(PG_DSN, autocommit=True) as conn:       # 再起動やネットワーク断のかわり
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        for _ in range(3):
            assert client.get("/api/pmo", headers=headers()).status_code == 200
    finally:
        cleanup_pg(tenant)
