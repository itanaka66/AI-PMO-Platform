"""Slack 経由の承認のテスト / tests for Slack-based approval.

実際に待つ・実際に Slack へ届くことは確かめない（アダプタ自体のテストは
test_jira_slack.py にある）。ここで見るのは、反応をどう判定に変えるか、
ポーリングがいつ止まるかという `SlackApprover` 自身のふるまい。

Does not check real waiting or real delivery — the adapter itself is tested
in test_jira_slack.py. What matters here is `SlackApprover`'s own behaviour:
how a reaction becomes a decision, and when polling stops.
"""
from __future__ import annotations

import json

from aipmo.adapters.slack import SlackAdapter
from aipmo.approval import SlackApprover


class FakeTransport:
    """test_jira_slack.py と同じ形。requests をそのまま記録する。"""

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
        return 404, {}, b'{"errorMessages":["no route"]}'


def ok(payload: dict, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def build(routes, **kwargs) -> tuple[SlackApprover, FakeTransport]:
    transport = FakeTransport(routes)
    slack = SlackAdapter(token="xoxb-t", transport=transport, max_retries=1)
    approver = SlackApprover(slack=slack, channel="#approvals", **kwargs)
    return approver, transport


def reactions_sequence(*replies):
    """reactions.get への呼び出しごとに、順番に用意した応答を返す。"""
    it = iter(replies)

    def route():
        return ok({"ok": True, "message": {"reactions": next(it)}})

    return route


def test_approves_when_the_approve_emoji_appears(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    approver, transport = build({
        "chat.postMessage": ok({"ok": True, "ts": "1.0", "channel": "C1"}),
        "reactions.get": reactions_sequence(
            [],  # 1回目: まだ何も無い
            [{"name": "white_check_mark", "users": ["U1"], "count": 1}],
        ),
    }, poll_seconds=0, timeout_seconds=10)

    result = approver("jira.create_issues", {"project": "PROJ"})

    assert result is True
    # 提案の投稿と、決定後の返信の両方が飛んでいること。
    posted = [r for r in transport.requests if "chat.postMessage" in r[1]]
    assert len(posted) == 2
    assert "承認されました" in posted[1][2]["text"] or "approved" in posted[1][2]["text"]


def test_declines_when_the_decline_emoji_appears(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    approver, _ = build({
        "chat.postMessage": ok({"ok": True, "ts": "1.0", "channel": "C1"}),
        "reactions.get": reactions_sequence(
            [{"name": "x", "users": ["U1"], "count": 1}],
        ),
    }, poll_seconds=0, timeout_seconds=10)

    assert approver("jira.create_issues", {"project": "PROJ"}) is False


def test_times_out_and_declines_with_no_reaction(monkeypatch):
    """反応が無いまま期限が来たら、対話端末の承認と同じく通さない。"""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    # 開始, ループ1周目（まだ間に合う）, ループを抜ける判定
    clock = iter([0.0, 0.005, 0.02])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))

    approver, transport = build({
        "chat.postMessage": ok({"ok": True, "ts": "1.0", "channel": "C1"}),
        "reactions.get": reactions_sequence([]),
    }, poll_seconds=0, timeout_seconds=0.01)

    assert approver("jira.create_issues", {"project": "PROJ"}) is False
    posted = [r for r in transport.requests if "chat.postMessage" in r[1]]
    assert "反応が無かった" in posted[1][2]["text"] or "no response" in posted[1][2]["text"]


def test_only_a_listed_approver_id_counts_when_restricted(monkeypatch):
    """approver_ids を絞ったら、それ以外の反応は無視する。"""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    approver, _ = build({
        "chat.postMessage": ok({"ok": True, "ts": "1.0", "channel": "C1"}),
        "reactions.get": reactions_sequence(
            [{"name": "white_check_mark", "users": ["U_STRANGER"], "count": 1}],
            [{"name": "white_check_mark", "users": ["U_STRANGER", "U_BOSS"], "count": 2}],
        ),
    }, poll_seconds=0, timeout_seconds=10, approver_ids=frozenset({"U_BOSS"}))

    assert approver("jira.create_issues", {"project": "PROJ"}) is True


def test_any_reaction_counts_when_approver_ids_is_unset(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    approver, _ = build({
        "chat.postMessage": ok({"ok": True, "ts": "1.0", "channel": "C1"}),
        "reactions.get": reactions_sequence(
            [{"name": "white_check_mark", "users": ["U_ANYONE"], "count": 1}],
        ),
    }, poll_seconds=0, timeout_seconds=10)

    assert approver("jira.create_issues", {"project": "PROJ"}) is True


def test_a_failed_reaction_read_does_not_crash_the_poll(monkeypatch):
    """読み取りに失敗しても、次のポーリングで直ることがあるので輪を止めない。"""
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    def broken():
        return 200, {}, b"<html>oops</html>"

    calls = {"n": 0}

    def route():
        calls["n"] += 1
        if calls["n"] == 1:
            return broken()
        return ok({"ok": True, "message": {"reactions": [
            {"name": "white_check_mark", "users": ["U1"], "count": 1},
        ]}})

    approver, _ = build({
        "chat.postMessage": ok({"ok": True, "ts": "1.0", "channel": "C1"}),
        "reactions.get": route,
    }, poll_seconds=0, timeout_seconds=10)

    assert approver("jira.create_issues", {"project": "PROJ"}) is True


def test_the_approver_describes_the_tool_and_arguments():
    approver, _ = build({})
    text = approver._describe("jira.create_issues", {"project": "PROJ"})

    assert "jira.create_issues" in text
    assert "PROJ" in text
    assert "white_check_mark" in text and "x" in text
