"""Plane アダプタのテスト / Plane adapter tests.

主眼は、黙って壊れる形を潰すこと。
Focus: the failure shapes that look like success.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.plane import PlaneAdapter


class FakeTransport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        payload = json.loads(body.decode("utf-8")) if body else {}
        self.requests.append((method, url, payload))
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            response = self.routes[max(matches)[1]]
            return response() if callable(response) else response
        return 404, {}, b'{}'


def ok(payload, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def plane(routes, **kwargs) -> tuple[PlaneAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    adapter = PlaneAdapter(api_key="k", workspace_slug="acme", project_id="proj-1",
                           transport=transport, max_retries=1, **kwargs)
    return adapter, transport


def test_missing_configuration_is_named():
    adapter = PlaneAdapter(transport=FakeTransport({}))
    with pytest.raises(AdapterError, match="api_key"):
        adapter.invoke("search", {})


# --- 検索 / search -------------------------------------------------------------

def test_search_flattens_the_response():
    adapter, _ = plane({"issues/": ok({"results": [
        {"id": "i1", "name": "Fix widget", "state": "backlog",
         "target_date": "2026-01-01", "priority": "high"},
    ]})})

    result = adapter.invoke("search", {})

    assert result["count"] == 1
    assert result["items"][0]["name"] == "Fix widget"
    assert result["items"][0]["priority"] == "high"


def test_find_overdue_filters_client_side_by_target_date():
    adapter, _ = plane({"issues/": ok({"results": [
        {"id": "i1", "name": "Late one", "target_date": "2020-01-01"},
        {"id": "i2", "name": "Done already", "target_date": "2020-01-01",
         "completed_at": "2020-01-02"},
        {"id": "i3", "name": "Future", "target_date": "2999-01-01"},
        {"id": "i4", "name": "No date"},
    ]})})

    result = adapter.invoke("find_overdue", {"as_of": "2026-01-01"})

    assert result["count"] == 1
    assert result["items"][0]["name"] == "Late one"


# --- 作成 / creating -----------------------------------------------------------

def test_created_issue_carries_the_given_fields():
    adapter, transport = plane({"issues/": ok({"id": "i9"}, status=201)})

    adapter.invoke("create_issues", {
        "issues": [{"summary": "New widget", "description": "details",
                   "due_date": "2026-02-01"}],
    })

    _, _, payload = transport.requests[0]
    assert payload["name"] == "New widget"
    assert "details" in payload["description_html"]
    assert payload["target_date"] == "2026-02-01"


def test_empty_issue_list_is_not_an_error():
    adapter, transport = plane({})
    result = adapter.invoke("create_issues", {"issues": []})
    assert result == {"created": [], "count": 0, "skipped": "no issues supplied"}
    assert transport.requests == []


def test_the_idempotency_key_becomes_an_external_id():
    adapter, transport = plane({"issues/": ok({"id": "i1"}, status=201)})

    adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    _, _, payload = transport.requests[0]
    assert payload["external_id"] == "meeting-1:0"
    assert payload["external_source"] == "aipmo"


def test_no_external_id_is_sent_without_an_idempotency_key():
    adapter, transport = plane({"issues/": ok({"id": "i1"}, status=201)})

    adapter.invoke("create_issues", {"issues": [{"summary": "x"}]})

    _, _, payload = transport.requests[0]
    assert "external_id" not in payload


def test_partial_failure_is_reported_not_hidden():
    calls = {"n": 0}

    def issue_response():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"id": "i1"}, status=201)
        return 400, {}, json.dumps({"name": ["required"]}).encode("utf-8")

    adapter, _ = plane({"issues/": issue_response})

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "a"}, {"summary": "b"}],
    })

    assert result["created"] == ["i1"]
    assert result["failed_count"] == 1


# --- 更新 / updating -----------------------------------------------------------

def test_only_the_given_fields_are_sent():
    adapter, transport = plane({"issues/i1/": ok({"id": "i1"})})

    result = adapter.invoke("update_issue", {"issue_id": "i1", "name": "New name"})

    _, _, payload = transport.requests[0]
    assert payload == {"name": "New name"}
    assert result["changed"] == ["name"]


def test_nothing_is_sent_when_there_is_nothing_to_change():
    adapter, transport = plane({})
    result = adapter.invoke("update_issue", {"issue_id": "i1"})
    assert result == {"issue_id": "i1", "changed": [], "skipped": "nothing to change"}
    assert transport.requests == []


def test_a_comment_can_accompany_the_change():
    adapter, transport = plane({
        "issues/i1/": ok({"id": "i1"}),
        "issues/i1/comments/": ok({"id": "c1"}, status=201),
    })

    result = adapter.invoke("update_issue", {"issue_id": "i1", "name": "x",
                                             "comment": "done"})

    assert result["commented"] is True
    assert any(url.endswith("/comments/") for _, url, _ in transport.requests)


# --- エラー / errors -----------------------------------------------------------

def test_401_mentions_the_api_key():
    adapter, _ = plane({"issues/": (401, {}, b'{}')})
    with pytest.raises(AdapterError, match="API key"):
        adapter.invoke("search", {})


def test_404_names_what_to_check():
    adapter, _ = plane({"issues/i404/": (404, {}, b'{}')})
    with pytest.raises(AdapterError, match="workspace_slug"):
        adapter.invoke("update_issue", {"issue_id": "i404", "name": "x"})


# --- 書き込みの印 / write flagging --------------------------------------------

def test_plane_write_actions_are_marked():
    adapter, _ = plane({})
    assert adapter.writes("create_issues") is True
    assert adapter.writes("update_issue") is True
    assert adapter.writes("add_comment") is True
    assert adapter.writes("search") is False
    assert adapter.writes("find_overdue") is False
