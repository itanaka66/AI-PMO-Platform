"""Teams アダプタのテスト / Teams adapter tests.

実テナントには繋げないので、HTTP 層を差し替えて実データの形を再現する。
Graph の応答形と VTT の癖を、ネットワークなしで確かめられるようにしてある。

A real tenant is unreachable from a test suite, so the HTTP layer is replaced
and the real shapes are reproduced. Graph's responses and WebVTT's quirks are
exercised without network.
"""
from __future__ import annotations

import json

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.teams import TeamsAdapter
from aipmo.adapters.teams_vtt import (
    merge_consecutive,
    parse_vtt,
    speakers,
    to_text,
)

# Teams が実際に返す形。キュー識別子は UUID で、発言者は <v> タグ。
# The shape Teams actually returns: UUID cue ids and speakers in <v> tags.
SAMPLE_VTT = """WEBVTT

d1b0f7e2-3a4c-4f5e-8a9b-1c2d3e4f5a6b/1-0
00:00:03.120 --> 00:00:07.480
<v 田中 健一>認証基盤の移行ですが、来週金曜までに</v>

d1b0f7e2-3a4c-4f5e-8a9b-1c2d3e4f5a6b/2-0
00:00:07.480 --> 00:00:10.900
<v 田中 健一>設計レビューを終えたいと考えています。</v>

d1b0f7e2-3a4c-4f5e-8a9b-1c2d3e4f5a6b/3-0
00:00:11.200 --> 00:00:16.050
<v 佐藤 美咲>API 側の互換対応が残っています。水曜までに一覧を出します。

d1b0f7e2-3a4c-4f5e-8a9b-1c2d3e4f5a6b/4-0
00:00:16.500 --> 00:00:20.000
<v 田中 健一>では鈴木さんは負荷試験の環境準備をお願いします。</v>
"""


# --- VTT の解析 / parsing --------------------------------------------------

def test_speakers_are_extracted():
    utterances = parse_vtt(SAMPLE_VTT)
    assert utterances[0].speaker == "田中 健一"
    assert "認証基盤" in utterances[0].text


def test_cue_identifiers_are_not_treated_as_dialogue():
    """UUID のキュー識別子が発言として混ざると、議事録が汚れる。"""
    for utterance in parse_vtt(SAMPLE_VTT):
        assert "d1b0f7e2" not in utterance.text


def test_webvtt_header_is_skipped():
    assert all("WEBVTT" not in u.text for u in parse_vtt(SAMPLE_VTT))


def test_missing_closing_tag_is_tolerated():
    """Teams は </v> を省くことがある。落とさず読めること。"""
    utterances = parse_vtt(SAMPLE_VTT)
    sato = [u for u in utterances if u.speaker == "佐藤 美咲"]
    assert sato and "互換対応" in sato[0].text


def test_timings_are_captured():
    first = parse_vtt(SAMPLE_VTT)[0]
    assert first.start == "00:00:03.120"
    assert first.end == "00:00:07.480"


def test_consecutive_utterances_are_merged():
    """WebVTT は数秒で切れる。文の途中で渡すと発言の意図を取り違える。"""
    merged = merge_consecutive(parse_vtt(SAMPLE_VTT))
    first = merged[0]

    assert first.speaker == "田中 健一"
    assert "来週金曜まで" in first.text and "設計レビュー" in first.text
    assert first.end == "00:00:10.900"      # 末尾は最後の断片のもの


def test_merge_does_not_join_across_speakers():
    merged = merge_consecutive(parse_vtt(SAMPLE_VTT))
    assert [u.speaker for u in merged] == ["田中 健一", "佐藤 美咲", "田中 健一"]


def test_speaker_order_is_stable():
    """参加者欄が毎回並び替わると、議事録の差分が読めなくなる。"""
    names = speakers(parse_vtt(SAMPLE_VTT))
    assert names == ["田中 健一", "佐藤 美咲"]


def test_unattributed_speech_is_kept():
    """発言者不明でも捨てない。内容は議事録に要る。"""
    utterances = parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n決定しました\n")
    assert utterances[0].speaker is None
    assert utterances[0].text == "決定しました"


def test_to_text_labels_unknown_speakers():
    text = to_text([*parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nはい\n")])
    assert "(不明): はい" in text


def test_empty_transcript_yields_nothing():
    assert parse_vtt("WEBVTT\n\n") == []


# --- HTTP の差し替え / fake transport --------------------------------------

class FakeTransport:
    def __init__(self, routes: dict[str, tuple[int, dict, bytes]]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        self.requests.append((method, url))
        # URL の後ろに現れる断片ほど具体的とみなす。本文の URL は
        # ".../transcripts/{id}/content" で、"/transcripts" も "/content" も
        # 含む。長さで選ぶと "/transcripts" が勝ってしまい、本文を求めた
        # ところに JSON が返る。
        # The fragment appearing latest in the URL is the specific one. A
        # content URL is ".../transcripts/{id}/content" and contains both
        # "/transcripts" and "/content"; choosing by length picks the former
        # and returns JSON where VTT was asked for.
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            return self.routes[max(matches)[1]]
        return 404, {}, b'{"error":{"message":"no route"}}'


def ok(payload: dict) -> tuple[int, dict, bytes]:
    return 200, {}, json.dumps(payload).encode("utf-8")


TOKEN = ok({"access_token": "tok", "expires_in": 3600})


def build(routes: dict, **kwargs) -> tuple[TeamsAdapter, FakeTransport]:
    transport = FakeTransport({"oauth2/v2.0/token": TOKEN, **routes})
    adapter = TeamsAdapter(
        tenant_id="t", client_id="c", client_secret="s",
        organiser_id="organiser@example.com", transport=transport,
        max_retries=2, **kwargs,
    )
    return adapter, transport


# --- 認証 / authentication -------------------------------------------------

def test_missing_configuration_is_named():
    adapter = TeamsAdapter(transport=FakeTransport({}))
    with pytest.raises(AdapterError, match="tenant_id"):
        adapter._access_token()


def test_token_is_cached_between_calls():
    """毎回取り直すと、Graph の絞り込みに引っかかる。"""
    adapter, transport = build({
        "/transcripts": ok({"value": [{"id": "t1", "createdDateTime": "2026-08-27T01:00:00Z"}]}),
        "/content": (200, {}, SAMPLE_VTT.encode("utf-8")),
    })
    adapter.invoke("list_transcripts", {"meeting_id": "m1"})
    adapter.invoke("list_transcripts", {"meeting_id": "m1"})

    token_calls = [u for _, u in transport.requests if "oauth2" in u]
    assert len(token_calls) == 1


def test_token_is_renewed_before_it_expires():
    """期限ちょうどまで使うと、要求の途中で切れる。"""
    now = [0.0]
    adapter, transport = build(
        {"/transcripts": ok({"value": []})}, clock=lambda: now[0])

    adapter._access_token()
    now[0] = 3600 - 299        # 猶予 300 秒の内側 / inside the 300s margin
    adapter._access_token()

    assert len([u for _, u in transport.requests if "oauth2" in u]) == 2


def test_token_failure_reports_the_reason():
    transport = FakeTransport({"oauth2": (401, {}, json.dumps(
        {"error": "invalid_client",
         "error_description": "client secret is expired"}).encode())})
    adapter = TeamsAdapter(tenant_id="t", client_id="c", client_secret="bad",
                           transport=transport)

    with pytest.raises(AdapterError, match="expired"):
        adapter._access_token()


# --- 権限まわり / permissions ----------------------------------------------

def test_403_explains_the_application_access_policy():
    """権限を付けても、アクセスポリシー未設定なら 403 が続く。
    ここで手順を出さないと、利用者は原因に辿り着けない。"""
    adapter, _ = build({"/transcripts": (403, {}, json.dumps(
        {"error": {"message": "Forbidden"}}).encode())})

    with pytest.raises(AdapterError, match="Grant-CsApplicationAccessPolicy"):
        adapter.invoke("list_transcripts", {"meeting_id": "m1"})


def test_403_message_includes_the_app_id():
    adapter, _ = build({"/transcripts": (403, {}, b'{"error":{"message":"x"}}')})
    with pytest.raises(AdapterError, match="-AppIds c"):
        adapter.invoke("list_transcripts", {"meeting_id": "m1"})


# --- 会議の特定 / meeting lookup -------------------------------------------

def test_meeting_is_found_by_join_url():
    """カレンダーが持っているのは参加 URL であって会議 ID ではない。"""
    adapter, transport = build({"/onlineMeetings?": ok({"value": [
        {"id": "MSo...", "subject": "設計レビュー",
         "startDateTime": "2026-08-27T01:00:00Z",
         "endDateTime": "2026-08-27T02:00:00Z"}]})})

    result = adapter.invoke("find_meeting",
                            {"join_url": "https://teams.microsoft.com/l/meetup-join/abc"})

    assert result["meeting_id"] == "MSo..."
    assert result["subject"] == "設計レビュー"


def test_join_url_is_url_encoded_in_the_filter():
    """素のまま入れるとフィルタが壊れる。"""
    adapter, transport = build({"/onlineMeetings?": ok({"value": [{"id": "x"}]})})
    adapter.invoke("find_meeting", {"join_url": "https://teams.microsoft.com/l/a?b=c"})

    url = [u for _, u in transport.requests if "onlineMeetings" in u][0]
    assert "https%3A%2F%2Fteams" in url


def test_unknown_join_url_is_reported():
    adapter, _ = build({"/onlineMeetings?": ok({"value": []})})
    with pytest.raises(AdapterError, match="no meeting"):
        adapter.invoke("find_meeting", {"join_url": "https://x"})


def test_organiser_is_required():
    transport = FakeTransport({"oauth2/v2.0/token": TOKEN})
    adapter = TeamsAdapter(tenant_id="t", client_id="c", client_secret="s",
                           transport=transport)
    with pytest.raises(AdapterError, match="organiser_id"):
        adapter.invoke("find_meeting", {"join_url": "https://x"})


# --- Transcript の取得 / fetching ------------------------------------------

def test_transcript_is_parsed_into_text_and_participants():
    adapter, _ = build({
        "/transcripts": ok({"value": [
            {"id": "t1", "createdDateTime": "2026-08-27T02:05:00Z"}]}),
        "/content": (200, {}, SAMPLE_VTT.encode("utf-8")),
    })

    result = adapter.invoke("get_transcript", {"meeting_id": "m1"})

    assert result["participants"] == ["田中 健一", "佐藤 美咲"]
    assert "田中 健一: 認証基盤" in result["text"]
    assert result["transcript_id"] == "t1"


def test_newest_transcript_wins():
    """録り直した会議で古い方を使うと、議事録が実際と食い違う。"""
    adapter, transport = build({
        "/transcripts": ok({"value": [
            {"id": "old", "createdDateTime": "2026-08-27T01:00:00Z"},
            {"id": "new", "createdDateTime": "2026-08-27T03:00:00Z"},
        ]}),
        "/content": (200, {}, SAMPLE_VTT.encode("utf-8")),
    })

    result = adapter.invoke("get_transcript", {"meeting_id": "m1"})
    assert result["transcript_id"] == "new"


def test_absent_transcript_explains_the_delay():
    """会議終了と同時には出ない。原因を書かないと利用者は設定を疑う。"""
    adapter, _ = build({"/transcripts": ok({"value": []})})

    with pytest.raises(AdapterError, match="wait_seconds"):
        adapter.invoke("get_transcript", {"meeting_id": "m1"})


def test_vtt_is_requested_as_text():
    adapter, transport = build({
        "/transcripts": ok({"value": [{"id": "t1", "createdDateTime": "z"}]}),
        "/content": (200, {}, SAMPLE_VTT.encode("utf-8")),
    })
    adapter.invoke("get_transcript", {"meeting_id": "m1"})

    content_url = [u for _, u in transport.requests if "/content" in u][0]
    assert "text/vtt" in content_url


# --- 絞り込みへの対応 / throttling -----------------------------------------

def test_retry_after_is_honoured(monkeypatch):
    """Graph の指示より短い間隔で叩き直しても、結局通らない。"""
    slept: list[float] = []
    monkeypatch.setattr("aipmo.adapters.teams.time.sleep", slept.append)

    calls = {"n": 0}

    class Throttling(FakeTransport):
        def request(self, method, url, headers, body=None, timeout=60.0):
            if "oauth2" in url:
                return TOKEN
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, {"Retry-After": "7"}, b"{}"
            return ok({"value": []})

    adapter = TeamsAdapter(tenant_id="t", client_id="c", client_secret="s",
                           organiser_id="u", transport=Throttling({}),
                           max_retries=3)
    adapter.invoke("list_transcripts", {"meeting_id": "m1"})

    assert slept == [7.0]


def test_expired_token_is_refetched_on_401(monkeypatch):
    monkeypatch.setattr("aipmo.adapters.teams.time.sleep", lambda _: None)
    calls = {"n": 0}

    class Expiring(FakeTransport):
        def request(self, method, url, headers, body=None, timeout=60.0):
            if "oauth2" in url:
                return TOKEN
            calls["n"] += 1
            if calls["n"] == 1:
                return 401, {}, b"{}"
            return ok({"value": []})

    adapter = TeamsAdapter(tenant_id="t", client_id="c", client_secret="s",
                           organiser_id="u", transport=Expiring({}), max_retries=3)
    result = adapter.invoke("list_transcripts", {"meeting_id": "m1"})

    assert result["count"] == 0
    assert calls["n"] == 2


# --- エージェントとの結線 / agent wiring ------------------------------------

def test_transcript_actions_are_read_only():
    """Transcript を読む操作が書き込み扱いになっていないこと。
    エージェントに読み取りだけ許す構成が成立しなくなる。"""
    adapter, _ = build({})
    assert adapter.writes("get_transcript") is False
    assert adapter.writes("find_meeting") is False


def test_actions_describe_themselves_for_tool_use():
    adapter, _ = build({})
    described = adapter.describe()

    assert "get_transcript" in described
    params = described["get_transcript"]["parameters"]
    assert params["properties"]["meeting_id"]["type"] == "string"
    assert params["properties"]["wait_seconds"]["type"] == "integer"
    assert params["required"] == ["meeting_id"]


# --- 予定表 / calendar ------------------------------------------------------

def test_calendar_entries_expose_the_join_url():
    """参加 URL が取れないと、find_meeting に渡すものが無い。

    Without the join URL there is nothing to hand to find_meeting.
    """
    adapter, _ = build({"calendarView": ok({"value": [
        {"id": "e1", "subject": "定例",
         "start": {"dateTime": "2026-08-28T09:00:00"},
         "end": {"dateTime": "2026-08-28T10:00:00"},
         "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/x"}},
    ]})})

    items = adapter.invoke("upcoming_meetings", {})["items"]
    assert items[0]["join_url"].startswith("https://teams.microsoft.com")
    assert items[0]["subject"] == "定例"


def test_calendar_entry_without_an_online_meeting_is_kept():
    """対面の予定も返る。join_url は None になるだけで、落としてはいけない。"""
    adapter, _ = build({"calendarView": ok({"value": [
        {"id": "e2", "subject": "対面打合せ",
         "start": {"dateTime": "2026-08-28T09:00:00"},
         "end": {"dateTime": "2026-08-28T10:00:00"}},
    ]})})

    items = adapter.invoke("upcoming_meetings", {})["items"]
    assert items[0]["join_url"] is None


def test_calendar_requires_an_organiser():
    adapter = TeamsAdapter(tenant_id="t", client_id="c", client_secret="s",
                           transport=FakeTransport({"oauth2/v2.0/token": TOKEN}))
    with pytest.raises(AdapterError, match="organiser_id"):
        adapter.invoke("upcoming_meetings", {})


def test_reading_actions_are_not_writes():
    """エージェントに読み取りだけを渡せること。"""
    adapter, _ = build({})
    for name in ("get_transcript", "find_meeting", "upcoming_meetings"):
        assert adapter.writes(name) is False
