"""自己学習サイクルの「RAG を信用する」設定の Web テスト
（GET /api/learning/control・POST /api/learning/trust-rag）。

`aipmo judgment pause/resume/reset` の Web 版（tests/test_review_judgment_screen.py）
と同じ形：制御は operator だけ、書く先は CLI と同じ制御の文書、判断ログにも残す。
viewer は読めるが書けない。

Mirrors the judgment pause/resume/reset web tests: operator-only writes to
the same control document the CLI uses, audited in the decision log; viewer
can read but not write.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aipmo import cli, demo
from aipmo.judgment import read_control

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "lw-operator", "lw-viewer"


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]))
    return TestClient(app)


def call(client, method, path, token=OPERATOR, **kw):
    return client.request(method, path, headers={"x-aipmo-token": token}, **kw)


def control_of(base):
    return read_control(cli.open_ledger(cli.load_config(base / "config.yaml"), base).side)


def test_disabled_by_default(base):
    client = client_for(base)
    assert call(client, "GET", "/api/learning/control").json() == {"trust_rag": False}


def test_enabling_writes_the_control_and_leaves_an_audit_line(base):
    client = client_for(base)
    response = call(client, "POST", "/api/learning/trust-rag", json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["trust_rag"] is True
    assert control_of(base)["learning_trust_rag"] is True
    assert control_of(base).get("learning_trust_rag_enabled_at")

    log = [json.loads(line) for line in (base / "pmo-decisions.jsonl").read_text(encoding="utf-8").splitlines()
           if "learning_trust_rag" in line]
    assert log[-1] == {**log[-1], "kind": "learning_trust_rag", "enabled": True, "by": "operator"}


def test_disabling_after_enabling_turns_it_back_off(base):
    client = client_for(base)
    call(client, "POST", "/api/learning/trust-rag", json={"enabled": True})
    response = call(client, "POST", "/api/learning/trust-rag", json={"enabled": False})
    assert response.json()["trust_rag"] is False
    assert control_of(base)["learning_trust_rag"] is False
    assert control_of(base).get("learning_trust_rag_disabled_at")


def test_a_viewer_can_read_but_not_write(base):
    client = client_for(base)
    assert call(client, "GET", "/api/learning/control", VIEWER).status_code == 200
    response = call(client, "POST", "/api/learning/trust-rag", VIEWER, json={"enabled": True})
    assert response.status_code in (401, 403)
    assert client.post("/api/learning/trust-rag", json={"enabled": True}).status_code == 401
    assert control_of(base).get("learning_trust_rag") is not True


def test_without_a_ledger_both_routes_are_404():
    adapters_only = cli.build_engine({"tenant": "acme"})
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(adapters_only.adapters, llms), Path("."), OPERATOR, lang="en", store=RunStore())
    client = TestClient(app)
    assert client.get("/api/learning/control", headers={"x-aipmo-token": OPERATOR}).status_code == 404
    assert client.post("/api/learning/trust-rag", headers={"x-aipmo-token": OPERATOR},
                       json={"enabled": True}).status_code == 404
