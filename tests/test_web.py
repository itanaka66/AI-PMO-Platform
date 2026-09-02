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

def test_secure_cookie_when_https(client):
    response = client.get(f"/?token={TOKEN}", headers={"x-forwarded-proto": "https"})
    assert response.status_code == 200
    cookie = response.headers.get("set-cookie")
    assert cookie is not None
    assert "Secure" in cookie

def test_insecure_cookie_when_http(client):
    response = client.get(f"/?token={TOKEN}")
    assert response.status_code == 200
    cookie = response.headers.get("set-cookie")
    assert cookie is not None
    assert "Secure" not in cookie

def test_index_page_includes_the_proposals_section(client):
    """承認待ち一覧の表示先が index.html から消えていないこと。"""
    client.cookies.set("aipmo_token", TOKEN)
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="proposals"' in response.text
    assert 'id="h-proposals"' in response.text

def test_static_app_js_wires_up_proposal_review():
    """承認・却下の呼び出しコードが app.js から消えていないこと。
    ブラウザテストは無いので、配線自体が残っていることを機械的に確認する。"""
    app_js = (Path(__file__).resolve().parents[1]
              / "aipmo" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "renderProposals" in app_js
    assert "refreshProposals" in app_js
    assert "/api/wbs-proposals" in app_js
    assert '"approve"' in app_js
    assert '"reject"' in app_js

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

def test_rate_limiter_blocks_many_requests(templates):
    adapters = AdapterRegistry()
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    app = create_app(Engine(adapters, llms), templates, TOKEN,
                     tenant="acme_corp", lang="en", store=RunStore())
    client = TestClient(app)

    # 既定の制限は10回。10回までは通る。
    for _ in range(10):
        response = client.post("/api/runs", headers={"x-aipmo-token": TOKEN},
                               json={"path": str(templates / "simple.yaml")})
        assert response.status_code == 200

    # 11回目はブロックされる。
    response = client.post("/api/runs", headers={"x-aipmo-token": TOKEN},
                           json={"path": str(templates / "simple.yaml")})
    assert response.status_code == 429
    assert response.json()["detail"] == "Too Many Requests"

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

# --- 権限分離 / role separation ---------------------------------------------

VIEWER = "viewer-token-value"

@pytest.fixture
def two_role_client(templates: Path) -> TestClient:
    adapters = AdapterRegistry()
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    app = create_app(Engine(adapters, llms), templates, TOKEN,
                     viewer_token=VIEWER, tenant="acme_corp", lang="en",
                     store=RunStore())
    return TestClient(app)

def viewer(client: TestClient) -> dict[str, str]:
    return {"x-aipmo-token": VIEWER}

def test_the_two_tokens_must_differ(templates):
    """同じ値だと、閲覧用を配った相手が実行もできる。
    分離したつもりで分離できていない、が一番危ない。

    Identical values would let everyone given the viewer token run things:
    believing the roles are separated when they are not is the worst outcome.
    """
    adapters = AdapterRegistry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    with pytest.raises(ValueError, match="differ"):
        create_app(Engine(adapters, llms), templates, TOKEN, viewer_token=TOKEN)

def test_a_viewer_can_read_templates(two_role_client):
    response = two_role_client.get("/api/templates", headers=viewer(two_role_client))
    assert response.status_code == 200

def test_a_viewer_can_read_run_history(two_role_client):
    assert two_role_client.get("/api/runs",
                               headers=viewer(two_role_client)).status_code == 200

def test_a_viewer_cannot_start_a_run(two_role_client, templates):
    """画面でボタンを隠すのは権限管理ではない。サーバーが拒否すること。

    Hiding the button is not access control; the endpoint must refuse.
    """
    response = two_role_client.post("/api/runs", headers=viewer(two_role_client),
                                    json={"path": "simple.yaml"})
    assert response.status_code == 403

def test_refusal_is_403_not_401(two_role_client):
    """401 だと『鍵が違う』と思って入れ直そうとする。問題はそこではない。

    A 401 sends the reader off to re-enter a key that was never the problem.
    """
    response = two_role_client.post("/api/runs", headers=viewer(two_role_client),
                                    json={"path": "simple.yaml"})
    assert response.status_code == 403
    assert "run" in response.json()["detail"]

def test_a_viewer_run_attempt_leaves_no_trace(two_role_client, templates):
    """拒否された実行が履歴に残らないこと。"""
    two_role_client.post("/api/runs", headers=viewer(two_role_client),
                         json={"path": "simple.yaml"})
    items = two_role_client.get("/api/runs",
                                headers=viewer(two_role_client)).json()["items"]
    assert items == []

def test_an_operator_can_still_run(two_role_client, templates):
    response = two_role_client.post("/api/runs", headers={"x-aipmo-token": TOKEN},
                                    json={"path": "simple.yaml"})
    assert response.status_code == 200

def test_session_reports_the_role(two_role_client):
    as_viewer = two_role_client.get("/api/session",
                                    headers=viewer(two_role_client)).json()
    assert as_viewer["role"] == "viewer"
    assert as_viewer["can_run"] is False

    as_operator = two_role_client.get(
        "/api/session", headers={"x-aipmo-token": TOKEN}).json()
    assert as_operator["can_run"] is True

def test_an_unknown_token_gets_no_role(two_role_client):
    assert two_role_client.get(
        "/api/session", headers={"x-aipmo-token": "neither"}).status_code == 401

def test_runs_record_who_started_them(two_role_client, templates):
    """PMO では『いつ動いたか』より『誰が動かしたか』が問われる。"""
    record = two_role_client.post("/api/runs", headers={"x-aipmo-token": TOKEN},
                                  json={"path": "simple.yaml"}).json()
    assert record["started_by"] == "operator"

def test_the_viewer_cookie_does_not_grant_running(two_role_client, templates):
    """URL から Cookie に移した後も、権限は上がらないこと。"""
    two_role_client.get(f"/?token={VIEWER}")
    assert two_role_client.cookies.get("aipmo_token") == VIEWER

    response = two_role_client.post("/api/runs", json={"path": "simple.yaml"})
    assert response.status_code == 403

def test_a_single_token_deployment_still_works(templates):
    """閲覧用を設定していない場合、従来どおり動くこと。"""
    adapters = AdapterRegistry()
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    app = create_app(Engine(adapters, llms), templates, TOKEN, lang="en")
    client = TestClient(app)

    assert client.post("/api/runs", headers={"x-aipmo-token": TOKEN},
                       json={"path": "simple.yaml"}).status_code == 200

def test_webhook_triggers_event_templates(client, templates):
    # Prepare a template with event trigger
    content = """
name: Webhook Test
trigger: event:pull_request
steps:
  - id: t
    adapter: slack
    action: post_message
    inputs: { channel: "C123", text: "Hello" }
"""
    (templates / "webhook.yaml").write_text(content.strip(), encoding="utf-8")

    payload = {"event": "pull_request", "action": "opened"}
    response = client.post("/api/webhook", json=payload, headers={"x-aipmo-token": TOKEN})
    assert response.status_code == 200
    assert response.json()["matched"] == 1

    runs_res = client.get("/api/runs", headers={"x-aipmo-token": TOKEN})
    assert runs_res.status_code == 200
    runs = runs_res.json()["items"]
    assert len(runs) > 0
    assert runs[0]["template"] == "Webhook Test"
    assert runs[0]["status"] == "success"

def test_webhook_no_match(client):
    payload = {"event": "unknown_event"}
    response = client.post("/api/webhook", json=payload, headers={"x-aipmo-token": TOKEN})
    assert response.status_code == 200
    assert response.json()["matched"] == 0
