"""テスト・デモ用のダミーアダプタ。

Step 2 以降で実装する jira / slack / teams と同じ形をしているので、
これで通るテンプレートは、実アダプタに差し替えてもそのまま動く。
"""
from __future__ import annotations

from typing import Any

from .base import Adapter, action


class MockTeamsAdapter(Adapter):
    name = "teams"

    def __init__(self, transcript: str = "", **config: Any) -> None:
        super().__init__(**config)
        self.transcript = transcript or (
            "田中: 認証基盤の移行、来週金曜までに設計レビューを終えたい。\n"
            "佐藤: API 側の互換対応が残っています。私が水曜までに一覧を出します。\n"
            "田中: では鈴木さんは負荷試験の環境準備をお願いします。期限は来週木曜。\n"
        )

    @action()
    def get_transcript(self, meeting_id: str) -> dict[str, Any]:
        return {
            "meeting_id": meeting_id,
            "text": self.transcript,
            "participants": ["田中", "佐藤", "鈴木"],
        }


class MockJiraAdapter(Adapter):
    name = "jira"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.created: list[dict[str, Any]] = []

    @action(writes=True)
    def create_issues(
        self, issues: list[dict[str, Any]], project: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        keys = []
        for issue in issues:
            number = len(self.created) + 1
            record = {**issue, "project": project, "key": f"{project}-{number}",
                      "idempotency_key": idempotency_key}
            self.created.append(record)
            keys.append(record["key"])
        return {"created": keys, "count": len(keys)}

    @action()
    def find_overdue(self, project: str, as_of: str | None = None) -> dict[str, Any]:
        overdue = [i for i in self.created if i.get("due_date") and i.get("status") != "Done"]
        return {"items": overdue, "count": len(overdue)}


class MockSlackAdapter(Adapter):
    name = "slack"

    def __init__(self, **config: Any) -> None:
        super().__init__(**config)
        self.posted: list[dict[str, Any]] = []

    @action(writes=True)
    def post_message(self, channel: str, text: str,
                     thread_ts: str | None = None) -> dict[str, Any]:
        message = {"channel": channel, "text": text, "thread_ts": thread_ts}
        self.posted.append(message)
        return {"ok": True, "ts": f"{len(self.posted)}.000000"}
