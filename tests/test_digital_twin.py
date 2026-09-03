"""Project Digital Twin のテスト / Project Digital Twin tests.

2つの層に分けて検証する:
  1. postgres アダプタ層: dt_ で始まる named query が正しく束縛されること
     （生成 SQL のドリフト検知。test_wbs_approval.py と同じ手法）。
  2. Web 層: 読み取り専用の2エンドポイント（/state・/diagnose）の
     200/404/503 とテナント分離。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from aipmo.adapters.postgres import PostgresAdapter

ROOT = Path(__file__).resolve().parents[1]
REAL_QUERIES = yaml.safe_load((ROOT / "queries.yaml").read_text(encoding="utf-8"))


# --- fakes（postgres 層。tests/test_wbs_approval.py と同一実装） ------------

class FakeCursor:
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


def test_dt_upsert_project_binds_tenant_and_upserts_on_jira_key():
    connection = FakeConnection(rows=[("proj-1",)], columns=["id"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    adapter.invoke("execute", {
        "name": "dt_upsert_project",
        "params": {"name": "PROJ", "description": "", "jira_project_key": "PROJ"},
    })

    sql, values = connection.log[0]
    assert "ON CONFLICT (tenant, jira_project_key) DO UPDATE" in sql
    assert "acme" in values and "PROJ" in values
    assert connection.commits == 1


def test_dt_get_project_binds_tenant_and_project_id():
    connection = FakeConnection(rows=[("proj-1", "PROJ")], columns=["id", "name"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    result = adapter.invoke("query", {
        "name": "dt_get_project", "params": {"project_id": "proj-1"},
    })

    sql, values = connection.log[0]
    assert "%s" in sql and ":tenant" not in sql and ":project_id" not in sql
    assert values == ["acme", "proj-1"]
    assert result["rows"][0]["id"] == "proj-1"


@pytest.mark.parametrize("query_name", [
    "dt_list_wbs_nodes", "dt_list_tasks", "dt_list_resources", "dt_list_risks",
    "dt_list_issues", "dt_list_dependencies", "dt_list_decisions",
    "dt_list_documents",
])
def test_dt_list_queries_bind_tenant_and_project_id(query_name: str):
    connection = FakeConnection(rows=[], columns=[])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    adapter.invoke("query", {"name": query_name, "params": {"project_id": "proj-1"}})

    sql, values = connection.log[0]
    assert values == ["acme", "proj-1"]


@pytest.mark.parametrize("query_name,column", [
    ("dt_latest_schedule_forecast", "variance_percent"),
    ("dt_get_budget", "variance_percent"),
])
def test_dt_single_row_queries_return_exactly_one_null_row_when_empty(
    query_name: str, column: str,
):
    """latest_forecast_snapshot と同じ保証: 行が無くても rows[0] を安全に
    参照できる（diagnose テンプレートが rows[0].variance_percent のような
    テンプレート式で直接読むため）。FakeConnection は実際の UNION ALL の
    NOT EXISTS 分岐を評価しないので、ここでは SQL 文にその形が含まれる
    ことだけを確認する — 実際の空行保証は本物の Postgres 上でのみ検証
    できる。
    """
    connection = FakeConnection(rows=[], columns=[])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    adapter.invoke("query", {"name": query_name, "params": {"project_id": "proj-1"}})

    sql, _ = connection.log[0]
    assert "UNION ALL" in sql and "NOT EXISTS" in sql
    assert column in sql


def test_dt_risk_exposure_total_binds_tenant_and_project_id():
    connection = FakeConnection(rows=[(0.0,)], columns=["total_exposure"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    result = adapter.invoke("query", {
        "name": "dt_risk_exposure_total", "params": {"project_id": "proj-1"},
    })

    sql, values = connection.log[0]
    assert values == ["acme", "proj-1"]
    assert result["rows"][0]["total_exposure"] == 0.0


def test_dt_record_health_diagnostic_binds_all_fields():
    connection = FakeConnection(rows=[("diag-1",)], columns=["id"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    adapter.invoke("execute", {
        "name": "dt_record_health_diagnostic",
        "params": {
            "project_id": "proj-1", "health_score": 70, "health_status": "Yellow",
            "rule_scores": {"schedule": 40}, "blockers": ["スケジュール遅延が25%"],
            "recommendations": [{"action": "a", "priority": "High", "impact": "b"}],
            "confidence": 0.7, "analysis_summary": "summary",
        },
    })

    sql, values = connection.log[0]
    assert "INSERT INTO dt_health_diagnostics" in sql
    assert "acme" in values and "proj-1" in values and 70 in values


def test_dt_upsert_task_is_a_write_query():
    from aipmo.adapters.base import AdapterError

    connection = FakeConnection()
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    with pytest.raises(AdapterError, match="書き込みクエリ"):
        adapter.invoke("query", {"name": "dt_upsert_task", "params": {}})


# --- テンプレートの読み込み ---------------------------------------------------
#
# ロード自体・プロンプトの存在確認は tests/test_templates.py が全テンプレート
# に対して自動的にカバーする。ここでは digital_twin 固有のステップ構成
# （transform ステップが project_health を呼んでいること）だけを確認する。

from aipmo.dsl.loader import load_file  # noqa: E402


def test_digital_twin_diagnose_template_calls_the_project_health_transform():
    template = load_file(
        ROOT / "templates" / "examples" / "digital_twin_diagnose.yaml"
    )
    assessment_step = next(s for s in template.steps if s.id == "assessment")
    assert assessment_step.expression == "project_health"


def test_digital_twin_sync_template_upserts_project_wbs_and_tasks():
    template = load_file(
        ROOT / "templates" / "examples" / "digital_twin_sync.yaml"
    )
    query_names = {
        s.inputs.get("name") for s in template.steps
        if s.adapter == "postgres"
    }
    assert query_names == {"dt_upsert_project", "dt_upsert_wbs_node", "dt_upsert_task"}


# --- Web 層 -----------------------------------------------------------------

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aipmo.adapters.base import AdapterRegistry  # noqa: E402
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
    """/state・/diagnose の分岐（200/404/503・テナント分離）だけを検証する
    ための単純な二重体。tests/test_wbs_approval.py の StubPostgres と同じ
    考え方。
    """

    name = "postgres"

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.diagnoses: dict[str, dict[str, Any]] = {}
        self.tenant_of: dict[str, str] = {}

    def health_check(self) -> bool:
        return True

    def _tenant_of(self, project_id: str) -> str:
        return self.tenant_of.get(project_id, DEFAULT_TENANT)

    def query(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        tenant = params.get("tenant")
        project_id = params.get("project_id")
        if name == "dt_get_project":
            row = self.projects.get(project_id)
            if row is None or self._tenant_of(project_id) != tenant:
                return {"rows": [], "count": 0}
            return {"rows": [row], "count": 1}
        if name in {
            "dt_list_wbs_nodes", "dt_list_tasks", "dt_list_resources",
            "dt_list_risks", "dt_list_issues", "dt_list_dependencies",
            "dt_list_decisions", "dt_list_documents",
        }:
            return {"rows": [], "count": 0}
        if name in {"dt_latest_schedule_forecast", "dt_get_budget"}:
            return {"rows": [{}], "count": 1}
        if name == "dt_latest_health_diagnostic":
            row = self.diagnoses.get(project_id)
            if row is None or self._tenant_of(project_id) != tenant:
                return {"rows": [], "count": 0}
            return {"rows": [row], "count": 1}
        raise AssertionError(f"unexpected query: {name}")


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
    return _build_client(postgres, tmp_path / "templates_other", "other_corp")


def _auth(token: str) -> dict[str, str]:
    return {"x-aipmo-token": token}


def test_viewer_can_read_project_state(client: TestClient, postgres: StubPostgres):
    postgres.projects["proj-1"] = {"id": "proj-1", "name": "PROJ"}

    response = client.get("/api/v1/projects/proj-1/state", headers=_auth(VIEWER))

    assert response.status_code == 200
    body = response.json()
    assert body["project"]["id"] == "proj-1"
    assert body["wbs_nodes"] == []


def test_project_state_404_when_project_missing(client: TestClient):
    response = client.get("/api/v1/projects/ghost/state", headers=_auth(OPERATOR))
    assert response.status_code == 404


def test_viewer_can_read_project_diagnosis(client: TestClient, postgres: StubPostgres):
    postgres.diagnoses["proj-1"] = {
        "id": "diag-1", "health_score": 70, "health_status": "Yellow",
    }

    response = client.get("/api/v1/projects/proj-1/diagnose", headers=_auth(VIEWER))

    assert response.status_code == 200
    assert response.json()["health_score"] == 70


def test_project_diagnosis_404_when_none_recorded_yet(
    client: TestClient, postgres: StubPostgres,
):
    postgres.projects["proj-1"] = {"id": "proj-1", "name": "PROJ"}

    response = client.get("/api/v1/projects/proj-1/diagnose", headers=_auth(OPERATOR))

    assert response.status_code == 404


def test_project_state_503_when_postgres_not_configured(tmp_path: Path):
    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    adapters = AdapterRegistry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    app = create_app(Engine(adapters, llms), templates_root, OPERATOR,
                      tenant="acme", lang="en", store=RunStore())
    client = TestClient(app)

    response = client.get("/api/v1/projects/proj-1/state", headers=_auth(OPERATOR))

    assert response.status_code == 503


def test_no_token_is_rejected_on_digital_twin_routes(client: TestClient):
    for path in ("/api/v1/projects/proj-1/state", "/api/v1/projects/proj-1/diagnose"):
        response = client.get(path)
        assert response.status_code == 401


def test_a_tenant_cannot_read_another_tenants_project_state(
    client: TestClient, other_tenant_client: TestClient, postgres: StubPostgres,
):
    postgres.projects["proj-1"] = {"id": "proj-1", "name": "PROJ"}
    postgres.tenant_of["proj-1"] = "other_corp"

    cross_tenant = client.get("/api/v1/projects/proj-1/state", headers=_auth(OPERATOR))
    own_tenant = other_tenant_client.get(
        "/api/v1/projects/proj-1/state", headers=_auth(OPERATOR))

    assert cross_tenant.status_code == 404
    assert own_tenant.status_code == 200


def test_a_tenant_cannot_read_another_tenants_diagnosis(
    client: TestClient, other_tenant_client: TestClient, postgres: StubPostgres,
):
    postgres.diagnoses["proj-1"] = {"id": "diag-1", "health_score": 70}
    postgres.tenant_of["proj-1"] = "other_corp"

    cross_tenant = client.get("/api/v1/projects/proj-1/diagnose", headers=_auth(OPERATOR))
    own_tenant = other_tenant_client.get(
        "/api/v1/projects/proj-1/diagnose", headers=_auth(OPERATOR))

    assert cross_tenant.status_code == 404
    assert own_tenant.status_code == 200
