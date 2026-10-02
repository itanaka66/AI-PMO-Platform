"""自律度の変更（POST /api/judgment/autonomy）、WBS の編集（POST /api/wbs/edit）、変化の目印（GET /api/pulse）。

自律度: 設定の値が基準。下げる・止めるは operator なら可。基準より上げるのは `ui_can_raise` が無ければ拒否し、
常駐も（画面が書いても）上げた上書きは無視する。変更は判断ログに残る。基準に戻すと上書きは消える。
WBS の編集: 固定の形だけ。apply が無ければ差分だけで何も書かない。壊れる変更・viewer・競合は書かない。

Autonomy overrides, WBS editing and the change marker.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aipmo import cli, demo
from aipmo.judgment import effective_autonomy, write_control
from aipmo.wbs_edit import WbsEditError, plan_changes, write_plan

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "ae-operator", "ae-viewer"


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    demo.load(cli.load_config(base / "config.yaml"), base)
    return base


def client_for(base: Path, *, raise_ok: bool = False) -> TestClient:
    config = cli.load_config(base / "config.yaml")
    if raise_ok:
        config["pmo_core"]["judgment"] = {"ui_can_raise": True}
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]),
                     wbs_view=cli._wbs_view(config, base), judgment=cli._judgment_config(config))
    return TestClient(app)


def call(client, method, path, token=OPERATOR, **kw):
    return client.request(method, path, headers={"x-aipmo-token": token}, **kw)


def side(base):
    return cli.open_ledger(cli.load_config(base / "config.yaml"), base).side


# ---- 自律度 ----

def test_lowering_is_allowed_raising_is_refused_and_returning_to_the_config_clears_it(base):
    client = client_for(base)
    ok = call(client, "POST", "/api/judgment/autonomy", json={"remedy": "recollect", "level": "propose"})
    assert ok.status_code == 200
    state = call(client, "GET", "/api/judgment/control", VIEWER).json()["autonomy"]
    assert state["override"] == {"recollect": "propose"} and state["config"]["recollect"] == "auto"
    assert call(client, "POST", "/api/judgment/autonomy",
                json={"remedy": "followup", "level": "auto"}).status_code == 403       # 基準(propose)より上
    assert call(client, "POST", "/api/judgment/autonomy",
                json={"remedy": "nope", "level": "off"}).status_code == 422
    assert call(client, "POST", "/api/judgment/autonomy",
                json={"remedy": "notify", "level": "maybe"}).status_code == 422
    call(client, "POST", "/api/judgment/autonomy", json={"remedy": "recollect", "level": "auto"})
    assert call(client, "GET", "/api/judgment/control").json()["autonomy"]["override"] == {}


def test_raising_works_only_when_the_config_allows_it_and_changes_are_audited(base):
    client = client_for(base, raise_ok=True)
    assert call(client, "POST", "/api/judgment/autonomy",
                json={"remedy": "followup", "level": "auto"}).status_code == 200
    call(client, "POST", "/api/judgment/autonomy", json={"remedy": "notify", "level": "off"})
    log = [json.loads(x) for x in side(base).tail("pmo-decisions.jsonl", 50) if "judgment_autonomy" in x]
    assert [(e["remedy"], e["from"], e["to"], e["by"]) for e in log] == [
        ("followup", "propose", "auto", "operator"), ("notify", "auto", "off", "operator")]


def test_the_resident_ignores_a_raise_the_config_does_not_allow():
    base = {"notify": "auto", "followup": "propose", "launch": "off"}
    written = {"autonomy_override": {"followup": "auto", "notify": "off", "launch": "propose",
                                     "bogus": "auto", "recollect": "wild"}}
    assert effective_autonomy(base, written, can_raise=False) == {
        "notify": "off", "followup": "propose", "launch": "off"}
    assert effective_autonomy(base, written, can_raise=True) == {
        "notify": "off", "followup": "auto", "launch": "propose"}


def test_a_running_cycle_applies_the_override(base):
    config = cli.load_config(base / "config.yaml")
    task_engine = cli.open_ledger(config, base)
    write_control(task_engine.side, autonomy_override={"notify": "off", "followup": "auto"})
    core = cli.build_pmo_core(config, task_engine, base=base)
    summary = core.cycle()["judgment"]
    assert summary["autonomy"]["notify"] == "off" and summary["autonomy"]["followup"] == "propose"
    assert summary["autonomy_config"]["notify"] == "auto" and summary["ui_can_raise"] is False


# ---- WBS の編集 ----

CHANGE = [{"op": "set", "node": "2.2", "field": "due", "value": "2026-10-20"}]


def _flat(nodes):
    for n in nodes:
        yield n
        yield from _flat(n.get("children", []))


def test_preview_writes_nothing_and_apply_writes_a_minimal_diff(base):
    client, wbs = client_for(base), base / "wbs-demo.yaml"
    before = wbs.read_bytes()
    preview = call(client, "POST", "/api/wbs/edit", json={"changes": CHANGE}).json()
    assert preview["applied"] is False and preview["changed"] is True and "2026-10-20" in preview["diff"]
    assert wbs.read_bytes() == before
    done = call(client, "POST", "/api/wbs/edit", json={"changes": CHANGE, "apply": True}).json()
    assert done["applied"] is True
    after = wbs.read_text(encoding="utf-8")
    assert "due: 2026-10-20" in after and "# デモ用の WBS" in after            # コメントは残る
    shown = call(client, "GET", "/api/wbs").json()
    assert {n["id"]: n for n in _flat(shown["tree"])}["2.2"]["due"] == "2026-10-20"
    assert any("wbs_edited" in x for x in side(base).tail("pmo-decisions.jsonl", 20))


def test_bad_changes_viewers_and_conflicts_write_nothing(base):
    client, wbs = client_for(base), base / "wbs-demo.yaml"
    before = wbs.read_bytes()
    for bad in ([{"op": "set", "node": "9.9", "field": "due", "value": "2026-10-20"}],
                [{"op": "set", "node": "2.2", "field": "status", "value": "done"}],   # 証拠なしの完了
                [{"op": "delete", "node": "2.2"}], [], "x"):
        r = call(client, "POST", "/api/wbs/edit", json={"changes": bad, "apply": True})
        assert r.status_code == 422, bad
    assert call(client, "POST", "/api/wbs/edit", VIEWER,
                json={"changes": CHANGE, "apply": True}).status_code in (401, 403)
    assert wbs.read_bytes() == before
    stale = plan_changes(wbs, base, CHANGE)                                   # 読んだ後に別の人が書き換えた
    wbs.write_text(wbs.read_text(encoding="utf-8") + "\n# 別の人の編集\n", encoding="utf-8")
    with pytest.raises(WbsEditError):
        write_plan(stale)


def test_no_wbs_means_no_edit(base):
    config = cli.load_config(base / "config.yaml")
    built = cli.build_engine(config, base_dir=base)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, store=RunStore(),
                     pmo_ledger=base / "task-ledger.db")
    assert call(TestClient(app), "POST", "/api/wbs/edit", json={"changes": CHANGE}).status_code == 404


# ---- 変化の目印 ----

def test_the_pulse_changes_when_something_is_decided(base):
    client = client_for(base)
    first = call(client, "GET", "/api/pulse", VIEWER).json()
    assert first["briefing_at"] and call(client, "GET", "/api/pulse", VIEWER).json() == first
    call(client, "POST", "/api/judgment/pause")
    assert call(client, "GET", "/api/pulse").json()["log"] != first["log"]
    assert client.get("/api/pulse").status_code == 401
