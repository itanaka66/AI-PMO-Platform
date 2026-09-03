"""プロジェクト健康度の決定論的な採点 / deterministic project health scoring.

「このプロジェクトは大丈夫か」を5つの軸（スケジュール・リソース・
リスク・予算・ブロッカー）で採点する。[aipmo/portfolio.py](aipmo/portfolio.py)
の Risk/Forecast 採点、[aipmo/knowledge.py](aipmo/knowledge.py) の公開可能性
スコアと同じ理由で、言語モデルには頼らない — 同じ入力から毎回同じ結論が
出ることが、後から「なぜこの点数か」を説明できることの前提だから。

LLM は、この採点結果を材料として受け取り、文面の要約・推奨アクションだけを
書く（テンプレート側の prompt を参照）。点数を計算し直させることはない。

Scores "is this project okay?" across five dimensions — schedule,
resources, risks, budget, blockers. No language model involved, the same
reasoning as [aipmo/portfolio.py](aipmo/portfolio.py)'s Risk/Forecast score
and [aipmo/knowledge.py](aipmo/knowledge.py)'s publicability score: a
reproducible number is what makes a later "why this score" explainable.

The model only ever receives this result as material for a narrative and
recommendations (see the template's prompt) — never recomputes the score
itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUS_GREEN = "Green"
STATUS_YELLOW = "Yellow"
STATUS_RED = "Red"

# 各軸の重み。スケジュールを最も重く見る — 遅延はリソース・予算の両方に
# 波及するが、逆は必ずしも成り立たない。
#
# Per-dimension weights. Schedule weighs heaviest: a delay ripples into both
# resourcing and budget, but the reverse doesn't always hold.
_WEIGHTS = {
    "schedule": 0.30,
    "resources": 0.20,
    "risks": 0.25,
    "budget": 0.15,
    "blockers": 0.10,
}

_STATUS_THRESHOLDS = ((70, STATUS_GREEN), (40, STATUS_YELLOW), (0, STATUS_RED))

# 各軸の最悪値（下の _xxx_score 群）は、全軸が同時に最悪でも加重平均が
# 40 を下回るように選んである。PROJECT-DIGITAL-TWIN-ARCHITECTURE.md の
# 元の数値（各軸の最低が 50〜60）だと、加重平均の下限が約54.5 にしかならず、
# Red（40未満）に一度も到達しない — 採点の下限としきい値の食い違いを
# そのまま移すと、Red が定義上存在するのに実際には出ないという不整合を
# 引き継いでしまう。
#
# The worst-case value on every dimension (the _xxx_score functions below)
# is chosen so the weighted average, even with every dimension at its
# worst simultaneously, falls below 40. The source design doc's own
# figures (each dimension floors at 50-60) only bottom out around a
# weighted 54.5 — Red is never actually reachable. Porting those floors
# as-is would carry over a real inconsistency: a status that's defined but
# can never be assigned.


@dataclass(frozen=True)
class ScheduleState:
    variance_percent: float | None = None  # (forecast - planned) / planned * 100


@dataclass(frozen=True)
class ResourceState:
    utilization_percent: float | None = None  # 割り当て済み工数の合計 / allocated capacity, summed


@dataclass(frozen=True)
class RiskState:
    total_exposure: float = 0.0  # sum(probability * impact) across active risks


@dataclass(frozen=True)
class BudgetState:
    variance_percent: float | None = None  # (forecast - planned) / planned * 100


@dataclass(frozen=True)
class IssueState:
    critical_blocker_count: int = 0  # severity=Critical かつ is_blocker の件数


@dataclass(frozen=True)
class ProjectHealthAssessment:
    score: int
    status: str
    rule_scores: dict[str, int]
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "health_score": self.score,
            "health_status": self.status,
            "rule_scores": self.rule_scores,
            "reasons": self.reasons,
        }


def _schedule_score(state: ScheduleState) -> tuple[int, str | None]:
    variance = state.variance_percent
    if variance is None:
        return 100, None
    if variance > 20:
        return 40, f"スケジュール遅延が {variance:.0f}%"
    if variance > 10:
        return 70, f"スケジュール遅延が {variance:.0f}%"
    if variance > 5:
        return 90, f"スケジュール遅延が {variance:.0f}%"
    return 100, None


def _resource_score(state: ResourceState) -> tuple[int, str | None]:
    utilization = state.utilization_percent
    if utilization is None:
        return 100, None
    if utilization > 100:
        return 30, f"リソース利用率が {utilization:.0f}%（過負荷）"
    if utilization > 95:
        return 65, f"リソース利用率が {utilization:.0f}%"
    if utilization > 85:
        return 90, f"リソース利用率が {utilization:.0f}%"
    return 100, None


def _risk_score(state: RiskState) -> tuple[int, str | None]:
    exposure = state.total_exposure
    if exposure > 0.6:
        return 30, f"リスク露出度の合計が {exposure:.2f}"
    if exposure > 0.3:
        return 65, f"リスク露出度の合計が {exposure:.2f}"
    if exposure > 0.1:
        return 90, f"リスク露出度の合計が {exposure:.2f}"
    return 100, None


def _budget_score(state: BudgetState) -> tuple[int, str | None]:
    variance = state.variance_percent
    if variance is None:
        return 100, None
    magnitude = abs(variance)
    if magnitude > 15:
        return 40, f"予算逸脱が {variance:+.1f}%"
    if magnitude > 10:
        return 70, f"予算逸脱が {variance:+.1f}%"
    if magnitude > 5:
        return 90, f"予算逸脱が {variance:+.1f}%"
    return 100, None


def _blocker_score(state: IssueState) -> tuple[int, str | None]:
    count = state.critical_blocker_count
    if count > 3:
        return 30, f"クリティカルなブロッキング課題が {count} 件"
    if count > 1:
        return 65, f"クリティカルなブロッキング課題が {count} 件"
    if count > 0:
        return 90, f"クリティカルなブロッキング課題が {count} 件"
    return 100, None


def _status_for(score: int) -> str:
    for threshold, status in _STATUS_THRESHOLDS:
        if score >= threshold:
            return status
    return STATUS_RED  # pragma: no cover - _STATUS_THRESHOLDS always bottoms at 0


def assess_project_health(
    schedule: ScheduleState,
    resources: ResourceState,
    risks: RiskState,
    budget: BudgetState,
    issues: IssueState,
) -> ProjectHealthAssessment:
    """5軸のルールベース採点をまとめて行う / run all five rule-based dimensions."""
    dims: dict[str, tuple[int, str | None]] = {
        "schedule": _schedule_score(schedule),
        "resources": _resource_score(resources),
        "risks": _risk_score(risks),
        "budget": _budget_score(budget),
        "blockers": _blocker_score(issues),
    }

    rule_scores = {name: points for name, (points, _reason) in dims.items()}
    reasons = [reason for _points, reason in dims.values() if reason]

    total = sum(rule_scores[name] * weight for name, weight in _WEIGHTS.items())
    score = int(total)
    status = _status_for(score)

    return ProjectHealthAssessment(
        score=score, status=status, rule_scores=rule_scores, reasons=reasons,
    )


def project_health(
    schedule_variance_percent: float | None = None,
    resource_utilization_percent: float | None = None,
    risk_total_exposure: float = 0.0,
    budget_variance_percent: float | None = None,
    critical_blocker_count: int = 0,
) -> dict[str, Any]:
    """テンプレートの `transform` ステップから呼ばれる入口。

    データベースから読んだ集計済みの値（分散率・利用率・露出度合計・
    ブロッカー件数）を受け取り、採点結果を辞書で返す。個々のタスクや
    リスクの一覧をここで集計し直すことはしない — その集計は SQL 側の
    named query（`dt_get_budget` 等）が担う。

    Called from the template's `transform` step. Takes already-aggregated
    figures (variance percentages, utilization, total exposure, blocker
    count) read from the database, and returns the scoring result as a
    dict. It never re-aggregates a raw list of tasks or risks itself — that
    aggregation is the SQL side's job (the named queries such as
    `dt_get_budget`).
    """
    result = assess_project_health(
        schedule=ScheduleState(variance_percent=schedule_variance_percent),
        resources=ResourceState(utilization_percent=resource_utilization_percent),
        risks=RiskState(total_exposure=risk_total_exposure),
        budget=BudgetState(variance_percent=budget_variance_percent),
        issues=IssueState(critical_blocker_count=critical_blocker_count),
    )
    return result.as_dict()
