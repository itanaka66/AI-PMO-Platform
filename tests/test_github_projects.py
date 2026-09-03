"""GitHub Projects アダプタのテスト / GitHub Projects adapter tests.

主眼は、黙って壊れる形を潰すこと。
Focus: the failure shapes that look like success.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.github_projects import GitHubProjectsAdapter


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


def github(routes, **kwargs) -> tuple[GitHubProjectsAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    adapter = GitHubProjectsAdapter(token="t", owner="acme", repo="widgets",
                                     transport=transport, max_retries=1, **kwargs)
    return adapter, transport


def test_missing_configuration_is_named():
    adapter = GitHubProjectsAdapter(transport=FakeTransport({}))
    with pytest.raises(AdapterError, match="token"):
        adapter.invoke("search", {"query": "x"})


# --- 検索 / search -----------------------------------------------------------

def test_search_scopes_to_the_configured_repository():
    adapter, transport = github({"/search/issues": ok({"items": [], "total_count": 0})})
    adapter.invoke("search", {"query": "is:open"})

    _, url, _ = transport.requests[0]
    assert "repo%3Aacme/widgets" in url


def test_search_flattens_the_response():
    adapter, _ = github({"/search/issues": ok({
        "items": [{
            "number": 42, "title": "Fix the widget", "state": "open",
            "assignee": {"login": "octocat"},
            "labels": [{"name": "bug"}, {"name": "aipmo-x"}],
            "html_url": "https://github.com/acme/widgets/issues/42",
        }],
        "total_count": 1,
    })})

    result = adapter.invoke("search", {"query": "is:open"})

    assert result["count"] == 1
    item = result["items"][0]
    assert item["number"] == 42
    assert item["assignee"] == "octocat"
    assert item["labels"] == ["bug", "aipmo-x"]


# --- 作成 / creating ----------------------------------------------------------

def test_created_issue_carries_the_given_fields():
    adapter, transport = github({
        "/repos/acme/widgets/issues": ok({"number": 7}, status=201),
    })

    adapter.invoke("create_issues", {
        "issues": [{"summary": "New widget", "description": "details",
                   "assignee": "octocat"}],
    })

    _, _, payload = transport.requests[0]
    assert payload["title"] == "New widget"
    assert payload["body"] == "details"
    assert payload["assignees"] == ["octocat"]


def test_empty_issue_list_is_not_an_error():
    adapter, transport = github({})
    result = adapter.invoke("create_issues", {"issues": []})
    assert result == {"created": [], "count": 0, "skipped": "no issues supplied"}
    assert transport.requests == []


def test_the_idempotency_key_is_carried_as_a_label():
    adapter, transport = github({
        "/search/issues": ok({"items": [], "total_count": 0}),
        "/repos/acme/widgets/issues": ok({"number": 1}, status=201),
    })

    adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    _, _, payload = transport.requests[1]
    assert "aipmo-meeting-1" in payload["labels"]


def test_reprocessing_the_same_meeting_does_not_duplicate_issues():
    adapter, transport = github({
        "/search/issues": ok({
            "items": [{"number": 9, "title": "x", "state": "open"}],
            "total_count": 1,
        }),
    })

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "x"}], "idempotency_key": "meeting-1",
    })

    assert result == {"created": [9], "count": 1,
                       "skipped": "already created for this idempotency key"}
    # 検索しか呼んでいない。作成 POST は無い。
    assert len(transport.requests) == 1


def test_partial_failure_is_reported_not_hidden():
    calls = {"n": 0}

    def issue_response():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"number": 1}, status=201)
        return 422, {}, json.dumps({"message": "validation failed"}).encode("utf-8")

    adapter, _ = github({"/repos/acme/widgets/issues": issue_response})

    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "a"}, {"summary": "b"}],
    })

    assert result["created"] == [1]
    assert result["count"] == 1
    assert result["failed_count"] == 1


# --- 更新 / updating -----------------------------------------------------------

def test_only_the_given_fields_are_sent():
    adapter, transport = github({
        "/repos/acme/widgets/issues/5": ok({"number": 5}),
    })

    result = adapter.invoke("update_issue", {"issue_number": 5, "title": "New title"})

    _, _, payload = transport.requests[0]
    assert payload == {"title": "New title"}
    assert result["changed"] == ["title"]


def test_nothing_is_sent_when_there_is_nothing_to_change():
    adapter, transport = github({})
    result = adapter.invoke("update_issue", {"issue_number": 5})
    assert result == {"issue_number": 5, "changed": [], "skipped": "nothing to change"}
    assert transport.requests == []


def test_labels_are_added_via_the_dedicated_endpoint_not_replaced():
    adapter, transport = github({
        "/repos/acme/widgets/issues/5/labels": ok({}),
    })

    adapter.invoke("update_issue", {"issue_number": 5, "add_labels": ["urgent"]})

    method, url, payload = transport.requests[0]
    assert method == "POST" and url.endswith("/labels")
    assert payload == {"labels": ["urgent"]}


def test_a_comment_can_accompany_the_change():
    adapter, transport = github({
        "/repos/acme/widgets/issues/5": ok({"number": 5}),
        "/repos/acme/widgets/issues/5/comments": ok({"id": 99}, status=201),
    })

    result = adapter.invoke("update_issue", {"issue_number": 5, "title": "x",
                                              "comment": "done"})

    assert result["commented"] is True
    assert any(url.endswith("/comments") for _, url, _ in transport.requests)


# --- エラー / errors -----------------------------------------------------------

def test_401_mentions_the_token():
    adapter, _ = github({"/search/issues": (401, {}, b'{"message":"Bad credentials"}')})
    with pytest.raises(AdapterError, match="token"):
        adapter.invoke("search", {"query": "x"})


def test_404_names_what_to_check():
    adapter, _ = github({
        "/repos/acme/widgets/issues/999": (404, {}, b'{"message":"Not Found"}'),
    })
    with pytest.raises(AdapterError, match="owner/repo"):
        adapter.invoke("update_issue", {"issue_number": 999, "title": "x"})


# --- 書き込みの印 / write flagging --------------------------------------------

def test_github_projects_write_actions_are_marked():
    adapter, _ = github({})
    assert adapter.writes("create_issues") is True
    assert adapter.writes("update_issue") is True
    assert adapter.writes("add_comment") is True
    assert adapter.writes("search") is False
