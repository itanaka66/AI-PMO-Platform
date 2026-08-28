"""実行コンテキスト。

テンプレート内から参照できる名前空間はここに集約する。
  params.*  実行時パラメータ
  trigger.* 起動イベントのペイロード
  run.*     実行メタ情報 (id, 開始時刻)
  steps.*   先行ステップの結果
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class StepResult:
    id: str
    status: str                    # success / skipped / failed
    output: Any = None
    error: str | None = None
    attempts: int = 0
    duration_ms: int = 0


@dataclass
class RunContext:
    template_name: str
    params: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    results: dict[str, StepResult] = field(default_factory=dict)

    def scope(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "trigger": self.trigger,
            "run": {
                "id": self.run_id,
                "template": self.template_name,
                "started_at": self.started_at.isoformat(),
                "date": self.started_at.date().isoformat(),
            },
            "steps": {
                step_id: {"output": r.output, "status": r.status}
                for step_id, r in self.results.items()
            },
        }

    def idempotency_key(self, step_id: str) -> str:
        """書き込み系アダプタに渡す既定のキー。

        run_id ではなくトリガー由来の識別子を優先する。
        同じ会議を再処理したときに重複作成させないため。
        """
        anchor = (
            self.trigger.get("meeting_id")
            or self.trigger.get("id")
            or self.params.get("meeting_id")
            or self.run_id
        )
        return f"{self.template_name}:{step_id}:{anchor}"
