"""複数 WBS・ポートフォリオ横断の Risk/Forecast 採点。

1つの WBS（Jira プロジェクトなど、進め方はチームごとに違ってよい）の
遅延兆候を、数えれば決まる範囲だけで点数化する。言語モデルには頼らない
— [aipmo/knowledge.py](aipmo/knowledge.py) の公開可能性スコアと同じ理由で、
同じ入力から毎回同じ結論が出ることが、後から「なぜこの順位か」を
説明できることの前提だから。

ポートフォリオ全体の状況は、この WBS 単位の点数を積み上げるだけで作る。
文面の要約・提案だけを言語モデルに任せる（テンプレート側の prompt を参照）。

Deterministic Risk/Forecast scoring across several WBS (Work Breakdown
Structures — Jira projects here, though teams may run them differently).
One WBS's delay signal is scored only from what is countable, with no
language model involved — the same reasoning as
[aipmo/knowledge.py](aipmo/knowledge.py)'s publicability score: a
reproducible number is what makes a later "why is this ranked here"
explainable. The portfolio-level picture is just these per-WBS scores
rolled up; only the narrative and suggestions are handed to a language
model (see the template's prompt).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

FORECAST_ON_TRACK = "on_track"
FORECAST_AT_RISK = "at_risk"
FORECAST_LIKELY_LATE = "likely_late"

# 期限超過の件数に応じた加点。件数そのものが分かりやすい兆候なので、
# 母数（全タスク数）を要求しない — 母数を取るには WBS ごとに追加の
# Jira 検索が要り、複数 WBS を跨ぐほど失敗点が増える。件数が多いほど
# 加点も大きい（「以上」で判定、しきい値は昇順）。
#
# Points added per overdue-issue count. Using the raw count (rather than a
# ratio) avoids needing each WBS's total task count, which would mean one
# more Jira query per WBS — more places to fail as the portfolio grows. More
# overdue means more points ("at least N", thresholds ascending).
def _overdue_points(count: int) -> int:
    if count >= 6:
        return 40
    if count >= 3:
        return 25
    if count >= 1:
        return 10
    return 0


# 目標日までの残日数に応じた加点。残りが少ない（マイナス、つまり
# すでに目標日を過ぎている場合を含む）ほど加点が大きい（「以下」で判定、
# しきい値は緩い方から厳しい方の順）。
#
# Points added per days remaining to the target date. Fewer days remaining —
# including negative, already past the target — means more points ("at most
# N", checked loosest threshold first).
def _deadline_points(days_remaining: int | None) -> int:
    if days_remaining is None:
        return 0
    if days_remaining > 14:
        return 0
    if days_remaining > 7:
        return 5
    if days_remaining > 3:
        return 15
    if days_remaining >= 1:
        return 30
    return 50


_LEVEL_THRESHOLDS = ((60, RISK_HIGH), (25, RISK_MEDIUM), (0, RISK_LOW))


@dataclass(frozen=True)
class WbsSnapshot:
    """1つの WBS の現状。テンプレートから渡される辞書と1対1。"""

    name: str
    overdue_count: int = 0
    days_remaining: int | None = None
    blocked_count: int = 0
    note: str | None = None


@dataclass(frozen=True)
class RiskAssessment:
    wbs: str
    score: int
    level: str
    forecast: str
    reasons: list[str] = field(default_factory=list)
    overdue_count: int = 0
    days_remaining: int | None = None
    blocked_count: int = 0
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "wbs": self.wbs,
            "score": self.score,
            "level": self.level,
            "forecast": self.forecast,
            "reasons": self.reasons,
            "overdue_count": self.overdue_count,
            "days_remaining": self.days_remaining,
            "blocked_count": self.blocked_count,
            "note": self.note,
        }


def _level_for(score: int) -> str:
    for threshold, level in _LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return RISK_LOW  # pragma: no cover - _LEVEL_THRESHOLDS always bottoms at 0


def assess_wbs(snapshot: WbsSnapshot) -> RiskAssessment:
    """1つの WBS を採点する / score a single WBS."""
    reasons: list[str] = []
    score = 0

    overdue_points = _overdue_points(snapshot.overdue_count)
    if overdue_points:
        score += overdue_points
        reasons.append(f"期限超過が {snapshot.overdue_count} 件")

    deadline_points = _deadline_points(snapshot.days_remaining)
    if deadline_points:
        score += deadline_points
        if snapshot.days_remaining is not None and snapshot.days_remaining < 0:
            reasons.append(f"目標日を {-snapshot.days_remaining} 日過ぎている")
        else:
            reasons.append(f"目標日まで残り {snapshot.days_remaining} 日")

    if snapshot.blocked_count:
        # ブロック中は期限超過より先に表れる遅延の予兆であることが多いので、
        # 少数でも無視しない。
        # A blocked count is often the earlier warning sign, ahead of an
        # actual overdue date, so even a small one is not ignored.
        blocked_points = 15 if snapshot.blocked_count >= 3 else 5
        score += blocked_points
        reasons.append(f"ブロック中が {snapshot.blocked_count} 件")

    score = min(100, score)
    level = _level_for(score)

    if snapshot.days_remaining is not None and snapshot.days_remaining < 0:
        forecast = FORECAST_LIKELY_LATE
    elif level == RISK_HIGH:
        forecast = FORECAST_LIKELY_LATE
    elif level == RISK_MEDIUM:
        forecast = FORECAST_AT_RISK
    else:
        forecast = FORECAST_ON_TRACK

    return RiskAssessment(
        wbs=snapshot.name, score=score, level=level, forecast=forecast,
        reasons=reasons, overdue_count=snapshot.overdue_count,
        days_remaining=snapshot.days_remaining,
        blocked_count=snapshot.blocked_count, note=snapshot.note,
    )


def assess_portfolio(snapshots: list[WbsSnapshot]) -> dict[str, Any]:
    """複数 WBS をまとめて採点し、順位付けする / score and rank several WBS."""
    assessed = [assess_wbs(s) for s in snapshots]
    # 危険度の高い順。同点は元の並び順を保つ（stable sort）。
    # Riskiest first; ties keep their original order (a stable sort).
    ranked = sorted(assessed, key=lambda a: a.score, reverse=True)
    at_risk = [a for a in ranked if a.level != RISK_LOW]

    counts = {RISK_HIGH: 0, RISK_MEDIUM: 0, RISK_LOW: 0}
    for a in assessed:
        counts[a.level] += 1

    if counts[RISK_HIGH]:
        overall = RISK_HIGH
    elif counts[RISK_MEDIUM]:
        overall = RISK_MEDIUM
    else:
        overall = RISK_LOW

    return {
        "assessed": [a.as_dict() for a in ranked],
        "at_risk": [a.as_dict() for a in at_risk],
        "total_count": len(assessed),
        "at_risk_count": len(at_risk),
        "high_risk_count": counts[RISK_HIGH],
        "medium_risk_count": counts[RISK_MEDIUM],
        "low_risk_count": counts[RISK_LOW],
        "overall_level": overall,
    }


def portfolio_risk_forecast(
    wbs: list[dict[str, Any]],
    overdue: list[dict[str, Any]] | None = None,
    overdue_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """テンプレートの `transform` ステップから呼ばれる入口。

    `wbs` は params のとおり（各要素に `name` と、目標日までの残日数
    `days_remaining` を含む）。`overdue` は `for_each` で `wbs` を1件ずつ
    `jira.find_overdue` した結果の配列で、途中で失敗した要素は含まれない
    （`overdue_errors` にその元の index が残る）。位置だけでは対応が
    取れないので、失敗した index を除いた残りの index に対して
    `overdue` を順番に割り当て直す。

    Called from the template's `transform` step. `wbs` matches params (each
    element carries `name` and `days_remaining` to its target date).
    `overdue` is the array from running `jira.find_overdue` on each `wbs`
    entry via `for_each`; an entry that failed is missing from it (its
    original index is recorded in `overdue_errors` instead). Position alone
    cannot correlate the two, so `overdue` is re-paired against the indices
    of `wbs` that did **not** fail, in order.
    """
    overdue = overdue or []
    failed_indices = {e.get("index") for e in (overdue_errors or [])}
    success_indices = [i for i in range(len(wbs)) if i not in failed_indices]

    overdue_by_index: dict[int, dict[str, Any]] = dict(zip(success_indices, overdue))

    snapshots = []
    for index, entry in enumerate(wbs):
        result = overdue_by_index.get(index)
        note = None if result is not None else (
            "期限超過の取得に失敗したため、その他の兆候のみで採点 "
            "/ overdue lookup failed; scored on the remaining signals only"
        )
        snapshots.append(WbsSnapshot(
            name=entry.get("name") or entry.get("project") or f"wbs[{index}]",
            overdue_count=(result or {}).get("count", 0),
            days_remaining=entry.get("days_remaining"),
            blocked_count=entry.get("blocked_count", 0),
            note=note,
        ))

    return assess_portfolio(snapshots)
