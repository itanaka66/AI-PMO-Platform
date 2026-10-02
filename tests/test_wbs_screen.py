"""WBS 画面の API（GET /api/wbs）のテスト。

デモの WBS（demo/wbs-demo.yaml）を読んで、木・進捗・予測・ずれ・証拠の確認結果が返ること、
読むだけであること（WBS ファイルも台帳も変えない）、設定が無ければ 404、
プロジェクトを限定された viewer には 403 を確かめる。

The WBS screen API: tree, progress, forecast, drift and per-evidence results from the demo WBS;
read-only; 404 when no WBS is configured; 403 for a viewer confined to some projects.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aipmo import cli, demo
from aipmo.wbs import load_wbs, view

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "wbs-operator", "wbs-viewer"


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path, *, wbs: bool = True, viewer_projects=None) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]),
                     wbs_view=cli._wbs_view(config, base) if wbs else None,
                     viewer_projects=viewer_projects)
    return TestClient(app)


def get(client, token=OPERATOR):
    return client.get("/api/wbs", headers={"x-aipmo-token": token})


def flat(nodes):
    for n in nodes:
        yield n
        yield from flat(n.get("children", []))


def test_the_tree_progress_and_drift_come_back(base):
    data = get(client_for(base)).json()
    nodes = {n["id"]: n for n in flat(data["tree"])}
    assert data["wbs"]["id"] == "demo-product" and data["wbs"]["deadline"] == "2026-10-31"
    assert set(nodes) >= {"1", "1.1", "1.2", "2", "2.1", "2.2", "2.3"}
    assert nodes["1"]["leaf"] is False and nodes["1"]["percent"] == 50
    assert nodes["1.1"]["leaf"] is True and nodes["1.1"]["evidence"][0]["ok"] is True
    # わざと仕込んだ 2 つのずれが、ノードの注意（flags）と証拠の確認結果に出る
    assert "maybe_done" in nodes["1.2"]["flags"] and nodes["1.2"]["evidence"][0]["ok"] is True
    assert "evidence_missing" in nodes["2.1"]["flags"] and nodes["2.1"]["evidence"][0]["ok"] is False
    assert nodes["2.1"]["evidence"][0]["why"]
    assert data["summary"]["leaves"] == 5 and data["error_count"] >= 1
    for key in ("tasks", "items", "summary_text"):
        assert key not in data


def test_reading_changes_nothing(base):
    wbs_file = base / "wbs-demo.yaml"
    before = wbs_file.read_bytes()
    client = client_for(base)
    assert get(client).status_code == 200 and get(client).status_code == 200
    assert wbs_file.read_bytes() == before


def test_viewers_can_read_but_a_confined_viewer_cannot(base):
    assert get(client_for(base), VIEWER).status_code == 200
    assert get(client_for(base, viewer_projects=["infra"]), VIEWER).status_code == 403
    assert get(client_for(base, viewer_projects=["infra"]), OPERATOR).status_code == 200


def test_without_a_wbs_the_screen_is_not_there(base):
    assert get(client_for(base, wbs=False)).status_code == 404


def test_a_broken_wbs_file_is_reported_not_a_crash(base):
    (base / "wbs-demo.yaml").write_text("wbs: [", encoding="utf-8")
    response = get(client_for(base))
    assert response.status_code == 500 and "WBS" in response.json()["detail"]


def test_view_marks_the_critical_path_on_leaves():
    wbs, problems = load_wbs(ROOT / "demo" / "wbs-demo.yaml")
    result = view(wbs, ROOT / "demo", problems=problems)
    marked = {n["id"] for n in flat(result["tree"]) if n.get("critical")}
    assert marked == set(result["critical_path"])
