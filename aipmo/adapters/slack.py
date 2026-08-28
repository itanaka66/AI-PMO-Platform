"""Slack アダプタ / Slack adapter.

ボットトークン (xoxb-) で Web API を叩く。

最大の落とし穴 / The trap that matters
--------------------------------------
**Slack は失敗しても HTTP 200 を返します。** 成否は本文の `ok` にあります。

    {"ok": false, "error": "channel_not_found"}

ステータスコードだけを見る実装は、通知が1件も届いていないのに
「全部成功」と報告し続けます。障害としては最悪の部類です。

**Slack answers 200 even when the call failed**; success lives in the body's
`ok` field. Code that checks only the status code will report every send as
successful while not a single message arrives — among the worst failure shapes
there is, because nothing looks wrong.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Adapter, AdapterError, action

logger = logging.getLogger("aipmo.adapters.slack")

API = "https://slack.com/api"

# 再送しても実らないもの。待つだけ無駄なので、すぐ諦める。
# Retrying these never helps, so they fail immediately rather than sleeping.
PERMANENT_ERRORS = {
    "invalid_auth", "account_inactive", "token_revoked", "not_authed",
    "channel_not_found", "not_in_channel", "is_archived",
    "msg_too_long", "no_text", "restricted_action",
}

HINTS = {
    "not_in_channel": "ボットをチャンネルに招待してください / invite the bot to the channel: /invite @yourbot",
    "channel_not_found": "チャンネル名か ID を確認してください。プライベートチャンネルはボットの参加が要ります / private channels require the bot to be a member",
    "invalid_auth": "トークンを確認してください / check the token. xoxb- で始まります",
    "missing_scope": "ボットに chat:write スコープを付けてください / grant the chat:write scope",
    "msg_too_long": "本文が長すぎます。要約するか分割してください / shorten or split the message",
}


class SlackAdapter(Adapter):
    name = "slack"

    def __init__(self, token: str | None = None, default_channel: str | None = None,
                 transport: Any = None, max_retries: int = 3,
                 timeout: float = 30.0, **config: Any) -> None:
        super().__init__(**config)
        self.token = token
        self.default_channel = default_channel
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport

    # -- HTTP ---------------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise AdapterError("slack: token が設定されていません / token is not configured")

        url = f"{API}/{method}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        for attempt in range(1, self.max_retries + 1):
            status, headers_out, raw = self._send(url, headers, body)

            if status == 429:
                # Slack は Retry-After を返す。無視して押すと絞りが長引く。
                # Honour Retry-After; pushing through extends the throttling.
                wait = float((headers_out or {}).get("Retry-After") or 2 ** attempt)
                if attempt == self.max_retries:
                    raise AdapterError(
                        f"slack: 送信制限に達しました / rate limited after "
                        f"{self.max_retries} attempts"
                    )
                logger.warning("slack: rate limited, waiting %.0fs", wait)
                time.sleep(min(wait, 60))
                continue

            data = _decode(raw)

            # ここが本体。200 でも ok が false なら失敗。
            # The crux: a 200 with ok=false is a failure.
            if not data.get("ok"):
                error = data.get("error", "unknown_error")
                hint = HINTS.get(error, "")
                message = f"slack: {method} が失敗 / failed: {error}"
                if hint:
                    message += f"\n  {hint}"

                if error in PERMANENT_ERRORS or attempt == self.max_retries:
                    raise AdapterError(message)
                logger.warning("%s (retrying)", message)
                time.sleep(2 ** attempt)
                continue

            return data

        raise AdapterError(f"slack: {method} に失敗しました / failed")

    def _send(self, url: str, headers: dict[str, str],
              body: bytes) -> tuple[int, dict[str, str], bytes]:
        if self._transport is not None:
            return self._transport.request("POST", url, headers, body, self.timeout)

        request = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except urllib.error.URLError as exc:
            raise AdapterError(f"slack: 接続できません / cannot reach Slack: {exc}")

    def health_check(self) -> bool:
        try:
            self._call("auth.test", {})
            return True
        except Exception:
            return False

    # -- アクション / actions -----------------------------------------------

    @action(writes=True)
    def post_message(self, text: str, channel: str | None = None,
                     thread_ts: str | None = None,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        """メッセージを送る / send a message.

        Slack に冪等キーの仕組みはありません。同じ内容を二度送らない保証は
        ここでは作れないので、呼び出し側の when や、テンプレート全体の
        冪等性で担保してください。キーは記録のために付けるだけです。

        Slack has no idempotency mechanism, and one cannot be built here. Guard
        against double sends with the caller's `when` or the template's own
        idempotency; the key is recorded for traceability only.
        """
        target = channel or self.default_channel
        if not target:
            raise AdapterError("slack: channel が必要です / channel is required")

        payload: dict[str, Any] = {"channel": target, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if idempotency_key:
            payload["metadata"] = {
                "event_type": "aipmo_run",
                "event_payload": {"idempotency_key": idempotency_key},
            }

        data = self._call("chat.postMessage", payload)
        return {
            "ok": True,
            "ts": data.get("ts"),
            "channel": data.get("channel"),
            # スレッドに続けたいときのために返す / for replying in-thread later
            "thread_ts": data.get("ts"),
        }

    @action(writes=True)
    def reply_in_thread(self, thread_ts: str, text: str,
                        channel: str | None = None) -> dict[str, Any]:
        return self.post_message(text=text, channel=channel, thread_ts=thread_ts)

    @action()
    def find_user(self, email: str) -> dict[str, Any]:
        """メールアドレスから利用者を引く / look up a user by email.

        メンションには ID が要る。氏名では飛びません。
        A mention needs the id; a display name does not notify anyone.
        """
        query = urllib.parse.urlencode({"email": email})
        data = self._call(f"users.lookupByEmail?{query}", {})
        user = data.get("user") or {}
        return {
            "id": user.get("id"),
            "name": user.get("real_name") or user.get("name"),
            "mention": f"<@{user.get('id')}>" if user.get("id") else None,
        }

    @action()
    def list_channels(self, limit: int = 100) -> dict[str, Any]:
        data = self._call(f"conversations.list?limit={limit}", {})
        items = [
            {"id": c.get("id"), "name": c.get("name"),
             "is_member": c.get("is_member")}
            for c in (data.get("channels") or [])
        ]
        return {"items": items, "count": len(items)}


def _decode(payload: bytes | str | None) -> dict[str, Any]:
    if payload is None:
        return {"ok": False, "error": "empty_response"}
    text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_json", "raw": text[:300]}
    return data if isinstance(data, dict) else {"ok": False, "error": "unexpected_shape"}
