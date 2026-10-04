"""接続の診断（aipmo/probe.py・`aipmo integrations`・POST /api/integrations/check）のテスト。

実サービスにつなぐ前に、診断そのものが正しいことを、作り物の HTTP で確かめる:
  (1) 正常なら 疎通 → 1 件の読み取り → 担当候補の一覧 が全部通る（Jira は担当候補の一覧が無い）
  (2) 401 / 404 / タイムアウト / 接続できない を、止まった所と原因の分類（hint）で返す
  (3) 設定の秘密（トークン・API キー）はエラー文に出ない
  (4) 書き込みは一切しない（GET 以外の要求が 1 つも出ない）
  (5) 画面の API は operator だけ・5 秒に 1 回・知らないアダプタは 404

Diagnosis is verified against fake HTTP: ok path, error classification, secret scrubbing, zero writes,
operator-only and throttled endpoint.
"""
from __future__ import annotations

import json

import pytest

from aipmo import cli
from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.jira import JiraAdapter
from aipmo.adapters.openproject import OpenProjectAdapter
from aipmo.adapters.plane import PlaneAdapter
from aipmo.probe import classify, probe_all, scrub

SECRET = "sup3r-secret-token-9999"


class Transport:
    def __init__(self, routes=None, error: Exception | None = None, status: int | None = None) -> None:
        self.routes, self.error, self.status = routes or {}, error, status
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        self.requests.append((method, url))
        if self.error is not None:
            raise self.error
        if self.status is not None:
            return self.status, {}, json.dumps({"message": f"denied for {SECRET}"}).encode("utf-8")
        for fragment, payload in self.routes.items():
            if fragment in url:
                return 200, {}, json.dumps(payload).encode("utf-8")
        return 404, {}, b"{}"


PLANE_OK = {"issues/": {"results": [{"id": "i1", "name": "A", "state": "x"}]},
            "members/": [{"member": {"id": "u1", "display_name": "Sato", "email": "s@x"}}],
            "/projects/proj-1/": {"id": "proj-1"}}
OP_OK = {"work_packages": {"_embedded": {"elements": [{"id": 1, "subject": "A", "_links": {}}]}},
         "memberships": {"_embedded": {"elements": []}}, "users": {"_embedded": {"elements": []}},
         "/projects/widgets": {"id": 1}}
JIRA_OK = {"/myself": {"accountId": "a"}, "/search/jql": {"issues": [{"key": "P-1", "fields": {"summary": "s"}}]}}


def plane(transport):
    return PlaneAdapter(api_key=SECRET, workspace_slug="acme", project_id="proj-1", transport=transport,
                        max_retries=1)


def openproject(transport):
    return OpenProjectAdapter(base_url="https://op.example.com", api_key=SECRET, project_id="widgets",
                              transport=transport, max_retries=1)


def jira(transport):
    return JiraAdapter(site="https://acme.atlassian.net", email="a@b.c", api_token=SECRET, project="P",
                       transport=transport, max_retries=1)


def registry(**adapters) -> AdapterRegistry:
    reg = AdapterRegistry()
    for adapter in adapters.values():
        reg.register(adapter)
    return reg


def steps(report):
    return {s["id"]: s for s in report["steps"]}


def test_a_healthy_connection_passes_every_step_without_writing():
    transports = {"plane": Transport(PLANE_OK), "openproject": Transport(OP_OK), "jira": Transport(JIRA_OK)}
    reg = registry(plane=plane(transports["plane"]), op=openproject(transports["openproject"]),
                   jira=jira(transports["jira"]))
    reports = {r["name"]: r for r in probe_all(reg)}
    assert set(reports) == {"plane", "openproject", "jira"}
    for name, report in reports.items():
        assert report["ok"], (name, report)
        assert [s["id"] for s in report["steps"]][:2] == ["health", "read"]
        assert report["writes_back"] is True
    assert "people" in steps(reports["plane"]) and "people" in steps(reports["openproject"])
    assert "people" not in steps(reports["jira"])                       # Jira は名前を自分で引き当てる
    for name, transport in transports.items():
        assert transport.requests, name
        # 読むだけ。Jira の検索（/search/jql）は仕様上 POST だが読み取り
        writes = [(m, u) for m, u in transport.requests if m != "GET" and "/search/jql" not in u]
        assert writes == [], (name, writes)


@pytest.mark.parametrize("status,hint", [(401, "auth"), (403, "auth"), (404, "not_found"), (429, "rate_limit")])
def test_http_failures_are_classified_and_secrets_are_hidden(status, hint):
    report = probe_all(registry(plane=plane(Transport(status=status))))[0]
    assert report["ok"] is False
    failed = [s for s in report["steps"] if not s["ok"]]
    assert failed and all(s["hint"] == hint for s in failed if s["id"] != "health"), failed
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("error,hint", [
    (TimeoutError("timed out"), "timeout"),
    (ConnectionRefusedError("connection refused"), "network"),
    (OSError("getaddrinfo failed"), "network"),
])
def test_network_failures_are_classified(error, hint):
    report = probe_all(registry(jira=jira(Transport(error=error))))[0]
    assert report["ok"] is False
    read = steps(report)["read"]
    assert read["ok"] is False and read["hint"] == hint, read
    assert steps(report)["health"]["ok"] is False                       # 疎通も偽（原因は read の行に出る）


def test_a_step_failure_does_not_stop_the_later_steps_or_raise():
    reg = registry(plane=plane(Transport({"issues/": {"results": []}})))     # health と people の経路が無い（404）
    report = probe_all(reg)[0]
    assert not report["ok"] and steps(report)["read"]["ok"] is True
    assert {"health", "read", "people"} <= set(steps(report))


def test_only_one_adapter_can_be_named_and_other_adapters_get_the_health_check_only():
    reg = registry(plane=plane(Transport(PLANE_OK)), mock=__import__("aipmo.adapters.mock", fromlist=["x"]).MockSlackAdapter())
    names = {r["name"] for r in probe_all(reg)}
    assert {"plane", "slack"} <= names
    only = probe_all(reg, "plane")
    assert [r["name"] for r in only] == ["plane"]
    slack = next(r for r in probe_all(reg) if r["name"] == "slack")
    assert [s["id"] for s in slack["steps"]] == ["health"]


def test_classify_and_scrub_helpers():
    assert classify("HTTP 401 Unauthorized") == "auth" and classify("404 not found") == "not_found"
    assert classify("The read operation timed out") == "timeout" and classify("weird") == "other"
    adapter = plane(Transport())
    assert SECRET not in scrub(f"failed with {SECRET} and Bearer abcdefghijkl", adapter)
    assert "Bearer ***" in scrub("Bearer abcdefghijklmnop", adapter)
    assert len(scrub("x" * 1000, adapter)) <= 240


def test_the_cli_exit_code_follows_the_result(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: t\nadapters:\n  mock_jira: {}\n", encoding="utf-8")
    bad = registry(plane=plane(Transport(status=401)))
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: type("E", (), {"adapters": bad})())
    assert cli.main(["--config", str(config), "integrations"]) == 1
    out = capsys.readouterr().out
    assert "NG" in out and "[auth]" in out and SECRET not in out
    good = registry(plane=plane(Transport(PLANE_OK)))
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: type("E", (), {"adapters": good})())
    assert cli.main(["--config", str(config), "integrations", "plane"]) == 0
    assert cli.main(["--config", str(config), "integrations", "nope"]) == 1


# ---- 画面の API ----

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "pr-operator", "pr-viewer"


def client(tmp_path, reg):
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    return TestClient(create_app(Engine(reg, llms), tmp_path, OPERATOR, viewer_token=VIEWER, store=RunStore()))


def test_the_check_endpoint_is_operator_only_throttled_and_read_only(tmp_path):
    transport = Transport(PLANE_OK)
    c = client(tmp_path, registry(plane=plane(transport)))
    h = {"x-aipmo-token": OPERATOR}
    assert c.post("/api/integrations/check", json={}, headers={"x-aipmo-token": VIEWER}).status_code in (401, 403)
    assert c.post("/api/integrations/check", json={}).status_code == 401
    assert c.post("/api/integrations/check", json={"name": "nope"}, headers=h).status_code == 404
    ok = c.post("/api/integrations/check", json={"name": "plane"}, headers=h)
    assert ok.status_code == 200 and ok.json()["reports"][0]["ok"] is True
    assert c.post("/api/integrations/check", json={}, headers=h).status_code == 429       # 5 秒に 1 回
    assert {m for m, _ in transport.requests} == {"GET"}
