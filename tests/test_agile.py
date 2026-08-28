"""Jira Agile のテスト / Jira Agile tests.

黙って壊れる形を潰すのが主眼。特に、見積もり項目が引けなかったときに
「全件が見積もり無し」に見えてしまう経路。

Focus on the failure shapes that stay silent — above all the one where an
unresolved estimation field makes every item read as unestimated.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.jira import JiraAdapter
from aipmo.adapters.jira_agile import JiraAgileAdapter

BOARD_CONFIG = {"estimation": {"field": {"fieldId": "customfield_10099",
                                         "displayName": "Story Points"}}}


class FakeTransport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        self.requests.append((method, url, json.loads(body) if body else {}))
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            response = self.routes[max(matches)[1]]
            return response() if callable(response) else response
        return 404, {}, b'{"errorMessages":["no route"]}'


def ok(payload, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def agile(routes, **kwargs):
    transport = FakeTransport(routes)
    client = JiraAdapter(site="https://acme.atlassian.net", email="a@b.c",
                         api_token="t", transport=transport, max_retries=1)
    return JiraAgileAdapter(board_id=7, client=client, **kwargs), transport


def issue(key, *, status="To Do", category="new", points=None, assignee=None):
    fields = {
        "summary": f"{key} の作業",
        "status": {"name": status, "statusCategory": {"key": category}},
        "issuetype": {"name": "Story"},
        "updated": "2026-08-27T10:00:00.000+0900",
    }
    if assignee:
        fields["assignee"] = {"displayName": assignee}
    if points is not None:
        fields["customfield_10099"] = points
    return {"key": key, "fields": fields}


# --- ボードとスプリント / boards and sprints --------------------------------

def test_board_id_is_required_and_explained():
    adapter, _ = agile({})
    adapter.board_id = None
    with pytest.raises(AdapterError, match="list_boards"):
        adapter.invoke("active_sprint", {})


def test_active_sprint_is_returned():
    adapter, _ = agile({"/sprint?state=active": ok({"values": [
        {"id": 42, "name": "Sprint 12", "goal": "認証基盤の移行",
         "startDate": "2026-08-24", "endDate": "2026-09-07"}]})})

    sprint = adapter.invoke("active_sprint", {})
    assert sprint["active"] is True
    assert sprint["id"] == 42 and sprint["goal"] == "認証基盤の移行"


def test_no_active_sprint_is_not_an_error():
    """スプリント間の期間。障害ではないので、後続を止めない形で返す。"""
    adapter, _ = agile({"/sprint?state=active": ok({"values": []})})
    result = adapter.invoke("active_sprint", {})

    assert result["active"] is False
    assert "reason" in result


def test_a_kanban_board_reports_that_it_has_no_sprints():
    """カンバンにスプリントは無い。設定の事実であって障害ではない。"""
    adapter, _ = agile({"/sprint?state=active":
                        ok({"errorMessages": ["Board does not support sprints"]},
                           status=400)})
    result = adapter.invoke("active_sprint", {})

    assert result["active"] is False
    assert "スプリント" in result["reason"]


# --- 見積もり項目 / the estimation field ------------------------------------

def test_the_estimation_field_comes_from_the_board():
    """ID は Jira ごとに違う。他所の customfield_10016 を書くと全件が空になる。

    The id differs per instance; a borrowed one silently yields nothing.
    """
    adapter, transport = agile({"/board/7/configuration": ok(BOARD_CONFIG)})
    assert adapter.estimation_field() == "customfield_10099"
    assert any("configuration" in r[1] for r in transport.requests)


def test_the_estimation_field_is_looked_up_once():
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return ok(BOARD_CONFIG)

    adapter, _ = agile({"/board/7/configuration": counting})
    adapter.estimation_field()
    adapter.estimation_field()
    assert calls["n"] == 1


def test_an_unavailable_board_config_does_not_stop_everything():
    """設定が取れなくても、課題の一覧そのものは返せる。"""
    adapter, _ = agile({
        "/board/7/configuration": ok({}, status=403),
        "/sprint/42/issue": ok({"issues": [issue("PROJ-1")]}),
    })
    result = adapter.invoke("sprint_issues", {"sprint_id": 42})

    assert result["count"] == 1
    assert result["estimation_field"] is None


def test_the_field_in_use_is_reported_back():
    """『全部見積もり無し』に見えたとき、設定を疑えるようにする。"""
    adapter, _ = agile({
        "/board/7/configuration": ok(BOARD_CONFIG),
        "/sprint/42/issue": ok({"issues": [issue("PROJ-1", points=3)]}),
    })
    result = adapter.invoke("sprint_issues", {"sprint_id": 42})
    assert result["estimation_field"] == "customfield_10099"


# --- スプリントの状況 / sprint state ----------------------------------------

def sprint_with(*issues):
    return agile({
        "/board/7/configuration": ok(BOARD_CONFIG),
        "/sprint/42/issue": ok({"issues": list(issues)}),
    })[0].invoke("sprint_issues", {"sprint_id": 42})


def test_done_work_is_counted_by_status_category_not_name():
    """状態名はワークフローごとに違う。カテゴリで判定する。"""
    result = sprint_with(
        issue("PROJ-1", status="完了", category="done", points=3),
        issue("PROJ-2", status="対応中", category="indeterminate", points=5),
    )
    assert result["done_count"] == 1
    assert result["points_done"] == 3.0
    assert result["points_total"] == 8.0


def test_unassigned_open_work_is_surfaced():
    result = sprint_with(
        issue("PROJ-1", assignee="佐藤"),
        issue("PROJ-2"),
        issue("PROJ-3", category="done"),      # 完了済みは担当不在でも問題ない
    )
    assert result["unassigned"] == ["PROJ-2"]


def test_unestimated_work_is_surfaced():
    result = sprint_with(issue("PROJ-1", points=3), issue("PROJ-2"))
    assert result["unestimated"] == ["PROJ-2"]


def test_no_estimates_at_all_gives_none_not_zero():
    """0.0 を返すと『全部終わっている』と読めてしまう。

    A zero would read as "all complete"; having no estimates is not the same as
    summing to nothing.
    """
    result = sprint_with(issue("PROJ-1"), issue("PROJ-2"))
    assert result["points_total"] is None
    assert result["points_done"] is None


def test_an_empty_sprint_is_handled():
    result = sprint_with()
    assert result["count"] == 0 and result["points_total"] is None


# --- 書き込み / writing -----------------------------------------------------

def test_moving_issues_is_batched():
    """1回あたりの上限がある。超えると弾かれる。"""
    adapter, transport = agile({"/sprint/42/issue": ok({})})
    keys = [f"PROJ-{n}" for n in range(120)]
    result = adapter.invoke("move_to_sprint", {"sprint_id": 42, "issue_keys": keys})

    posts = [r for r in transport.requests if r[0] == "POST"]
    assert result["moved"] == 120
    assert len(posts) == 3
    assert all(len(p[2]["issues"]) <= 50 for p in posts)


def test_moving_nothing_sends_nothing():
    adapter, transport = agile({})
    result = adapter.invoke("move_to_sprint", {"sprint_id": 42, "issue_keys": []})
    assert result["moved"] == 0 and transport.requests == []


def test_reading_actions_are_not_writes():
    adapter, _ = agile({})
    assert adapter.writes("move_to_sprint") is True
    for name in ("list_boards", "active_sprint", "sprint_issues", "backlog"):
        assert adapter.writes(name) is False


# --- 数えれば決まる値 / countable facts --------------------------------------
# テンプレートに計算の仕組みは無く、言語モデルに数えさせると間違える。
# 数えれば決まる値は、渡す側で決めておく。
#
# Templates cannot do arithmetic and a language model miscounts. Countable
# facts are settled before they are handed over.

from datetime import datetime, timedelta, timezone  # noqa: E402

from aipmo.adapters.jira_agile import _days_until, _percent  # noqa: E402


def test_days_remaining_rounds_up():
    """残り半日を「0日」と出すと、もう終わっているように読める。"""
    soon = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    assert _days_until(soon) == 1


def test_days_remaining_is_zero_once_past():
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert _days_until(past) == 0


def test_a_date_without_a_timezone_is_read_as_utc():
    """書式ひとつで報告全体を止めない。"""
    assert _days_until("2099-01-01T00:00:00") is not None


def test_an_unreadable_date_yields_nothing_rather_than_raising():
    assert _days_until("not a date") is None
    assert _days_until(None) is None


def test_percentage_uses_points_where_they_exist():
    items = [{"points": 3}, {"points": 5}, {"points": 2}]
    assert _percent(items, [{"points": 8}]) == 80


def test_percentage_falls_back_to_counts_without_points():
    """ポイントを使わないチームもある。件数で出せば足りる。"""
    items = [{"points": None}] * 4
    assert _percent(items, [{"points": None}]) == 25


def test_nothing_completed_yet_is_zero_not_a_crash():
    """未見積もりの None と、完了が無い状態を取り違えない。"""
    assert _percent([{"points": 3}, {"points": 5}], []) == 0


def test_an_empty_sprint_does_not_divide_by_zero():
    assert _percent([], []) == 0


def test_the_sprint_ending_soonest_is_chosen():
    """同時に走っている場合、報告で見たいのは近い方の締め切り。"""
    adapter, _ = agile({"/sprint?state=active": ok({"values": [
        {"id": 9, "name": "遅い方", "endDate": "2026-12-01T00:00:00Z"},
        {"id": 8, "name": "近い方", "endDate": "2026-09-01T00:00:00Z"},
    ]})})

    found = adapter.invoke("active_sprint", {})
    assert found["name"] == "近い方"
    assert found["concurrent_sprints"] == 2


def test_the_active_sprint_carries_its_remaining_days():
    end = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
    adapter, _ = agile({"/sprint?state=active": ok({"values": [
        {"id": 8, "name": "Sprint 12", "endDate": end}]})})

    assert adapter.invoke("active_sprint", {})["days_remaining"] == 4
