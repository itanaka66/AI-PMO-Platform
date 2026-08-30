"""Slack 経由の人の承認 / human approval over Slack.

エージェントの承認ゲート（`AgentSpec.require_approval`、
`aipmo/engine/agent.py`）は、承認する側（`ApprovalCallback`）を渡さなければ
すべての書き込みを断る。`aipmo run` は対話端末があればその場で尋ねるが、
スケジューラや Web からの実行には対話端末が無い。ここはその代わりに、
Slack を承認の場にする。

しくみ / How it works
----------------------
  1. 提案されている書き込みの内容を、指定したチャンネルに投稿する。
  2. そのメッセージに付く絵文字リアクションを、一定間隔で確認する。
  3. 承認の絵文字（✅）が付けば許可、却下の絵文字（❌）が付けば拒否。
     どちらも付かないまま期限が来れば拒否する — 対話端末の承認と同じく、
     「反応が得られない = 通さない」。

Slack の Events API（Webhook）は使わない。ボットトークンだけで動く単純な
仕組みにするため、ポーリングにしている。反応が届くまで最大 `poll_seconds`
の遅れが出るが、公開エンドポイントや Slack App の Event Subscriptions の
用意は要らない。

The agent approval gate (`AgentSpec.require_approval` in
`aipmo/engine/agent.py`) refuses every write unless handed an approver
(`ApprovalCallback`). `aipmo run` asks at its own terminal when one is
attached, but a scheduler or web-triggered run has none. This gives it Slack
instead: post the proposed write to a channel, poll for an emoji reaction on
it, treat an approving reaction as yes, a rejecting one as no, and no
reaction by the deadline as no — the same "no response means refuse" rule the
terminal path already follows.

This deliberately polls rather than using Slack's Events API/webhooks,
trading up to `poll_seconds` of latency for needing nothing beyond the bot
token already in use elsewhere — no public endpoint or Slack App event
configuration to stand up.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .adapters.slack import SlackAdapter

logger = logging.getLogger("aipmo.approval")

APPROVE_EMOJI = "white_check_mark"   # ✅
DECLINE_EMOJI = "x"                  # ❌


@dataclass
class SlackApprover:
    """`run_agent` / `Engine` の `approve` にそのまま渡せる呼び出し可能体。

    A callable that can be passed directly as `run_agent`'s or `Engine`'s
    `approve`.
    """

    slack: SlackAdapter
    channel: str
    poll_seconds: float = 5.0
    timeout_seconds: float = 300.0
    # 承認できる人を絞りたい場合の Slack ユーザー ID。空なら誰の反応でもよい
    # — その場合、絞り込みはチャンネルそのものの参加者で行う前提になる。
    # Slack user ids allowed to approve, if restricting who can. Empty means
    # any reaction counts — the assumption then is that the channel's own
    # membership is the restriction.
    approver_ids: frozenset[str] = field(default_factory=frozenset)

    def __call__(self, tool: str, arguments: dict[str, Any]) -> bool:
        posted = self.slack.post_message(
            channel=self.channel, text=self._describe(tool, arguments),
        )
        channel, ts = posted["channel"], posted["ts"]

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            decision = self._check(channel, ts)
            if decision is not None:
                self._tell(channel, ts, decision)
                return decision
            time.sleep(self.poll_seconds)

        logger.warning("slack approval timed out after %.0fs: %s",
                       self.timeout_seconds, tool)
        self._tell(channel, ts, None)
        return False

    def _describe(self, tool: str, arguments: dict[str, Any]) -> str:
        body = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        return (
            f"承認が必要です / approval needed\n"
            f"道具 / tool: `{tool}`\n"
            f"引数 / arguments:\n```{body}```\n\n"
            f":{APPROVE_EMOJI}: で承認・:{DECLINE_EMOJI}: で却下 "
            f"/ react :{APPROVE_EMOJI}: to approve, :{DECLINE_EMOJI}: to decline"
        )

    def _check(self, channel: str, ts: str) -> bool | None:
        try:
            result = self.slack.get_reactions(channel=channel, ts=ts)
        except Exception:
            # 読み取りに失敗しても輪は止めない。次のポーリングで直ることが多い。
            # A failed read does not stop the loop; the next poll often recovers.
            logger.warning("slack approval: failed to read reactions", exc_info=True)
            return None

        for reaction in result["reactions"]:
            users = set(reaction.get("users") or [])
            if self.approver_ids and not (self.approver_ids & users):
                continue
            if reaction.get("name") == APPROVE_EMOJI:
                return True
            if reaction.get("name") == DECLINE_EMOJI:
                return False
        return None

    def _tell(self, channel: str, ts: str, decision: bool | None) -> None:
        if decision is True:
            text = "承認されました。実行します。 / approved — proceeding."
        elif decision is False:
            text = "却下されました。実行しません。 / declined — not proceeding."
        else:
            text = (
                f"{int(self.timeout_seconds)} 秒以内に反応が無かったため、"
                f"実行しません / no response within "
                f"{int(self.timeout_seconds)}s — not proceeding."
            )
        try:
            self.slack.reply_in_thread(channel=channel, thread_ts=ts, text=text)
        except Exception:
            # 決定そのものはもう出ている。確認メッセージが送れなくても、
            # 呼び出し元に返す結果に影響させない。
            # The decision has already been made; a failed confirmation
            # message must not change what gets returned.
            logger.warning("slack approval: failed to post the outcome", exc_info=True)
