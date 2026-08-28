"""Microsoft Teams アダプタ / Microsoft Teams adapter.

Microsoft Graph 経由で、会議の Transcript を取得する。

必要な権限 / Required permissions
----------------------------------
アプリケーション権限で `OnlineMeetingTranscript.Read.All` が要る。
ただし**それだけでは足りない**。テナント管理者が PowerShell で
アプリケーションアクセスポリシーを作り、対象ユーザーに割り当てる必要がある。

  New-CsApplicationAccessPolicy -Identity <name> -AppIds <app-id>
  Grant-CsApplicationAccessPolicy -PolicyName <name> -Identity <user>

これを飛ばすと、権限は付与済みなのに 403 が返り続ける。導入で最も詰まる箇所。

The application permission `OnlineMeetingTranscript.Read.All` is necessary but
**not sufficient**: a tenant administrator must also create an application
access policy in PowerShell and grant it to the users whose meetings will be
read. Skipping it produces a persistent 403 despite the permission being
consented — this is where deployments stall.

Transcript が出るまでの遅延 / Transcript availability
------------------------------------------------------
会議終了と同時には出ない。数分かかることがある。会議終了イベントで即座に
取りに行くと、たいてい空で返る。待って取り直す前提で作ってある。

A transcript is not ready when the meeting ends; it can take minutes. Fetching
on the meeting-ended event usually returns nothing, so this polls.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .base import Adapter, AdapterError, action
from .teams_vtt import merge_consecutive, parse_vtt, speakers, to_text

logger = logging.getLogger("aipmo.adapters.teams")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"

# トークンの寿命ぎりぎりまで使うと、要求の途中で切れる。
# Renewing only at expiry risks a token dying mid-request.
TOKEN_MARGIN_SECONDS = 300


class GraphTransport:
    """HTTP の実行部分。テストで差し替えられるように切り出してある。

    Isolated so tests can substitute it: there is no way to reach a real tenant
    from a test suite, and an adapter that can only be exercised against live
    Teams is an adapter that goes untested.
    """

    def request(self, method: str, url: str, headers: dict[str, str],
                body: bytes | None = None, timeout: float = 60.0) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()


class TeamsAdapter(Adapter):
    name = "teams"

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        organiser_id: str | None = None,
        transport: GraphTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 3,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        # 会議の所有者。Transcript は所有者のパス配下にある。
        # Transcripts live under the organiser's path.
        self.organiser_id = organiser_id
        self.transport = transport or GraphTransport()
        self.clock = clock
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- 認証 / authentication ---------------------------------------------

    def _access_token(self) -> str:
        if self._token and self.clock() < self._token_expires_at:
            return self._token

        missing = [n for n, v in (("tenant_id", self.tenant_id),
                                  ("client_id", self.client_id),
                                  ("client_secret", self.client_secret)) if not v]
        if missing:
            raise AdapterError(
                f"teams: 設定が足りません / missing configuration: {', '.join(missing)}"
            )

        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("utf-8")

        status, _, payload = self.transport.request(
            "POST", f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/token",
            {"Content-Type": "application/x-www-form-urlencoded"}, body,
        )
        data = _decode(payload)

        if status != 200:
            raise AdapterError(
                f"teams: トークンを取得できませんでした / token request failed "
                f"({status}): {data.get('error_description') or data.get('error')}"
            )

        self._token = data["access_token"]
        self._token_expires_at = (
            self.clock() + int(data.get("expires_in", 3600)) - TOKEN_MARGIN_SECONDS
        )
        return self._token

    # -- Graph 呼び出し / Graph calls ---------------------------------------

    def _get(self, path: str, accept: str = "application/json") -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{GRAPH}{path}"

        for attempt in range(1, self.max_retries + 1):
            status, headers, payload = self.transport.request(
                "GET", url,
                {"Authorization": f"Bearer {self._access_token()}", "Accept": accept},
            )

            if status == 429 or 500 <= status < 600:
                # Graph は絞ったとき Retry-After を返す。自分で決めた間隔より
                # 相手の指示に従う方が、結局早く通る。
                # Graph sends Retry-After when it throttles; honouring it gets
                # through sooner than any interval chosen here.
                if attempt < self.max_retries:
                    wait = float(headers.get("Retry-After", 2 ** attempt))
                    logger.warning("teams: %s, retrying in %ss", status, wait)
                    time.sleep(min(wait, 60))
                    continue

            if status == 401:
                # 期限切れの可能性。取り直して一度だけやり直す。
                # Possibly expired; refresh once and retry.
                self._token = None
                if attempt < self.max_retries:
                    continue

            if accept.startswith("text/"):
                return status, payload.decode("utf-8", errors="replace")
            return status, _decode(payload)

        raise AdapterError(f"teams: {url} に到達できませんでした / gave up on {url}")

    def _require(self, status: int, data: Any, what: str) -> Any:
        if status == 403:
            raise AdapterError(
                f"teams: {what} を読む権限がありません (403) / not permitted.\n"
                f"  アプリケーション権限に加えて、テナント管理者による "
                f"アプリケーションアクセスポリシーの割り当てが要ります。\n"
                f"  Beyond the application permission, a tenant admin must grant "
                f"an application access policy:\n"
                f"    New-CsApplicationAccessPolicy -Identity <name> -AppIds {self.client_id}\n"
                f"    Grant-CsApplicationAccessPolicy -PolicyName <name> -Identity <user>"
            )
        if status == 404:
            raise AdapterError(f"teams: {what} が見つかりません / not found (404)")
        if status >= 400:
            detail = data.get("error", {}).get("message") if isinstance(data, dict) else data
            raise AdapterError(f"teams: {what} の取得に失敗 ({status}): {detail}")
        return data

    def health_check(self) -> bool:
        try:
            self._access_token()
            return True
        except Exception:
            return False

    # -- アクション / actions ------------------------------------------------

    @action()
    def find_meeting(self, join_url: str, organiser_id: str | None = None) -> dict[str, Any]:
        """参加 URL から会議を引く / look up a meeting by its join URL.

        カレンダーのイベントや通知が持っているのは参加 URL であって、
        会議 ID ではない。橋渡しがないと、その先に進めない。

        What a calendar event or a notification carries is the join URL, not a
        meeting id; without this bridge nothing downstream can start.
        """
        user = organiser_id or self.organiser_id
        if not user:
            raise AdapterError("teams: organiser_id が必要です / organiser_id is required")

        quoted = join_url.replace("'", "''")
        status, data = self._get(
            f"/users/{user}/onlineMeetings"
            f"?$filter=JoinWebUrl%20eq%20'{urllib.parse.quote(quoted, safe='')}'"
        )
        data = self._require(status, data, "会議 / meeting")

        items = data.get("value") or []
        if not items:
            raise AdapterError(
                f"teams: この参加 URL の会議が見つかりません / no meeting for that join URL"
            )
        meeting = items[0]
        return {
            "meeting_id": meeting.get("id"),
            "subject": meeting.get("subject"),
            "start": meeting.get("startDateTime"),
            "end": meeting.get("endDateTime"),
            "organiser_id": user,
        }

    @action()
    def list_transcripts(self, meeting_id: str,
                         organiser_id: str | None = None) -> dict[str, Any]:
        """会議に紐づく Transcript の一覧 / transcripts attached to a meeting."""
        user = organiser_id or self.organiser_id
        if not user:
            raise AdapterError("teams: organiser_id が必要です / organiser_id is required")

        status, data = self._get(
            f"/users/{user}/onlineMeetings/{meeting_id}/transcripts")
        data = self._require(status, data, "Transcript 一覧 / transcript list")

        items = [
            {"id": t.get("id"), "created": t.get("createdDateTime")}
            for t in (data.get("value") or [])
        ]
        return {"items": items, "count": len(items)}

    @action()
    def get_transcript(
        self,
        meeting_id: str,
        organiser_id: str | None = None,
        wait_seconds: int = 0,
        poll_interval: int = 30,
        with_timestamps: bool = False,
    ) -> dict[str, Any]:
        """会議の Transcript を平文で取得する / fetch a transcript as plain text.

        wait_seconds を指定すると、出るまで待つ。会議終了と同時には出ないため。
        With wait_seconds it polls: a transcript is not ready when the meeting
        ends.
        """
        user = organiser_id or self.organiser_id
        deadline = time.monotonic() + max(0, wait_seconds)

        while True:
            listing = self.list_transcripts(meeting_id, organiser_id=user)
            if listing["items"]:
                break
            if time.monotonic() >= deadline:
                raise AdapterError(
                    "teams: Transcript がまだありません "
                    "/ no transcript yet.\n"
                    "  会議終了から数分かかることがあります。wait_seconds を"
                    "指定して待つか、後で実行してください。\n"
                    "  It can take minutes after a meeting ends; set "
                    "wait_seconds or run this later."
                )
            time.sleep(min(poll_interval, max(1, int(deadline - time.monotonic()))))

        # 複数ある場合は最新を採る。会議を録り直した場合、
        # 古い方を議事録にすると内容が食い違う。
        # Take the newest: if a meeting was re-recorded, minutes built from the
        # older transcript would contradict what people remember.
        latest = sorted(listing["items"], key=lambda t: t["created"] or "")[-1]

        status, content = self._get(
            f"/users/{user}/onlineMeetings/{meeting_id}"
            f"/transcripts/{latest['id']}/content?$format=text/vtt",
            accept="text/vtt",
        )
        if status >= 400:
            self._require(status, content, "Transcript 本文 / transcript content")

        utterances = merge_consecutive(parse_vtt(content))
        participants = speakers(utterances)

        return {
            "meeting_id": meeting_id,
            "transcript_id": latest["id"],
            "created": latest["created"],
            "text": to_text(utterances, with_timestamps=with_timestamps),
            "participants": participants,
            "utterance_count": len(utterances),
        }

    @action()
    def upcoming_meetings(self, organiser_id: str | None = None,
                          days: int = 1) -> dict[str, Any]:
        """予定表を引く / list calendar entries.

        トリガーの材料になる。終わった会議を拾って処理を回すには、
        まず「どの会議があったか」が要る。参加 URL もここから取れるので、
        find_meeting に渡して会議 ID に変換できる。

        Material for triggers: acting on a finished meeting starts with knowing
        which meetings there were. The join URL comes from here too, and feeds
        find_meeting to obtain the meeting id.
        """
        from datetime import datetime, timedelta, timezone

        user = organiser_id or self.organiser_id
        if not user:
            raise AdapterError("teams: organiser_id が必要です / organiser_id is required")

        now = datetime.now(timezone.utc)
        window = urllib.parse.urlencode({
            "startDateTime": now.isoformat(),
            "endDateTime": (now + timedelta(days=days)).isoformat(),
        })

        status, data = self._get(f"/users/{user}/calendarView?{window}")
        payload = self._require(status, data, "予定表 / calendar")

        items = [
            {
                "id": event.get("id"),
                "subject": event.get("subject"),
                "start": (event.get("start") or {}).get("dateTime"),
                "end": (event.get("end") or {}).get("dateTime"),
                "join_url": (event.get("onlineMeeting") or {}).get("joinUrl"),
            }
            for event in (payload.get("value") or [])
        ]
        return {"items": items, "count": len(items)}


def _decode(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw": payload[:500].decode("utf-8", errors="replace")}
