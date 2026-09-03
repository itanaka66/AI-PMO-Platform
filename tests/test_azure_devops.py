"""Azure DevOps アダプタのテスト / Azure DevOps adapter tests.

主眼は、黙って壊れる形を潰すこと。
Focus: the failure shapes that look like success.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.azure_devops import AzureDevOpsAdapter
from aipmo.adapters.base import AdapterError


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
        return 404, {}, b'{"message":"no route"}'


def ok(payload, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def ado(routes, **kwargs) -> tuple[AzureDevOpsAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    adapter = AzureDevOpsAdapter(organization="acme", project="Widgets", pat="t",
                                  transport=transport, max_retries=1, **kwargs)
    return adapter, transport


def test_missing_configuration_is_named():
    adapter = AzureDevOpsAdapter(transport=FakeTransport({}))
    with pytest.raises(AdapterError, match="organization"):
        adapter.invoke("search", {})


# --- 検索 / search -------------------------------------------------------------

def test_search_is_a_two_step_wiql_then_detail_fetch():
    adapter, transport = ado({
        "_apis/wit/wiql": ok({"workItems": [{"id": 1}, {"id": 2}]}),
        "_apis/wit/workitems?ids=1,2": ok({"value": [
            {"id": 1, "fields": {"System.Title": "A", "System.State": "New"}},
            {"id": 2, "fields": {"System.Title": "B", "System.State": "Active"}},
        ]}),
    })

    result = adapter.invoke("search", {})

    assert result["count"] == 2
    assert result["items"][0]["title"] == "A"
    assert transport.requests[0][0] == "POST"
    assert transport.requests[1][0] == "GET"


def test_search_with_no_matching_ids_skips_the_detail_call():
    adapter, transport = ado({"_apis/wit/wiql": ok({"workItems": []})})
    result = adapter.invoke("search", {})
    assert result == {"items": [], "count": 0}
    assert len(transport.requests) == 1


def test_find_overdue_filters_on_the_configured_due_date_field():
    adapter, transport = ado({
        "_apis/wit/wiql": ok({"workItems": []}),
    }, due_date_field="Custom.Deadline")

    adapter.invoke("find_overdue", {"as_of": "2026-01-01"})

    _, _, payload = transport.requests[0]
    assert "Custom.Deadline" in payload["query"]
    assert "2026-01-01" in payload["query"]


# --- 作成 / creating -----------------------------------------------------------

def test_created_work_item_uses_a_json_patch_body():
    adapter, transport = ado({
        r"_apis/wit/workitems/$Task": ok({"id": 10}, status=201),
    })

    adapter.invoke("create_issues", {"issues": [{"summary": "Fix it", "assignee": "a@b.c"}]})

    _, url, payload = transport.requests[0]
    assert "$Task" in url
    assert isinstance(payload, list)
    title_op = next(p for p in payload if p["path"] == "/fields/System.Title")
    assert title_op["value"] == "Fix it"


def test_empty_issue_list_is_not_an_error():
    adapter, transport = ado({})
    result = adapter.invoke("create_issues", {"issues": []})
    assert result == {"created": [], "count": 0, "skipped": "no issues supplied"}
    assert transport.requests == []


def test_the_idempotency_key_is_carried_as_a_tag():
    adapter, transport = ado({
        "_apis/wit/wiql": ok({"workItems": []}),
        r"_apis/wit/workitems/$Task": ok({"id": 1}, status=201),
    })

    adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    _, _, payload = transport.requests[1]
    tags_op = next(p for p in payload if p["path"] == "/fields/System.Tags")
    assert "aipmo-meeting-1" in tags_op["value"]


def test_reprocessing_the_same_meeting_does_not_duplicate_work_items():
    adapter, transport = ado({
        "_apis/wit/wiql": ok({"workItems": [{"id": 5}]}),
        "_apis/wit/workitems?ids=5": ok({"value": [
            {"id": 5, "fields": {"System.Title": "x"}},
        ]}),
    })

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    assert result == {"created": [5], "count": 1,
                       "skipped": "already created for this idempotency key"}
    assert len(transport.requests) == 2  # search only, no create POST


def test_partial_failure_is_reported_not_hidden():
    calls = {"n": 0}

    def item_response():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"id": 1}, status=201)
        return 400, {}, json.dumps({"message": "invalid field"}).encode("utf-8")

    adapter, _ = ado({r"_apis/wit/workitems/$Task": item_response})

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "a"}, {"summary": "b"}],
    })

    assert result["created"] == [1]
    assert result["failed_count"] == 1


# --- 更新 / updating -----------------------------------------------------------

def test_only_the_given_fields_are_sent():
    adapter, transport = ado({"_apis/wit/workitems/5": ok({"id": 5})})

    result = adapter.invoke("update_issue", {"work_item_id": 5, "title": "New title"})

    _, _, payload = transport.requests[0]
    assert payload == [{"op": "add", "path": "/fields/System.Title", "value": "New title"}]
    assert result["changed"] == ["System.Title"]


def test_nothing_is_sent_when_there_is_nothing_to_change():
    adapter, transport = ado({})
    result = adapter.invoke("update_issue", {"work_item_id": 5})
    assert result == {"work_item_id": 5, "changed": [], "skipped": "nothing to change"}
    assert transport.requests == []


def test_a_comment_is_sent_as_history_and_flagged_separately():
    adapter, transport = ado({"_apis/wit/workitems/5": ok({"id": 5})})

    result = adapter.invoke("update_issue", {"work_item_id": 5, "comment": "done"})

    _, _, payload = transport.requests[0]
    history_op = next(p for p in payload if p["path"] == "/fields/System.History")
    assert history_op["value"] == "done"
    assert result["changed"] == []
    assert result["commented"] is True


# --- エラー / errors -----------------------------------------------------------

def test_401_mentions_the_pat():
    adapter, _ = ado({"_apis/wit/wiql": (401, {}, b'{"message":"unauthorized"}')})
    with pytest.raises(AdapterError, match="PAT"):
        adapter.invoke("search", {})


def test_409_explains_the_conflict():
    adapter, _ = ado({"_apis/wit/workitems/5": (409, {}, b'{"message":"stale"}')})
    with pytest.raises(AdapterError, match="競合|conflict"):
        adapter.invoke("update_issue", {"work_item_id": 5, "title": "x"})


# --- 書き込みの印 / write flagging --------------------------------------------

def test_azure_devops_write_actions_are_marked():
    adapter, _ = ado({})
    assert adapter.writes("create_issues") is True
    assert adapter.writes("update_issue") is True
    assert adapter.writes("add_comment") is True
    assert adapter.writes("search") is False
    assert adapter.writes("find_overdue") is False
