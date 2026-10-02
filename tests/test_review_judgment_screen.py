"""成果レビューの履歴（GET /api/agents/reviews）と、判断の制御（POST /api/judgment/{pause,resume,reset}）。

履歴は判断ログから、新しい順。制御は operator だけで、`aipmo judgment` と同じ制御の文書に書き、判断ログにも残す。
viewer には書かせない。自律度（off/propose/auto）は画面から変えられない。

Review history from the decision log; judgment pause/resume/reset for operators only, writing the same
control document as the CLI and leaving an audit line.
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
OPERATOR, VIEWER = "rj-operator", "rj-viewer"


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path, viewer_projects=None) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]),
                     viewer_projects=viewer_projects)
    return TestClient(app)


def call(client, method, path, token=OPERATOR, **kw):
    return client.request(method, path, headers={"x-aipmo-token": token}, **kw)


def control_of(base):
    return read_control(cli.open_ledger(cli.load_config(base / "config.yaml"), base).side)


def test_the_review_history_is_newest_first_with_a_tally(base):
    data = call(client_for(base), "GET", "/api/agents/reviews").json()
    assert data["total"] == 2 and len(data["items"]) == 2
    assert data["tally"] == {"開発AI": {"accepted": 1, "rejected": 1}}
    assert [e["at"] for e in data["items"]] == sorted((e["at"] for e in data["items"]), reverse=True)
    for e in data["items"]:
        assert e["decision"] in ("accepted", "rejected") and e["title"] and e["project"] and e["by"]
    rejected = next(e for e in data["items"] if e["decision"] == "rejected")
    assert rejected["note"]                                           # 差し戻しには理由がある


def test_a_new_review_appears_and_the_tally_counts_the_last_decision_only(base):
    client = client_for(base)
    pending = call(client, "GET", "/api/pmo").json()["briefing"]["agent_review"]["pending"][0]
    body = {"ref": pending["task"], "decision": "accept", "dispatch": pending["dispatch"]}
    out = call(client, "POST", "/api/pmo/agents/review", json=body)
    assert out.status_code == 200, out.text
    data = call(client, "GET", "/api/agents/reviews").json()
    assert data["items"][0]["task"] == pending["task"] and data["items"][0]["decision"] == "accepted"
    assert data["tally"]["開発AI"]["accepted"] == 2


def test_a_scoped_viewer_sees_only_reviews_of_their_projects(base):
    full = call(client_for(base), "GET", "/api/agents/reviews").json()["items"]
    assert {e["project"] for e in full} == {"infra", "mobile-app"}
    client = client_for(base, viewer_projects=["web-renewal"])
    scoped = call(client, "GET", "/api/agents/reviews", VIEWER).json()
    assert scoped["total"] == 0 and scoped["tally"] == {}
    only = call(client_for(base, viewer_projects=["infra"]), "GET", "/api/agents/reviews", VIEWER).json()
    assert {e["project"] for e in only["items"]} == {"infra"}
    assert call(client, "GET", "/api/agents/reviews").json()["total"] == len(full)
    assert call(client, "GET", "/api/agents/reviews", token="nope").status_code == 401


def test_pause_resume_and_reset_write_the_control_and_leave_an_audit_line(base):
    client = client_for(base)
    assert not control_of(base).get("paused")
    paused = call(client, "POST", "/api/judgment/pause")
    assert paused.status_code == 200 and paused.json()["control"]["paused"] is True
    assert control_of(base)["paused"] is True
    call(client, "POST", "/api/judgment/resume")
    assert control_of(base)["paused"] is False and control_of(base).get("resumed_at")
    call(client, "POST", "/api/judgment/reset")
    assert control_of(base).get("reset_at")
    log = [json.loads(line) for line in (base / "pmo-decisions.jsonl").read_text(encoding="utf-8").splitlines()
           if "judgment_control" in line]
    assert [e["action"] for e in log] == ["pause", "resume", "reset"] and all(e["by"] == "operator" for e in log)


def test_a_viewer_cannot_touch_the_control_and_unknown_actions_are_refused(base):
    client = client_for(base)
    before = control_of(base)
    assert call(client, "POST", "/api/judgment/pause", VIEWER).status_code in (401, 403)
    assert client.post("/api/judgment/pause").status_code == 401
    assert call(client, "POST", "/api/judgment/autonomy", VIEWER,
                json={"remedy": "notify", "level": "off"}).status_code in (401, 403)   # 閲覧用は変えられない
    assert call(client, "POST", "/api/judgment/nothing").status_code == 404
    assert control_of(base) == before


def test_the_requested_control_can_be_read_back(base):
    client = client_for(base)
    assert call(client, "GET", "/api/judgment/control").json()["control"].get("paused") in (None, False)
    call(client, "POST", "/api/judgment/pause")
    assert call(client, "GET", "/api/judgment/control", VIEWER).json()["control"]["paused"] is True
    call(client, "POST", "/api/judgment/resume")
    assert call(client, "GET", "/api/judgment/control").json()["control"]["paused"] is False
