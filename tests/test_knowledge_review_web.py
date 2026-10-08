"""`/api/knowledge`（ナレッジ公開候補のレビュー）の Web テスト。

アダプタ自体の振る舞いは tests/test_data_adapters.py で確認済み。ここでの
主眼は、経路・権限分離（閲覧は viewer にも、修正・承認・却下は operator だけ）・
テンプレートからは呼べないこと（@action を付けていない）。

The adapter's own behaviour is covered in tests/test_data_adapters.py. The
focus here is routing, the viewer/operator split (viewing is open to viewer;
editing, approving, rejecting require operator), and that no template can
reach these (not @action-decorated).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aipmo.adapters.base import AdapterRegistry  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.embeddings import HashEmbedder  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

from tests.test_data_adapters import FakeQdrantClient, QdrantAdapter  # noqa: E402

TOKEN = "op-token"
VIEWER = "view-token"


@pytest.fixture
def world(tmp_path: Path):
    client = FakeQdrantClient()
    adapter = QdrantAdapter(tenant="acme", embedder=HashEmbedder(), client=client)
    adapters = AdapterRegistry()
    adapters.register(adapter, name="vector_store")
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(adapters, llms), tmp_path, TOKEN, viewer_token=VIEWER,
                     tenant="acme", lang="en", store=RunStore())
    return TestClient(app), adapter, client


def operator() -> dict[str, str]:
    return {"x-aipmo-token": TOKEN}


def viewer() -> dict[str, str]:
    return {"x-aipmo-token": VIEWER}


def submit(adapter, text: str, score: int = 50) -> None:
    adapter.invoke("submit_candidate", {"knowledge": {"text": text}, "publicability_score": score})


def test_list_returns_pending_candidates_sorted_by_score(world):
    client, adapter, _ = world
    submit(adapter, "低い方", score=10)
    submit(adapter, "高い方", score=90)

    response = client.get("/api/knowledge", headers=operator())
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["payload"]["text"] for item in items] == ["高い方", "低い方"]


def test_list_defaults_to_pending_and_status_param_switches(world):
    client, adapter, qdrant_client = world
    submit(adapter, "x")
    candidate_id = qdrant_client.upserts[0][1][0].id
    client.post(f"/api/knowledge/{candidate_id}/approve", headers=operator())

    assert client.get("/api/knowledge", headers=operator()).json()["items"] == []
    approved = client.get("/api/knowledge?status=approved", headers=operator()).json()["items"]
    assert [item["id"] for item in approved] == [candidate_id]


def test_viewer_can_list_and_fetch_but_not_edit_or_decide(world):
    client, adapter, qdrant_client = world
    submit(adapter, "x")
    candidate_id = qdrant_client.upserts[0][1][0].id

    assert client.get("/api/knowledge", headers=viewer()).status_code == 200
    assert client.get(f"/api/knowledge/{candidate_id}", headers=viewer()).status_code == 200

    assert client.post(f"/api/knowledge/{candidate_id}/edit", headers=viewer(),
                       json={"text": "x"}).status_code == 403
    assert client.post(f"/api/knowledge/{candidate_id}/approve", headers=viewer()).status_code == 403
    assert client.post(f"/api/knowledge/{candidate_id}/reject", headers=viewer()).status_code == 403


def test_get_unknown_candidate_is_404(world):
    client, _, _ = world
    assert client.get("/api/knowledge/nope", headers=operator()).status_code == 404


def test_edit_rewrites_pending_content(world):
    client, adapter, qdrant_client = world
    submit(adapter, "下書き")
    candidate_id = qdrant_client.upserts[0][1][0].id

    response = client.post(f"/api/knowledge/{candidate_id}/edit", headers=operator(),
                           json={"text": "書き直した"})
    assert response.status_code == 200
    assert adapter.get_candidate(candidate_id)["payload"]["text"] == "書き直した"


def test_edit_an_unknown_candidate_is_404(world):
    client, _, _ = world
    response = client.post("/api/knowledge/nope/edit", headers=operator(), json={"text": "x"})
    assert response.status_code == 404


def test_approve_promotes_to_public_and_records_who_decided(world):
    client, adapter, qdrant_client = world
    submit(adapter, "主要担当者への依存はスケジュールリスクになる")
    candidate_id = qdrant_client.upserts[0][1][0].id

    response = client.post(f"/api/knowledge/{candidate_id}/approve", headers=operator(),
                           json={"note": "良い一般化"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"id": candidate_id, "status": "approved", "promoted": True}

    record = adapter.get_candidate(candidate_id)
    assert record["payload"]["reviewed_by"] == "operator"
    assert record["payload"]["review_note"] == "良い一般化"
    public_collection, public_points = next(
        (c, p) for c, p in qdrant_client.upserts if c == "public_pmo_knowledge")
    assert public_points[0].id == candidate_id


def test_reject_never_writes_public(world):
    client, adapter, qdrant_client = world
    submit(adapter, "x")
    candidate_id = qdrant_client.upserts[0][1][0].id

    response = client.post(f"/api/knowledge/{candidate_id}/reject", headers=operator())
    assert response.status_code == 200
    assert response.json()["promoted"] is False
    assert all(c != "public_pmo_knowledge" for c, _ in qdrant_client.upserts)


def test_deciding_an_already_decided_candidate_is_404(world):
    client, adapter, qdrant_client = world
    submit(adapter, "x")
    candidate_id = qdrant_client.upserts[0][1][0].id
    client.post(f"/api/knowledge/{candidate_id}/approve", headers=operator())

    response = client.post(f"/api/knowledge/{candidate_id}/reject", headers=operator())
    assert response.status_code == 404


def test_no_configured_backend_is_503():
    adapters = AdapterRegistry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(adapters, llms), Path("."), TOKEN, lang="en", store=RunStore())
    client = TestClient(app)

    assert client.get("/api/knowledge", headers=operator()).status_code == 503


def test_endpoints_require_a_token(world):
    client, _, _ = world
    assert client.get("/api/knowledge").status_code == 401
    assert client.post("/api/knowledge/x/approve").status_code == 401
