"""Jira / Slack 実アダプタのテスト / real Jira and Slack adapter tests.

主眼は、黙って壊れる形を潰すこと。
Focus: the failure shapes that look like success.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.jira import JiraAdapter, to_adf
from aipmo.adapters.slack import SlackAdapter


class FakeTransport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        payload = json.loads(body.decode("utf-8")) if body else {}
        self.requests.append((method, url, payload))
        # URL 内で最も後ろに現れる断片を、より具体的なものとして選ぶ。
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            response = self.routes[max(matches)[1]]
            return response() if callable(response) else response
        return 404, {}, b'{"errorMessages":["no route"]}'


def ok(payload: dict, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


# ===== Jira =================================================================

def jira(routes, **kwargs) -> tuple[JiraAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    adapter = JiraAdapter(site="https://acme.atlassian.net", email="a@b.c",
                          api_token="t", project="PROJ", transport=transport,
                          max_retries=1, **kwargs)
    return adapter, transport


def test_missing_configuration_is_named():
    adapter = JiraAdapter(transport=FakeTransport({}), project="PROJ")
    with pytest.raises(AdapterError, match="site"):
        adapter.invoke("search", {"jql": "x"})


# --- ADF --------------------------------------------------------------------

def test_description_is_converted_to_adf():
    """v3 の description は素の文字列を受け付けない。400 になる。"""
    document = to_adf("一行目\n\n二段落目")
    assert document["type"] == "doc" and document["version"] == 1
    assert len(document["content"]) == 2
    assert document["content"][0]["content"][0]["text"] == "一行目"


def test_empty_description_still_produces_a_valid_document():
    assert to_adf("")["content"] == [{"type": "paragraph", "content": []}]


def test_created_issue_carries_an_adf_description():
    adapter, transport = jira({
        "/search/jql": ok({"issues": []}),
        "/user/search": ok([{"accountId": "acc-1"}]),
        "/issue/bulk": ok({"issues": [{"key": "PROJ-1"}]}),
    })
    adapter.invoke("create_issues", {
        "issues": [{"summary": "s", "description": "本文"}]})

    body = [r for r in transport.requests if "bulk" in r[1]][0][2]
    description = body["issueUpdates"][0]["fields"]["description"]
    assert isinstance(description, dict) and description["type"] == "doc"


# --- 検索 / search -----------------------------------------------------------

def test_search_uses_the_current_endpoint():
    """旧 /rest/api/3/search は削除済み。410 が返る。"""
    adapter, transport = jira({"/search/jql": ok({"issues": []})})
    adapter.invoke("search", {"jql": "status = Open"})

    url = transport.requests[0][1]
    assert url.endswith("/rest/api/3/search/jql")


def test_search_enforces_project_filter():
    """任意のプロジェクトが引けないよう、プロジェクト指定が強制される。"""
    adapter, transport = jira({"/search/jql": ok({"issues": []})})
    adapter.invoke("search", {"jql": "status = Open"})

    body = transport.requests[0][2]
    # condition is parenthesized
    assert body["jql"] == 'project = "PROJ" AND (status = Open)'


def test_search_enforces_project_filter_with_order_by():
    """ORDER BY を伴う JQL で、括弧の付け方が正しいこと。"""
    adapter, transport = jira({"/search/jql": ok({"issues": []})})
    adapter.invoke("search", {"jql": "status = Open ORDER BY updated DESC"})

    body = transport.requests[0][2]
    assert body["jql"] == 'project = "PROJ" AND (status = Open) ORDER BY updated DESC'


def test_search_without_jql_conditions():
    """JQL が空でも、安全なプロジェクト指定だけが渡る。"""
    adapter, transport = jira({"/search/jql": ok({"issues": []})})
    adapter.invoke("search", {"jql": ""})

    body = transport.requests[0][2]
    assert body["jql"] == 'project = "PROJ"'


def test_search_names_its_fields_explicitly():
    """新しい検索は既定で id しか返さない。書かないと中身が空に見える。

    The endpoint returns id alone by default; omitting fields makes results
    look empty for reasons that point nowhere useful.
    """
    adapter, transport = jira({"/search/jql": ok({"issues": []})})
    adapter.invoke("search", {"jql": "project = PROJ"})

    body = transport.requests[0][2]
    assert "summary" in body["fields"]
    assert "assignee" in body["fields"]


def test_410_explains_the_removed_endpoint():
    adapter, _ = jira({"/search/jql": ok({"errorMessages": ["gone"]}, status=410)})
    with pytest.raises(AdapterError, match="削除"):
        adapter.invoke("search", {"jql": "x"})


def test_search_flattens_the_response():
    adapter, _ = jira({"/search/jql": ok({"issues": [
        {"key": "PROJ-7", "fields": {
            "summary": "API互換対応", "duedate": "2026-08-20",
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "佐藤 花子", "accountId": "acc-9"}}},
    ]})})

    items = adapter.invoke("search", {"jql": "x"})["items"]
    assert items[0]["key"] == "PROJ-7"
    assert items[0]["assignee"] == "佐藤 花子"
    assert items[0]["status"] == "In Progress"


def test_next_page_token_is_surfaced():
    """ページ送りは startAt ではなくトークンになった。"""
    adapter, _ = jira({"/search/jql": ok({"issues": [], "nextPageToken": "abc"})})
    assert adapter.invoke("search", {"jql": "x"})["next_page_token"] == "abc"


def test_find_overdue_excludes_done_work():
    adapter, transport = jira({"/search/jql": ok({"issues": []})})
    adapter.invoke("find_overdue", {"project": "PROJ"})

    jql = transport.requests[0][2]["jql"]
    assert "statusCategory != Done" in jql
    assert "duedate <" in jql


# --- 担当者 / assignees ------------------------------------------------------

def test_assignee_is_resolved_to_an_account_id():
    """氏名やメールでは設定できない。accountId でしか指定できない。"""
    adapter, transport = jira({
        "/search/jql": ok({"issues": []}),
        "/user/search": ok([{"accountId": "acc-42", "displayName": "佐藤 花子"}]),
        "/issue/bulk": ok({"issues": [{"key": "PROJ-1"}]}),
    })
    adapter.invoke("create_issues", {
        "issues": [{"summary": "s", "assignee": "佐藤 花子"}]})

    fields = [r for r in transport.requests if "bulk" in r[1]][0][2]["issueUpdates"][0]["fields"]
    assert fields["assignee"] == {"accountId": "acc-42"}


def test_unresolvable_assignee_leaves_the_issue_unassigned():
    """1人分からないだけで課題が1件も作られない、という形にしない。

    An unassigned issue can be fixed later; a run that created nothing cannot.
    """
    adapter, _ = jira({
        "/search/jql": ok({"issues": []}),
        "/user/search": ok([]),
        "/issue/bulk": ok({"issues": [{"key": "PROJ-1"}]}),
    })
    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "s", "assignee": "存在しない人"}]})

    assert result["count"] == 1
    assert result["unassigned"] == ["存在しない人"]


def test_assignee_lookups_are_cached():
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return ok([{"accountId": "acc-1"}])

    adapter, _ = jira({
        "/search/jql": ok({"issues": []}),
        "/user/search": counting,
        "/issue/bulk": ok({"issues": [{"key": "A-1"}, {"key": "A-2"}]}),
    })
    adapter.invoke("create_issues", {"issues": [
        {"summary": "a", "assignee": "佐藤"},
        {"summary": "b", "assignee": "佐藤"},
    ]})
    assert calls["n"] == 1


# --- 冪等性 / idempotency ----------------------------------------------------

def test_the_idempotency_key_is_carried_as_a_label():
    adapter, transport = jira({
        "/search/jql": ok({"issues": []}),
        "/user/search": ok([]),
        "/issue/bulk": ok({"issues": [{"key": "PROJ-1"}]}),
    })
    adapter.invoke("create_issues", {
        "issues": [{"summary": "s"}], "idempotency_key": "meeting:M1"})

    labels = [r for r in transport.requests
              if "bulk" in r[1]][0][2]["issueUpdates"][0]["fields"]["labels"]
    assert any("meeting:M1" in label for label in labels)


def test_reprocessing_the_same_meeting_does_not_duplicate_issues():
    """会議を2回処理しても課題が二重にならないこと。"""
    adapter, transport = jira({
        "/search/jql": ok({"issues": [{"key": "PROJ-1", "fields": {}}]}),
        "/issue/bulk": ok({"issues": [{"key": "PROJ-99"}]}),
    })
    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "s"}], "idempotency_key": "meeting:M1"})

    assert result["created"] == ["PROJ-1"]
    assert "already created" in result["skipped"]
    assert not any("bulk" in r[1] for r in transport.requests)


def test_partial_failure_is_reported_not_hidden():
    """一部だけ失敗したのを成功として返さない。"""
    adapter, _ = jira({
        "/search/jql": ok({"issues": []}),
        "/user/search": ok([]),
        "/issue/bulk": ok({"issues": [{"key": "PROJ-1"}],
                           "errors": [{"status": 400, "elementErrors": {}}]}),
    })
    result = adapter.invoke("create_issues", {
        "issues": [{"summary": "a"}, {"summary": "b"}]})

    assert result["count"] == 1
    assert result["failed_count"] == 1


def test_empty_issue_list_is_not_an_error():
    adapter, transport = jira({})
    result = adapter.invoke("create_issues", {"issues": []})
    assert result["count"] == 0 and transport.requests == []


def test_401_mentions_token_expiry():
    adapter, _ = jira({"/search/jql": ok({}, status=401)})
    with pytest.raises(AdapterError, match="API トークン"):
        adapter.invoke("search", {"jql": "x"})


# ===== Slack ================================================================

def slack(routes, **kwargs) -> tuple[SlackAdapter, FakeTransport]:
    transport = FakeTransport(routes)
    return SlackAdapter(token="xoxb-t", transport=transport, max_retries=2,
                        **kwargs), transport


def test_slack_missing_token_is_named():
    adapter = SlackAdapter(transport=FakeTransport({}))
    with pytest.raises(AdapterError, match="token"):
        adapter.invoke("post_message", {"channel": "#x", "text": "hi"})


def test_a_200_with_ok_false_is_a_failure():
    """Slack は失敗しても 200 を返す。ここを見落とすと、
    1件も届いていないのに全部成功と報告し続ける。

    Slack answers 200 on failure. Missing this reports every send as
    successful while nothing arrives.
    """
    adapter, _ = slack({"chat.postMessage":
                        ok({"ok": False, "error": "channel_not_found"})})

    with pytest.raises(AdapterError, match="channel_not_found"):
        adapter.invoke("post_message", {"channel": "#nope", "text": "hi"})


def test_common_errors_carry_a_usable_hint():
    adapter, _ = slack({"chat.postMessage":
                        ok({"ok": False, "error": "not_in_channel"})})

    with pytest.raises(AdapterError, match="invite"):
        adapter.invoke("post_message", {"channel": "#x", "text": "hi"})


def test_permanent_errors_are_not_retried():
    """再送しても実らないものを待たない。"""
    adapter, transport = slack({"chat.postMessage":
                                ok({"ok": False, "error": "invalid_auth"})})
    with pytest.raises(AdapterError):
        adapter.invoke("post_message", {"channel": "#x", "text": "hi"})

    assert len(transport.requests) == 1


def test_successful_send_returns_the_timestamp():
    adapter, _ = slack({"chat.postMessage":
                        ok({"ok": True, "ts": "1724.0001", "channel": "C1"})})
    result = adapter.invoke("post_message", {"channel": "#x", "text": "hi"})

    assert result["ok"] is True
    assert result["ts"] == "1724.0001"
    # スレッドに続けられるように返す / so a reply can continue the thread
    assert result["thread_ts"] == "1724.0001"


def test_rate_limiting_is_retried_then_reported():
    def throttled():
        return 429, {"Retry-After": "0"}, b'{"ok":false,"error":"ratelimited"}'

    adapter, transport = slack({"chat.postMessage": throttled})
    with pytest.raises(AdapterError, match="制限"):
        adapter.invoke("post_message", {"channel": "#x", "text": "hi"})

    assert len(transport.requests) == 2


def test_invalid_json_is_not_treated_as_success():
    adapter, _ = slack({"chat.postMessage": (200, {}, b"<html>oops</html>")})
    with pytest.raises(AdapterError, match="invalid_json"):
        adapter.invoke("post_message", {"channel": "#x", "text": "hi"})


def test_default_channel_is_used_when_none_given():
    adapter, transport = slack({"chat.postMessage": ok({"ok": True, "ts": "1"})},
                               default_channel="#updates")
    adapter.invoke("post_message", {"text": "hi"})
    assert transport.requests[0][2]["channel"] == "#updates"


def test_user_lookup_produces_a_mention_id():
    """氏名では通知が飛ばない。ID が要る。"""
    adapter, _ = slack({"users.lookupByEmail":
                        ok({"ok": True, "user": {"id": "U123", "real_name": "佐藤"}})})
    found = adapter.invoke("find_user", {"email": "sato@example.com"})
    assert found["mention"] == "<@U123>"


def test_reading_actions_are_not_writes():
    adapter, _ = slack({})
    assert adapter.writes("post_message") is True
    assert adapter.writes("find_user") is False
    assert adapter.writes("list_channels") is False
    assert adapter.writes("get_reactions") is False


def test_get_reactions_lists_names_and_users():
    adapter, _ = slack({"reactions.get": ok({
        "ok": True,
        "message": {"reactions": [
            {"name": "white_check_mark", "users": ["U1", "U2"], "count": 2},
        ]},
    })})
    result = adapter.invoke("get_reactions", {"channel": "C1", "ts": "1.0"})

    assert result["reactions"] == [{"name": "white_check_mark", "users": ["U1", "U2"]}]


def test_get_reactions_is_empty_when_none_are_present():
    """反応が無いメッセージでは Slack が reactions キー自体を返さない。"""
    adapter, _ = slack({"reactions.get": ok({
        "ok": True, "message": {"text": "hi"},
    })})
    result = adapter.invoke("get_reactions", {"channel": "C1", "ts": "1.0"})

    assert result["reactions"] == []


def test_jira_write_actions_are_marked():
    adapter, _ = jira({})
    assert adapter.writes("create_issues") is True
    assert adapter.writes("add_comment") is True
    assert adapter.writes("search") is False
    assert adapter.writes("find_overdue") is False


# --- 更新 / updating --------------------------------------------------------

def test_only_the_given_fields_are_sent():
    """指定しなかった項目に触れないこと。

    更新の誤りは、すでに正しかった値を消す。作成の誤りより重い。
    A mistaken update destroys a value that was right — heavier than a
    mistaken create, which merely adds noise.
    """
    adapter, transport = jira({"/issue/PROJ-7": ok({}, status=204)})
    adapter.invoke("update_issue", {"issue_key": "PROJ-7",
                                    "due_date": "2026-09-10"})

    fields = transport.requests[0][2]["fields"]
    assert fields == {"duedate": "2026-09-10"}
    assert "assignee" not in fields and "summary" not in fields


def test_nothing_is_sent_when_there_is_nothing_to_change():
    adapter, transport = jira({})
    result = adapter.invoke("update_issue", {"issue_key": "PROJ-7"})

    assert result["changed"] == []
    assert transport.requests == []


def test_an_unresolvable_name_does_not_unassign_anyone():
    """引き当てられなかったからといって、既存の担当を消さない。"""
    adapter, transport = jira({
        "/user/search": ok([]),
        "/issue/PROJ-7": ok({}, status=204),
    })
    result = adapter.invoke("update_issue", {"issue_key": "PROJ-7",
                                             "assignee": "知らない人"})

    sent = [r for r in transport.requests if r[0] == "PUT"]
    assert sent == [] or "assignee" not in sent[0][2].get("fields", {})
    assert result["unresolved_assignee"] == "知らない人"


def test_labels_are_added_not_replaced():
    adapter, transport = jira({"/issue/PROJ-7": ok({}, status=204)})
    adapter.invoke("update_issue", {"issue_key": "PROJ-7",
                                    "add_labels": ["reviewed"]})

    update = transport.requests[0][2]["update"]
    assert update["labels"] == [{"add": "reviewed"}]


def test_a_comment_can_accompany_the_change():
    """なぜ変わったかを課題に残せること。後から辿れないと信用されない。"""
    adapter, transport = jira({"/issue/PROJ-7": ok({}, status=204)})
    adapter.invoke("update_issue", {"issue_key": "PROJ-7",
                                    "due_date": "2026-09-10",
                                    "comment": "8/28 の定例で変更"})

    body = transport.requests[0][2]["update"]["comment"][0]["add"]["body"]
    assert body["type"] == "doc"


# --- 状態遷移 / transitions -------------------------------------------------

TRANSITIONS = {"transitions": [
    {"id": "21", "name": "作業開始", "to": {"name": "In Progress"}},
    {"id": "31", "name": "完了にする", "to": {"name": "Done"}},
]}


def test_status_moves_through_a_transition_not_a_field():
    """Jira の状態は項目の書き換えでは変えられない。"""
    adapter, transport = jira({
        "/transitions": ok(TRANSITIONS),
    })
    adapter.invoke("transition_issue", {"issue_key": "PROJ-7",
                                        "to_status": "Done"})

    posted = [r for r in transport.requests if r[0] == "POST"][0]
    assert posted[2]["transition"] == {"id": "31"}


def test_the_destination_status_name_is_accepted():
    adapter, _ = jira({"/transitions": ok(TRANSITIONS)})
    result = adapter.invoke("transition_issue", {"issue_key": "PROJ-7",
                                                 "to_status": "done"})
    assert result["to"] == "Done"


def test_the_transition_name_is_also_accepted():
    """ワークフローによって呼び名が違う。どちらでも通るようにする。"""
    adapter, _ = jira({"/transitions": ok(TRANSITIONS)})
    result = adapter.invoke("transition_issue", {"issue_key": "PROJ-7",
                                                 "to_status": "完了にする"})
    assert result["to"] == "Done"


def test_an_impossible_move_lists_what_is_possible():
    """いまの状態から進めない先には、理由がある。何ができるかを示して止まる。"""
    adapter, _ = jira({"/transitions": ok(TRANSITIONS)})
    with pytest.raises(AdapterError, match="作業開始"):
        adapter.invoke("transition_issue", {"issue_key": "PROJ-7",
                                            "to_status": "Cancelled"})


def test_transitions_are_write_actions_and_listing_is_not():
    adapter, _ = jira({})
    assert adapter.writes("transition_issue") is True
    assert adapter.writes("update_issue") is True
    assert adapter.writes("list_transitions") is False
