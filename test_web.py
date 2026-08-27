"""Web サーバーのテスト / web server tests.

ネットワークに開く以上、認証と経路の検証が主眼。
Opening a listener makes auth and path handling the things worth testing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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
from aipmo.web.server import RunStore, create_app, discover_templates  # noqa: E402

TOKEN = "test-token-value"

SIMPLE = """
name: simple_demo
industry: software
steps:
  - id: overdue
    adapter: jira
    action: find_overdue
    inputs: { project: PROJ }
  - id: notify
    adapter: slack
    action: post_message
    when: "{{ steps.overdue.output.count }} > 0"
    inputs: { channel: "#x", text: "hi" }
"""

BROKEN = """
name: broken_demo
steps:
  - id: first
    adapter: slack
    action: post_message
    inputs: { text: "{{ steps.later.output }}" }
  - id: later
    adapter: slack
    action: post_message
"""


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    root = tmp_path / "templates"
    root.mkdir()
    (root / "simple.yaml").write_text(SIMPLE, encoding="utf-8")
    (root / "broken.yaml").write_text(BROKEN, encoding="utf-8")
    return root


@pytest.fixture
def client(templates: Path) -> TestClient:
    adapters = AdapterRegistry()
    adapters.register(MockTeamsAdapter())
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())

    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    app = create_app(Engine(adapters, llms), templates, TOKEN,
                     tenant="acme_corp", lang="en", store=RunStore())
    return TestClient(app)


def auth(client: TestClient) -> dict[str, str]:
    return {"x-aipmo-token": TOKEN}


# --- 認証 / authentication -------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/session", "/api/templates", "/api/runs", "/api/health",
])
def test_endpoints_require_a_token(client, path):
    assert client.get(path).status_code == 401


def test_wrong_token_is_rejected(client):
    response = client.get("/api/templates", headers={"x-aipmo-token": "wrong"})
    assert response.status_code == 401


def test_running_requires_a_token(client, templates):
    response = client.post("/api/runs",
                           json={"path": str(templates / "simple.yaml")})
    assert response.status_code == 401


def test_index_without_token_shows_the_locked_page(client):
    response = client.get("/")
    assert response.status_code == 401
    assert "token" in response.text.lower()


def test_token_in_url_is_moved_into_a_cookie(client):
    """URL に鍵が残り続けないこと。共有や履歴からの漏洩を防ぐ。"""
    response = client.get(f"/?token={TOKEN}")
    assert response.status_code == 200
    assert response.cookies.get("aipmo_token") == TOKEN


def test_cookie_alone_authenticates(client):
    client.cookies.set("aipmo_token", TOKEN)
    assert client.get("/api/templates").status_code == 200


# --- テンプレート一覧 / template listing -----------------------------------

def test_broken_templates_stay_visible(client):
    """壊れたテンプレートを隠すと、利用者は理由もわからず詰まる。"""
    items = client.get("/api/templates", headers=auth(client)).json()["items"]
    by_name = {i["name"]: i for i in items}

    assert by_name["simple_demo"]["valid"] is True
    # 壊れたテンプレートは宣言名を読めないので、ファイル名で出す。
    # 直すべきファイルを探せるのはこちらなので、これが正しい。
    # A broken template's declared name is unreadable, so it falls back to the
    # filename — which is what the reader needs in order to find and fix it.
    assert by_name["broken"]["valid"] is False
    assert by_name["broken"]["error"]


def test_discover_reports_industry_and_steps(templates):
    items = {i["name"]: i for i in discover_templates(templates)}
    assert items["simple_demo"]["industry"] == "software"
    assert items["simple_demo"]["steps"] == ["overdue", "notify"]


# --- 実行 / running --------------------------------------------------------

def test_run_records_step_outcomes(client, templates):
    response = client.post("/api/runs", headers=auth(client),
                           json={"path": str(templates / "simple.yaml")})
    record = response.json()

    assert record["status"] == "success"
    assert [s["id"] for s in record["steps"]] == ["overdue", "notify"]
    # 条件を満たさない工程は skipped として残る。消してはいけない。
    assert record["steps"][1]["status"] == "skipped"


def test_run_appears_in_history_newest_first(client, templates):
    for _ in range(2):
        client.post("/api/runs", headers=auth(client),
                    json={"path": str(templates / "simple.yaml")})

    items = client.get("/api/runs", headers=auth(client)).json()["items"]
    assert len(items) == 2
    assert items[0]["started_at"] >= items[1]["started_at"]


def test_broken_template_returns_a_readable_error(client, templates):
    response = client.post("/api/runs", headers=auth(client),
                           json={"path": str(templates / "broken.yaml")})
    assert response.status_code == 400
    assert "broken" in response.json()["detail"]


@pytest.mark.parametrize("path", [
    "../../../etc/passwd",
    "/etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "subdir/../../../secrets.yaml",
])
def test_paths_outside_the_template_directory_are_refused(client, path):
    """配布テンプレートを扱う以上、渡されたパスは信用しない。"""
    response = client.post("/api/runs", headers=auth(client), json={"path": path})
    assert response.status_code == 400


def test_templates_in_subdirectories_can_be_run(client, templates):
    """一覧に出すなら実行もできること。片方だけ通るのは筋が通らない。"""
    nested = templates / "examples"
    nested.mkdir()
    (nested / "nested.yaml").write_text(SIMPLE.replace("simple_demo", "nested_demo"),
                                        encoding="utf-8")

    listed = client.get("/api/templates", headers=auth(client)).json()["items"]
    assert any(i["name"] == "nested_demo" for i in listed)

    response = client.post("/api/runs", headers=auth(client),
                           json={"path": "examples/nested.yaml"})
    assert response.status_code == 200
    assert response.json()["template"] == "nested_demo"


def test_symlink_out_of_the_root_is_refused(client, templates, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text(SIMPLE, encoding="utf-8")
    link = templates / "sneaky.yaml"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    response = client.post("/api/runs", headers=auth(client),
                           json={"path": "sneaky.yaml"})
    assert response.status_code == 400


def test_run_detail_by_id(client, templates):
    created = client.post("/api/runs", headers=auth(client),
                          json={"path": str(templates / "simple.yaml")}).json()
    fetched = client.get(f"/api/runs/{created['id']}", headers=auth(client))
    assert fetched.json()["id"] == created["id"]


def test_unknown_run_id_is_404(client):
    assert client.get("/api/runs/nope", headers=auth(client)).status_code == 404


# --- セッション / session --------------------------------------------------

def test_session_carries_tenant_and_strings(client):
    body = client.get("/api/session", headers=auth(client)).json()
    assert body["tenant"] == "acme_corp"
    assert body["strings"]["web_runs"] == "Runs"


def test_session_language_follows_config(templates):
    adapters = AdapterRegistry()
    adapters.register(MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(adapters, llms), templates, TOKEN, lang="ja")

    body = TestClient(app).get("/api/session",
                               headers={"x-aipmo-token": TOKEN}).json()
    assert body["lang"] == "ja"
    assert body["strings"]["web_runs"] == "実行"


def test_history_is_capped():
    store = RunStore(limit=3)
    for i in range(5):
        store.add({"id": str(i)})
    assert [r["id"] for r in store.list()] == ["4", "3", "2"]


def test_failed_run_keeps_step_detail(client, templates):
    """失敗した実行こそ、どの工程で落ちたかが要る。空の結果を返してはいけない。

    A failed run is precisely when the per-step breakdown matters; returning an
    empty step list would strip the reader of the only useful information.
    """
    (templates / "fails.yaml").write_text(
        "name: failing_demo\n"
        "steps:\n"
        "  - id: ok_step\n"
        "    adapter: jira\n"
        "    action: find_overdue\n"
        "    inputs: { project: PROJ }\n"
        "  - id: bad_step\n"
        "    adapter: jira\n"
        "    action: no_such_action\n",
        encoding="utf-8",
    )

    record = client.post("/api/runs", headers=auth(client),
                         json={"path": "fails.yaml"}).json()

    assert record["status"] == "failed"
    steps = {s["id"]: s for s in record["steps"]}
    assert steps["ok_step"]["status"] == "success"
    assert steps["bad_step"]["status"] == "failed"
    assert steps["bad_step"]["error"]
