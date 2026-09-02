"""WBS 再計画 AI 専用アダプタ / adapter for the WBS-replanning agent.

`agent` ステップに `postgres.execute` をそのまま道具として渡すと、LLM は
`name` 引数で任意の named query を選べてしまう -- decide_wbs_proposal
（自己承認）や save_candidate（無関係な知見テーブルへの書き込み）まで
呼べることになり、Phase 1 で作った「WBS再計画AIは wbs_replan_proposals
にしか書けない」という境界が崩れる。

このアダプタは PostgresAdapter を内部に持つ合成方式（JiraAgileAdapter が
JiraAdapter を内部に持つのと同じパターン）で、公開するアクションを
propose（提案の記録）と pending_count（重複確認のための読み取り）の
2つだけに絞る。LLM がどう振る舞っても、この2つ以外には手が届かない。

tier は LLM の自己申告を受け取らない / tier is never taken from the model
---------------------------------------------------------------------------
`propose` の引数に tier を含めていない。直近の risk_forecast.forecast の
結果（あらかじめ save_forecast_snapshot で保存されたスナップショット）
から機械的に引く。ツール呼び出しの引数として渡させると、実際のドリフト
より軽い（あるいは重い）tier を LLM が自己申告できてしまう --
「集計はAIにやらせない」という既存方針を、ここでも徹底する。

A/B の複数案（option_label）/ multiple alternatives via option_label
---------------------------------------------------------------------------
`propose` は同じ実行の中で複数回呼べる。呼ぶたびに異なる `option_label`
（例: "reschedule" / "add_resources"）を渡せば、それぞれ別の提案として
承認待ちに並ぶ -- 冪等キー（source_key）に option_label を織り込むことで、
同じ wbs_id・tier でもラベルが違えば別行として共存する。省略した場合は
これまで通り単一案として扱われ、既存テンプレートの挙動は変わらない。

`propose` can be called more than once in the same run. A different
option_label each time ("reschedule" vs. "add_resources", say) makes each
call land as its own pending proposal -- the label is folded into the
idempotency key (source_key), so alternatives for the same wbs_id/tier
coexist as separate rows. Omitting it preserves the previous single-option
behaviour unchanged.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from .base import Adapter, AdapterError, action
from .postgres import PostgresAdapter


class WbsReplanAdapter(Adapter):
    name = "wbs_replan"

    def __init__(self, postgres: PostgresAdapter, **config: Any) -> None:
        super().__init__(**config)
        self.postgres = postgres

    def health_check(self) -> bool:
        return self.postgres.health_check()

    @action(writes=True)
    def propose(
        self,
        wbs_id: str,
        diff: dict[str, Any],
        rationale: str,
        confidence: float,
        assumptions: dict[str, Any] | None = None,
        option_label: str | None = None,
    ) -> dict[str, Any]:
        """WBS 再計画の提案を1件記録する。生WBSには一切触れない。

        tier はここでは受け取らない。直前に記録された予測スナップショット
        （risk_forecast.forecast の結果）から引く。無ければ、その旨のエラー
        で止める -- スナップショットの無い提案は根拠が無い。

        option_label を指定すると、同じ wbs_id・tier に対する複数の代替案
        (A/B) をそれぞれ独立した提案として残せる。省略時は単一案として、
        従来と同じ冪等キー（wbs_id:tier）を使う。
        """
        snapshot = self.postgres.query("latest_forecast_snapshot", {"wbs_id": wbs_id})
        row = snapshot["rows"][0] if snapshot["rows"] else None
        if row is None or row.get("tier") is None:
            raise AdapterError(
                f"wbs_replan: no forecast snapshot recorded for wbs_id={wbs_id!r}. "
                f"Run risk_forecast.forecast and save it via save_forecast_snapshot "
                f"before proposing a replan."
            )
        tier = row["tier"]

        idempotency_key = f"{wbs_id}:tier{tier}"
        if option_label:
            idempotency_key = f"{idempotency_key}:{option_label}"

        result = self.postgres.execute(
            "save_wbs_proposal",
            {
                "id": str(uuid4()),
                "wbs_version_from": wbs_id,
                "diff": diff,
                "rationale": rationale,
                "assumptions": assumptions or {},
                "tier": tier,
                "confidence": confidence,
                "option_label": option_label,
            },
            idempotency_key=idempotency_key,
        )
        proposed = bool(result["rows"])
        return {
            "proposed": proposed,
            "id": result["rows"][0]["id"] if proposed else None,
            "tier": tier,
            "option_label": option_label,
            "reason": None if proposed else (
                "no pending row was created or updated - a decided proposal "
                "for this wbs/tier/option already exists and is not overwritten"
            ),
        }

    @action()
    def pending_count(self, wbs_id: str | None = None) -> dict[str, Any]:
        """承認待ちの提案件数（読み取り専用）。wbs_id を指定すると絞り込む。

        A/B の複数案がある場合、これは行数（= 案の総数）を返す。
        「ドリフトに対する提案が既にあるか」を確認したいだけなら、
        件数の 0/非0 で十分 -- 何個あるかは classify_drift の判断には
        関係しない。
        """
        result = self.postgres.query("pending_wbs_proposals", {})
        rows = result["rows"]
        if wbs_id is not None:
            rows = [r for r in rows if r.get("wbs_version_from") == wbs_id]
        return {"count": len(rows)}
