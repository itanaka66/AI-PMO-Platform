"""WBS 再計画案の受信箱・プレビュー・承認を、実際の PostgreSQL で確かめる。

`AIPMO_TEST_PG_DSN` があるときだけ動く（なければ飛ばす）。偽物の PostgreSQL では確かめられない部分：
出荷する `queries.yaml` の SQL と `sql/schema.sql` の表が本当に噛み合うこと、`diff` が JSONB で返ること、
承認が一度だけ通ること（二度目は 409）、他のテナントの行が見えないこと。

Runs only with AIPMO_TEST_PG_DSN: the shipped queries.yaml against the real schema.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="AIPMO_TEST_PG_DSN が未設定")

psycopg = pytest.importorskip("psycopg")
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo import cli, demo  # noqa: E402
from aipmo.adapters.postgres import PostgresAdapter  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.wbs_proposals import Target  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OPERATOR, VIEWER = "pg-operator", "pg-viewer"
QUERIES = yaml.safe_load((ROOT / "queries.yaml").read_text(encoding="utf-8"))
GOOD = [{"op": "set", "node": "2.2", "field": "due", "value": "2026-10-20"}]


@pytest.fixture(scope="module", autouse=True)
def schema():
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute((ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
    yield


@pytest.fixture
def world(tmp_path):
    base = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", base, ignore=shutil.ignore_patterns("task-ledger.db*", "pmo-*"))
    config = cli.load_config(base / "config.yaml")
    demo.load(config, base)
    tenant = config["tenant"]
    other = f"other-{uuid.uuid4().hex[:8]}"
    ids = {"ok": f"p-{uuid.uuid4().hex[:8]}", "bad": f"p-{uuid.uuid4().hex[:8]}",
           "free": f"p-{uuid.uuid4().hex[:8]}", "foreign": f"p-{uuid.uuid4().hex[:8]}"}
    rows = [(ids["ok"], tenant, {"changes": GOOD}, 2, None),
            (ids["bad"], tenant, {"changes": [{"op": "set", "node": "9.9", "field": "effort", "value": 1}]}, 3, "B"),
            (ids["free"], tenant, {"move": "自由な形"}, 1, None),
            (ids["foreign"], other, {"changes": GOOD}, 2, None)]
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM wbs_replan_proposals WHERE tenant = %s", (tenant,))
        for pid, ten, diff, tier, label in rows:
            conn.execute(
                "INSERT INTO wbs_replan_proposals (id, tenant, wbs_version_from, diff, rationale, tier, "
                "confidence, option_label, source_key) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)",
                (pid, ten, "demo-product", json.dumps(diff), "遅れを取り戻す", tier, 0.7, label, pid))
    built = cli.build_engine(config, base_dir=base)
    built.adapters.register(PostgresAdapter(dsn=PG_DSN, queries=QUERIES, tenant=tenant))
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    wbs_file = base / "wbs-demo.yaml"
    target = Target(file=wbs_file, root=base, decisions=base / "pmo-decisions.jsonl")
    app = create_app(Engine(built.adapters, llms), base / "templates", OPERATOR, viewer_token=VIEWER,
                     tenant=tenant, lang="ja", store=RunStore(), pmo_ledger=base / "task-ledger.db",
                     members=load_members(config["pmo_core"]["members"]), filing=cli._web_filing(config),
                     wbs_target=target, wbs_view=(wbs_file, base))
    yield TestClient(app), ids, wbs_file
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM wbs_replan_proposals WHERE id = ANY(%s)", (list(ids.values()),))


def call(client, method, path, token=OPERATOR, **kw):
    return client.request(method, path, headers={"x-aipmo-token": token}, **kw)


def test_the_real_queries_list_only_this_tenants_pending_proposals_in_the_inbox(world):
    client, ids, _ = world
    listed = call(client, "GET", "/api/wbs-proposals").json()["items"]
    assert {r["id"] for r in listed} == {ids["ok"], ids["bad"], ids["free"]}      # 別のテナントの行は見えない
    assert isinstance(listed[0]["diff"], (dict, list))                          # JSONB は構造のまま返る
    inbox = call(client, "GET", "/api/inbox").json()
    replans = {i["ref"]: i for i in inbox["items"] if i["kind"] == "replan"}
    assert set(replans) == {ids["ok"], ids["bad"], ids["free"]}
    assert replans[ids["bad"]]["urgency"] == 95 and replans[ids["ok"]]["urgency"] == 75
    assert [a["id"] for a in replans[ids["ok"]]["actions"]] == ["approve", "reject"]
    assert inbox["by_kind"]["replan"] == 3 and inbox["total"] == 14 + 3


def test_the_preview_reads_the_real_row_and_writes_nothing(world):
    client, ids, wbs = world
    before = wbs.read_bytes()
    ok = call(client, "GET", f"/api/wbs-proposals/{ids['ok']}/preview", VIEWER).json()
    assert ok["applicable"] is True and "2026-10-20" in ok["diff"] and ok["changed"] is True
    bad = call(client, "GET", f"/api/wbs-proposals/{ids['bad']}/preview").json()
    assert bad["applicable"] is False and bad["reason"] == "invalid" and "9.9" in " ".join(bad["problems"])
    assert call(client, "GET", f"/api/wbs-proposals/{ids['free']}/preview").json()["reason"] == "free_form"
    assert call(client, "GET", f"/api/wbs-proposals/{ids['foreign']}/preview").status_code == 404
    assert wbs.read_bytes() == before


def test_approving_applies_once_and_leaves_the_inbox(world):
    client, ids, wbs = world
    done = call(client, "POST", f"/api/wbs-proposals/{ids['ok']}/approve", json={"note": "ok"})
    assert done.status_code == 200 and done.json()["applied"] is True
    assert "due: 2026-10-20" in wbs.read_text(encoding="utf-8")
    assert call(client, "POST", f"/api/wbs-proposals/{ids['ok']}/approve", json={}).status_code == 409
    refs = {i["ref"] for i in call(client, "GET", "/api/inbox").json()["items"] if i["kind"] == "replan"}
    assert ids["ok"] not in refs and {ids["bad"], ids["free"]} <= refs
    with psycopg.connect(PG_DSN) as conn:
        row = conn.execute("SELECT status, decided_by, decision_note FROM wbs_replan_proposals WHERE id = %s",
                           (ids["ok"],)).fetchone()
    assert row == ("approved", "operator", "ok")


def test_an_unappliable_proposal_stays_pending_and_other_tenants_cannot_be_decided(world):
    client, ids, wbs = world
    before = wbs.read_bytes()
    r = call(client, "POST", f"/api/wbs-proposals/{ids['bad']}/approve", json={})
    assert r.status_code == 422 and wbs.read_bytes() == before
    with psycopg.connect(PG_DSN) as conn:
        assert conn.execute("SELECT status FROM wbs_replan_proposals WHERE id = %s",
                            (ids["bad"],)).fetchone() == ("pending",)
    assert call(client, "POST", f"/api/wbs-proposals/{ids['foreign']}/reject", json={}).status_code in (404, 409)
    with psycopg.connect(PG_DSN) as conn:
        assert conn.execute("SELECT status FROM wbs_replan_proposals WHERE id = %s",
                            (ids["foreign"],)).fetchone() == ("pending",)
    assert call(client, "POST", f"/api/wbs-proposals/{ids['free']}/reject", json={"note": "不要"}).status_code == 200
