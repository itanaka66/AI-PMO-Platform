"""WBS 再計画の承認ワークフローのテスト / WBS replan approval workflow tests.

2つの層に分けて検証する:
  1. postgres アダプタ層: 出荷される queries.yaml の named query が
     正しく束縛されること（生成 SQL のドリフト検知）。
  2. Web 層: 承認/却下エンドポイントの権限分離（operator のみ書き込める）
     と、pending でない提案への操作が 409 になること。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.postgres import PostgresAdapter

ROOT = Path(__file__).resolve().parents[1]
REAL_QUERIES = yaml.safe_load((ROOT / "queries.yaml").read_text(encoding="utf-8"))


# --- fakes（postgres 層） ---------------------------------------------------

class FakeCursor:
    """description を呼び出しごとに指定できる版。RETURNING 句のある
    書き込みクエリは呼び出しごとに列が違うため、固定の description を
    持つ既存の FakeCursor (tests/test_data_adapters.py) は使い回せない。
    """

    def __init__(self, log: list[tuple[str, list[Any]]],
                 rows: list[tuple], columns: list[str]) -> None:
        self._log = log
        self._rows = rows
        self.description = [(c,) for c in columns] if columns else None
        self.rowcount = len(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, values: list[Any] | None = None) -> None:
        self._log.append((sql, list(values or [])))

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, rows: list[tuple] | None = None,
                 columns: list[str] | None = None) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self.rows = rows or []
        self.columns = columns or []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.log, self.rows, self.columns)

    def commit(self):
        self.commits += 1


def test_pending_wbs_proposals_binds_tenant():
    connection = FakeConnection(
        rows=[("p1", "wbs-v3", {"add": []}, "reason", {}, 2, 0.8, "2026-01-01")],
        columns=["id", "wbs_version_from", "diff", "rationale", "assumptions",
                 "tier", "confidence", "created_at"],
    )
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme",
                               connection=connection)

    result = adapter.invoke("query", {"name": "pending_wbs_proposals",
                                       "params": {}})

    sql, values = connection.log[0]
    assert "%s" in sql and ":tenant" not in sql
    assert values == ["acme"]
    assert result["rows"][0]["tier"] == 2


def test_pending_wbs_proposals_is_read_only():
    from aipmo.adapters.base import AdapterError

    connection = FakeConnection()
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme",
                               connection=connection)

    with pytest.raises(AdapterError, match="書き込みクエリ"):
        adapter.invoke("query", {"name": "save_wbs_proposal", "params": {}})


def test_save_wbs_proposal_uses_idempotency_key_as_source_key():
    connection = FakeConnection(rows=[("p1",)], columns=["id"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme",
                               connection=connection)

    adapter.invoke("execute", {
        "name": "save_wbs_proposal",
        "params": {
            "id": "p1", "wbs_version_from": "v3", "diff": {"add": []},
            "rationale": "velocity dropped", "assumptions": {}, "tier": 2,
            "confidence": 0.8, "option_label": None,
        },
        "idempotency_key": "wbs-1:drift-signature-abc",
    })

    sql, values = connection.log[0]
    assert "ON CONFLICT (source_key) DO UPDATE" in sql
    assert "wbs-1:drift-signature-abc" in values
    assert connection.commits == 1


def test_decide_wbs_proposal_binds_status_and_actor():
    connection = FakeConnection(rows=[("p1", "approved")],
                                 columns=["id", "status"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme",
                               connection=connection)

    result = adapter.invoke("execute", {
        "name": "decide_wbs_proposal",
        "params": {"id": "p1", "status": "approved", "decided_by": "operator",
                   "decision_note": None},
    })

    sql, values = connection.log[0]
    assert "WHERE tenant = %s AND id = %s AND status = 'pending'" in sql
    assert "acme" in values and "p1" in values and "approved" in values
    assert result["rows"][0]["status"] == "approved"


def test_decide_wbs_proposal_only_touches_pending_rows_by_construction():
    connection = FakeConnection(rows=[], columns=["id", "status"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme",
                               connection=connection)

    adapter.invoke("execute", {
        "name": "decide_wbs_proposal",
        "params": {"id": "p1", "status": "rejected", "decided_by": "operator",
                   "decision_note": "budget already reallocated"},
    })

    sql, _ = connection.log[0]
    assert "status = 'pending'" in sql


# --- Web 層 -----------------------------------------------------------------

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aipmo.adapters.mock import (  # noqa: E402
    MockJiraAdapter,
    MockSlackAdapter,
    MockTeamsAdapter,
)
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR = "operator-token-value"
VIEWER = "viewer-token-value"


DEFAULT_TENANT = "acme"


class StubPostgres:
    """Web 層のテスト用の postgres アダプタの代わり。SQL の中身ではなく、
    エンドポイント側の分岐（200/404/409/503・権限）を検証したいので、
    ここでは戻り値を直接制御できる単純な二重体にする。

    tenant_of を明示的に設定しない限り、各行は DEFAULT_TENANT に属する
    ものとして扱う——既存のテストはすべて単一テナント前提で書かれている
    ので、その挙動をそのまま保つ。複数テナントを跨ぐ検証をしたいテストは
    tenant_of[id] = "他のテナント名" を設定する。
    """

    name = "postgres"

    def __init__(self) -> None:
        self.pending: dict[str, dict[str, Any]] = {}
        self.tenant_of: dict[str, str] = {}
        self.decisions: list[tuple[str, str, str, str | None]] = []

    def health_check(self) -> bool:
        return True

    def _tenant_of(self, proposal_id: str) -> str:
        return self.tenant_of.get(proposal_id, DEFAULT_TENANT)

    def query(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        tenant = params.get("tenant")
        if name == "pending_wbs_proposals":
            rows = [row for pid, row in self.pending.items()
                    if self._tenant_of(pid) == tenant]
            return {"rows": rows, "count": len(rows)}
        if name == "get_wbs_proposal":
            proposal_id = params["id"]
            row = self.pending.get(proposal_id)
            if row is None or self._tenant_of(proposal_id) != tenant:
                # 他テナントの行は「存在しない」のと同じ返り方にする。
                # 実クエリの WHERE tenant = :tenant AND id = :id も同じ形。
                return {"rows": [], "count": 0}
            return {"rows": [row], "count": 1}
        raise AssertionError(f"unexpected query: {name}")

    def execute(self, name: str, params: dict[str, Any] | None = None,
                idempotency_key: str | None = None) -> dict[str, Any]:
        assert name == "decide_wbs_proposal"
        params = params or {}
        proposal_id = params["id"]
        tenant = params.get("tenant")
        self.decisions.append((proposal_id, params["status"],
                                params["decided_by"], params.get("decision_note")))
        if proposal_id not in self.pending or self._tenant_of(proposal_id) != tenant:
            # 実クエリの WHERE tenant = :tenant AND id = :id AND status = 'pending'
            # と同じく、他テナントの行は対象 0 件として扱う（存在を漏らさない）。
            return {"affected": 0, "rows": []}
        row = self.pending.pop(proposal_id)
        row["status"] = params["status"]
        return {"affected": 1, "rows": [{"id": proposal_id, "status": params["status"]}]}


@pytest.fixture
def postgres() -> StubPostgres:
    return StubPostgres()


def _build_client(postgres: StubPostgres, templates_root: Path, tenant: str) -> TestClient:
    templates_root.mkdir(exist_ok=True)

    adapters = AdapterRegistry()
    adapters.register(MockTeamsAdapter())
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())
    adapters.register(postgres)

    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    return TestClient(create_app(
        Engine(adapters, llms), templates_root, OPERATOR,
        viewer_token=VIEWER, tenant=tenant, lang="en", store=RunStore(),
    ))


@pytest.fixture
def client(tmp_path: Path, postgres: StubPostgres) -> TestClient:
    return _build_client(postgres, tmp_path / "templates", DEFAULT_TENANT)


@pytest.fixture
def other_tenant_client(tmp_path: Path, postgres: StubPostgres) -> TestClient:
    """同じ postgres（＝実運用なら同じデータベース）を共有する、別テナント
    向けのサーバー・インスタンス。テナント分離が `tenant` の値そのもの
    から来ていて、トークンの違いから来ているのではないことを確かめる
    ため、トークンは client と同じものを使う。

    A server instance for a different tenant, sharing the same postgres
    (the same database, in a real deployment). Uses the same tokens as
    `client` on purpose — isolation should come from the `tenant` value
    itself, not from using a different credential.
    """
    return _build_client(postgres, tmp_path / "templates_other", "other_corp")


def _auth(token: str) -> dict[str, str]:
    return {"x-aipmo-token": token}


def test_viewer_can_list_pending_proposals(client: TestClient, postgres: StubPostgres):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.get("/api/wbs-proposals", headers=_auth(VIEWER))

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "p1"


def test_anonymous_request_is_rejected(client: TestClient):
    response = client.get("/api/wbs-proposals")
    assert response.status_code == 401


def test_viewer_cannot_approve(client: TestClient, postgres: StubPostgres):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.post("/api/wbs-proposals/p1/approve", headers=_auth(VIEWER))

    assert response.status_code == 403
    assert postgres.decisions == []


def test_viewer_cannot_reject(client: TestClient, postgres: StubPostgres):
    """approve だけでなく reject も、承認する権限が要る側の操作。

    決定そのもの（承認・却下のどちらであっても）を viewer にはさせない
    ——approve の拒否だけを見て安心すると、reject 側の分岐だけが
    抜け落ちていても気づけない。
    """
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.post("/api/wbs-proposals/p1/reject", headers=_auth(VIEWER))

    assert response.status_code == 403
    assert postgres.decisions == []


# --- 権限の総当たり / permission matrix -------------------------------------
#
# 承認画面が叩く4つの経路（一覧・詳細・承認・却下）それぞれについて、
# トークン無し・不正なトークン・viewer・operator の組み合わせを確かめる。
# 一覧だけ確認して他が抜けている、という穴を作らないため。
#
# The four routes the approval screen calls (list, detail, approve, reject),
# each checked against no token, a wrong token, a viewer token, and an
# operator token — so coverage of one route (the list) is never mistaken for
# coverage of all of them.

WBS_PROPOSAL_ROUTES = [
    ("GET", "/api/wbs-proposals"),
    ("GET", "/api/wbs-proposals/p1"),
    ("POST", "/api/wbs-proposals/p1/approve"),
    ("POST", "/api/wbs-proposals/p1/reject"),
]


@pytest.mark.parametrize("method,path", WBS_PROPOSAL_ROUTES)
def test_no_token_is_rejected_on_every_proposal_route(
    client: TestClient, postgres: StubPostgres, method: str, path: str,
):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.request(method, path)

    assert response.status_code == 401
    assert postgres.decisions == []


@pytest.mark.parametrize("method,path", WBS_PROPOSAL_ROUTES)
def test_a_wrong_token_is_rejected_on_every_proposal_route(
    client: TestClient, postgres: StubPostgres, method: str, path: str,
):
    """トークンが1文字でも違えば、viewer/operator のどちらの権限も持たない。"""
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.request(method, path, headers=_auth("wrong-token-entirely"))

    assert response.status_code == 401
    assert postgres.decisions == []


def test_viewer_can_view_a_single_proposal(client: TestClient, postgres: StubPostgres):
    """一覧だけでなく、詳細画面も viewer に開けること。"""
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending",
                              "diff": "some diff"}

    response = client.get("/api/wbs-proposals/p1", headers=_auth(VIEWER))

    assert response.status_code == 200
    assert response.json()["id"] == "p1"


def test_viewing_a_nonexistent_proposal_is_404(client: TestClient):
    response = client.get("/api/wbs-proposals/ghost", headers=_auth(OPERATOR))
    assert response.status_code == 404


def test_operator_can_approve(client: TestClient, postgres: StubPostgres):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.post("/api/wbs-proposals/p1/approve", headers=_auth(OPERATOR))

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert postgres.decisions == [("p1", "approved", "operator", None)]


def test_operator_can_reject_with_note(client: TestClient, postgres: StubPostgres):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    response = client.post(
        "/api/wbs-proposals/p1/reject", headers=_auth(OPERATOR),
        json={"note": "budget already reallocated"},
    )

    assert response.status_code == 200
    assert postgres.decisions == [
        ("p1", "rejected", "operator", "budget already reallocated"),
    ]


def test_approving_nonexistent_proposal_is_409(client: TestClient):
    response = client.post("/api/wbs-proposals/ghost/approve", headers=_auth(OPERATOR))
    assert response.status_code == 409


def test_approving_already_decided_proposal_is_409(client: TestClient, postgres: StubPostgres):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}
    client.post("/api/wbs-proposals/p1/approve", headers=_auth(OPERATOR))

    response = client.post("/api/wbs-proposals/p1/approve", headers=_auth(OPERATOR))

    assert response.status_code == 409


def test_wbs_proposals_503_when_postgres_not_configured(tmp_path: Path):
    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    adapters = AdapterRegistry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    app = create_app(Engine(adapters, llms), templates_root, OPERATOR,
                      tenant="acme", lang="en", store=RunStore())
    client = TestClient(app)

    response = client.get("/api/wbs-proposals", headers=_auth(OPERATOR))

    assert response.status_code == 503


# --- 実テナントでの検証 / verification against a real second tenant --------
#
# ここまでのテストはすべて単一テナント（"acme"）を前提にしていた。
# 分離が本当に効いているかは、同じデータベースを共有する別テナントを
# 実際に立てて確かめないと分からない——SQL 文に `tenant = %s` が
# 含まれることは test_pending_wbs_proposals_binds_tenant で確認済みだが、
# それだけでは「他テナントの行が実際に見えない」ことの証明にならない。
#
# Every test above assumed a single tenant ("acme"). Whether isolation
# actually holds can only be shown by standing up a second tenant that
# shares the same database — confirming the SQL text contains
# `tenant = %s` (already done in test_pending_wbs_proposals_binds_tenant)
# is not the same as proving another tenant's row is actually invisible.

def test_a_tenant_cannot_list_another_tenants_proposals(
    client: TestClient, other_tenant_client: TestClient, postgres: StubPostgres,
):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}
    postgres.tenant_of["p1"] = "other_corp"

    acme_view = client.get("/api/wbs-proposals", headers=_auth(OPERATOR))
    other_view = other_tenant_client.get("/api/wbs-proposals", headers=_auth(OPERATOR))

    assert acme_view.json()["items"] == []
    assert [item["id"] for item in other_view.json()["items"]] == ["p1"]


def test_a_tenant_cannot_view_another_tenants_proposal_detail(
    client: TestClient, other_tenant_client: TestClient, postgres: StubPostgres,
):
    """存在自体を漏らさない — 403 ではなく 404。"""
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}
    postgres.tenant_of["p1"] = "other_corp"

    cross_tenant = client.get("/api/wbs-proposals/p1", headers=_auth(OPERATOR))
    own_tenant = other_tenant_client.get("/api/wbs-proposals/p1", headers=_auth(OPERATOR))

    assert cross_tenant.status_code == 404
    assert own_tenant.status_code == 200


def test_a_tenant_cannot_approve_another_tenants_proposal(
    client: TestClient, other_tenant_client: TestClient, postgres: StubPostgres,
):
    """自テナントの operator であっても、他テナントの提案は承認できない。

    トークンは client と other_tenant_client で同じものを使っている
    （両方とも OPERATOR）。それでも分離が効くのは、拒否がロールではなく
    `tenant` の値そのものから来ているということ。
    """
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}
    postgres.tenant_of["p1"] = "other_corp"

    response = client.post("/api/wbs-proposals/p1/approve", headers=_auth(OPERATOR))

    assert response.status_code == 409
    # decide_wbs_proposal 自体は呼ばれた（監査ログには残る）が、
    # 対象0件で他テナントの行を書き換えてはいない。
    assert postgres.decisions == [("p1", "approved", "operator", None)]
    assert postgres.pending["p1"]["status"] == "pending"

    # 本来のテナント側からは、これまでどおり承認できる。
    own_tenant = other_tenant_client.post(
        "/api/wbs-proposals/p1/approve", headers=_auth(OPERATOR))
    assert own_tenant.status_code == 200
    assert own_tenant.json()["status"] == "approved"


def test_proposals_from_both_tenants_never_mix_in_one_listing(
    client: TestClient, other_tenant_client: TestClient, postgres: StubPostgres,
):
    postgres.pending["p1"] = {"id": "p1", "tier": 1, "status": "pending"}
    postgres.pending["p2"] = {"id": "p2", "tier": 2, "status": "pending"}
    postgres.tenant_of["p2"] = "other_corp"
    # p1 は tenant_of を設定しないので DEFAULT_TENANT ("acme") のまま。

    acme_ids = {item["id"] for item in
                client.get("/api/wbs-proposals", headers=_auth(OPERATOR)).json()["items"]}
    other_ids = {item["id"] for item in
                 other_tenant_client.get("/api/wbs-proposals",
                                        headers=_auth(OPERATOR)).json()["items"]}

    assert acme_ids == {"p1"}
    assert other_ids == {"p2"}
    assert acme_ids.isdisjoint(other_ids)


# --- 監査ログ / audit logging ------------------------------------------------
#
# DB の decided_by/decided_at/decision_note だけでは、テナント単位のクエリを
# 打たない限り誰も気づけない。決定はアプリのログにも残し、既存の監視・
# 集約基盤がそのまま拾えるようにする。
#
# The DB's own decided_by/decided_at/decision_note is invisible to anyone
# who isn't running a tenant-scoped query. Decisions are also written to the
# application log, so whatever monitoring/aggregation pipeline already
# watches this process picks them up without special-casing this endpoint.

def test_approval_is_written_to_the_audit_log(client: TestClient,
                                               postgres: StubPostgres, caplog):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    with caplog.at_level("INFO", logger="aipmo.web"):
        client.post("/api/wbs-proposals/p1/approve", headers=_auth(OPERATOR))

    assert any("p1" in r.message and "approved" in r.message and "operator" in r.message
               for r in caplog.records)


def test_rejection_note_is_written_to_the_audit_log(client: TestClient,
                                                     postgres: StubPostgres, caplog):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    with caplog.at_level("INFO", logger="aipmo.web"):
        client.post("/api/wbs-proposals/p1/reject", headers=_auth(OPERATOR),
                    json={"note": "budget already reallocated"})

    assert any("budget already reallocated" in r.message for r in caplog.records)


def test_a_viewer_attempting_to_approve_is_logged(client: TestClient,
                                                   postgres: StubPostgres, caplog):
    postgres.pending["p1"] = {"id": "p1", "tier": 2, "status": "pending"}

    with caplog.at_level("WARNING", logger="aipmo.web"):
        client.post("/api/wbs-proposals/p1/approve", headers=_auth(VIEWER))

    assert any("permission denied" in r.message and "viewer" in r.message
               for r in caplog.records)


def test_an_invalid_token_is_logged_without_the_token_itself(client: TestClient, caplog):
    with caplog.at_level("WARNING", logger="aipmo.web"):
        client.get("/api/wbs-proposals", headers=_auth("not-a-real-token"))

    assert any("auth failed" in r.message for r in caplog.records)
    assert not any("not-a-real-token" in r.message for r in caplog.records)
