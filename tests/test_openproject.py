"""OpenProject アダプタのテスト / OpenProject adapter tests.

主眼は、黙って壊れる形を潰すこと。
Focus: the failure shapes that look like success.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.openproject import OpenProjectAdapter


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


def openproject(routes, **kwargs) -> tuple[OpenProjectAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    adapter = OpenProjectAdapter(base_url="https://op.example.com", api_key="k",
                                 project_id="widgets", transport=transport,
                                 max_retries=1, **kwargs)
    return adapter, transport


def test_missing_configuration_is_named():
    adapter = OpenProjectAdapter(transport=FakeTransport({}))
    with pytest.raises(AdapterError, match="base_url"):
        adapter.invoke("search", {})


# --- 検索 / search -------------------------------------------------------------

def test_search_flattens_the_response():
    adapter, _ = openproject({"work_packages": ok({"_embedded": {"elements": [
        {"id": 1, "subject": "Fix widget",
         "_links": {"status": {"title": "New"}, "assignee": {"title": "A"}},
         "dueDate": "2026-01-01", "lockVersion": 3},
    ]}})})

    result = adapter.invoke("search", {})

    assert result["count"] == 1
    item = result["items"][0]
    assert item["subject"] == "Fix widget"
    assert item["status"] == "New"
    assert item["lock_version"] == 3


def test_default_search_filters_to_open_status():
    adapter, transport = openproject({"work_packages": ok({"_embedded": {"elements": []}})})
    adapter.invoke("search", {})
    _, url, _ = transport.requests[0]
    assert "status" in url


# --- 作成 / creating -----------------------------------------------------------

def test_created_work_package_carries_the_given_fields():
    adapter, transport = openproject({"work_packages": ok({"id": 9}, status=201)},
                                     work_package_type_id=5)

    adapter.invoke("create_issues", {
        "issues": [{"summary": "New widget", "description": "details",
                   "due_date": "2026-02-01"}],
    })

    _, _, payload = transport.requests[0]
    assert payload["subject"] == "New widget"
    assert payload["description"]["raw"] == "details"
    assert payload["dueDate"] == "2026-02-01"
    assert payload["_links"]["type"]["href"] == "/api/v3/types/5"


def test_empty_issue_list_is_not_an_error():
    adapter, transport = openproject({})
    result = adapter.invoke("create_issues", {"issues": []})
    assert result == {"created": [], "count": 0, "skipped": "no issues supplied"}
    assert transport.requests == []


def test_the_idempotency_key_is_embedded_in_the_description():
    routes = {
        "work_packages?filters": ok({"_embedded": {"elements": []}}),
    }
    adapter, transport = openproject(routes)
    transport.routes["work_packages"] = ok({"id": 1}, status=201)

    adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    create_call = transport.requests[-1]
    _, _, payload = create_call
    assert "[aipmo:meeting-1]" in payload["description"]["raw"]


def test_reprocessing_the_same_meeting_does_not_duplicate_work_packages():
    adapter, transport = openproject({
        "work_packages?filters": ok({"_embedded": {"elements": [
            {"id": 7, "subject": "x"},
        ]}}),
    })

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    assert result == {"created": [7], "count": 1,
                       "skipped": "already created for this idempotency key"}
    assert len(transport.requests) == 1  # search only


def test_partial_failure_is_reported_not_hidden():
    calls = {"n": 0}

    def wp_response():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"id": 1}, status=201)
        return 422, {}, json.dumps({"message": "invalid"}).encode("utf-8")

    adapter, _ = openproject({"work_packages": wp_response})

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "a"}, {"summary": "b"}],
    })

    assert result["created"] == [1]
    assert result["failed_count"] == 1


# --- 更新 / updating -----------------------------------------------------------

def test_update_fetches_lock_version_before_patching():
    calls = {"n": 0}

    def wp5_response():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"id": 5, "lockVersion": 4})  # GET
        return ok({"id": 5, "lockVersion": 5})       # PATCH

    adapter, transport = openproject({"work_packages/5": wp5_response})

    result = adapter.invoke("update_issue", {"work_package_id": 5, "subject": "New"})

    get_method, _, _ = transport.requests[0]
    patch_method, _, patch_payload = transport.requests[1]
    assert get_method == "GET"
    assert patch_method == "PATCH"
    assert patch_payload["lockVersion"] == 4
    assert patch_payload["subject"] == "New"
    assert result["changed"] == ["subject"]


def test_nothing_is_sent_when_there_is_nothing_to_change():
    adapter, transport = openproject({})
    result = adapter.invoke("update_issue", {"work_package_id": 5})
    assert result == {"work_package_id": 5, "changed": [],
                      "skipped": "nothing to change"}
    assert transport.requests == []


def test_a_comment_alone_does_not_require_a_lock_version_fetch():
    adapter, transport = openproject({
        "work_packages/5/activities": ok({"id": 1}, status=201),
    })

    result = adapter.invoke("update_issue", {"work_package_id": 5, "comment": "done"})

    assert result["commented"] is True
    assert len(transport.requests) == 1
    assert transport.requests[0][1].endswith("/activities")


# --- エラー / errors -----------------------------------------------------------

def test_401_mentions_the_api_key():
    adapter, _ = openproject({"work_packages": (401, {}, b'{}')})
    with pytest.raises(AdapterError, match="API key"):
        adapter.invoke("search", {})


def test_409_explains_the_stale_lock_version():
    # 1回目の GET は成功、2回目の PATCH で 409 にする。
    calls = {"n": 0}

    def route():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"id": 5, "lockVersion": 1})
        return 409, {}, b'{"message":"conflict"}'

    adapter, _ = openproject({"work_packages/5": route})

    with pytest.raises(AdapterError, match="版|lockVersion"):
        adapter.invoke("update_issue", {"work_package_id": 5, "subject": "x"})


# --- 書き込みの印 / write flagging --------------------------------------------

def test_openproject_write_actions_are_marked():
    adapter, _ = openproject({})
    assert adapter.writes("create_issues") is True
    assert adapter.writes("update_issue") is True
    assert adapter.writes("add_comment") is True
    assert adapter.writes("search") is False
    assert adapter.writes("find_overdue") is False
