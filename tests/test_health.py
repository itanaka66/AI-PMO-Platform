"""プロジェクト健康度採点のテスト。

決定論的な採点なので、数値がどう動くかと、5軸の重み付けが仕様どおりに
なることを見る。LLM 側の文面はここでは扱わない。

Deterministic scoring, so what's checked here is how the numbers move and
that the five-dimension weighting matches spec. The narrative (handed to
the LLM) is out of scope for this file.
"""
from __future__ import annotations

from aipmo.health import (
    STATUS_GREEN,
    STATUS_RED,
    STATUS_YELLOW,
    BudgetState,
    IssueState,
    ResourceState,
    RiskState,
    ScheduleState,
    assess_project_health,
    project_health,
)


def _calm() -> dict:
    return dict(
        schedule=ScheduleState(),
        resources=ResourceState(),
        risks=RiskState(),
        budget=BudgetState(),
        issues=IssueState(),
    )


def test_no_signals_scores_perfectly_and_is_green():
    result = assess_project_health(**_calm())
    assert result.score == 100
    assert result.status == STATUS_GREEN
    assert result.reasons == []


def test_schedule_variance_reduces_the_schedule_dimension_only():
    calm = assess_project_health(**_calm())
    args = _calm()
    args["schedule"] = ScheduleState(variance_percent=25)
    late = assess_project_health(**args)

    assert late.rule_scores["schedule"] < calm.rule_scores["schedule"]
    assert late.rule_scores["resources"] == calm.rule_scores["resources"]
    assert late.rule_scores["budget"] == calm.rule_scores["budget"]
    assert any("スケジュール遅延" in r for r in late.reasons)


def test_resource_overallocation_is_worse_than_high_utilization():
    args_high = _calm()
    args_high["resources"] = ResourceState(utilization_percent=96)
    high = assess_project_health(**args_high)

    args_over = _calm()
    args_over["resources"] = ResourceState(utilization_percent=110)
    over = assess_project_health(**args_over)

    assert over.rule_scores["resources"] < high.rule_scores["resources"]


def test_risk_exposure_above_threshold_lowers_the_risk_dimension():
    args = _calm()
    args["risks"] = RiskState(total_exposure=0.65)
    result = assess_project_health(**args)

    assert result.rule_scores["risks"] == 30
    assert any("リスク露出度" in r for r in result.reasons)


def test_budget_variance_uses_absolute_magnitude_either_direction():
    """予算逸脱は超過でも下振れでも同じ扱い（絶対値で見る）。"""
    over = _calm()
    over["budget"] = BudgetState(variance_percent=20)
    under = _calm()
    under["budget"] = BudgetState(variance_percent=-20)

    assert (assess_project_health(**over).rule_scores["budget"]
            == assess_project_health(**under).rule_scores["budget"])


def test_critical_blockers_reduce_the_blocker_dimension():
    args = _calm()
    args["issues"] = IssueState(critical_blocker_count=4)
    result = assess_project_health(**args)

    assert result.rule_scores["blockers"] == 30
    assert any("ブロッキング課題" in r for r in result.reasons)


def test_schedule_is_weighted_more_heavily_than_blockers():
    """設計どおり、schedule (0.30) は blockers (0.10) より重い。"""
    schedule_bad = _calm()
    schedule_bad["schedule"] = ScheduleState(variance_percent=25)  # -> 40
    blockers_bad = _calm()
    blockers_bad["issues"] = IssueState(critical_blocker_count=4)  # -> 30

    schedule_result = assess_project_health(**schedule_bad)
    blockers_result = assess_project_health(**blockers_bad)

    # 同程度に軸を悪化させても、重い軸(schedule)の方が総合スコアへの
    # 影響が大きい。
    assert (100 - schedule_result.score) / 0.30 == (100 - 40)
    assert (100 - blockers_result.score) / 0.10 == (100 - 30)
    assert schedule_result.score < blockers_result.score


def test_status_boundaries_match_spec():
    # 70 以上 Green、40〜69 Yellow、40未満 Red。
    assert assess_project_health(**_calm()).status == STATUS_GREEN

    args_yellow = _calm()
    args_yellow["schedule"] = ScheduleState(variance_percent=25)   # -> 40
    args_yellow["resources"] = ResourceState(utilization_percent=110)  # -> 30
    args_yellow["risks"] = RiskState(total_exposure=0.65)          # -> 30
    result_yellow = assess_project_health(**args_yellow)
    assert 40 <= result_yellow.score < 70
    assert result_yellow.status == STATUS_YELLOW

    args_red = _calm()
    args_red["schedule"] = ScheduleState(variance_percent=25)
    args_red["resources"] = ResourceState(utilization_percent=110)
    args_red["risks"] = RiskState(total_exposure=0.7)
    args_red["budget"] = BudgetState(variance_percent=20)
    args_red["issues"] = IssueState(critical_blocker_count=5)
    assert assess_project_health(**args_red).status == STATUS_RED


def test_score_is_bounded_between_0_and_100():
    args = _calm()
    args["schedule"] = ScheduleState(variance_percent=999)
    args["resources"] = ResourceState(utilization_percent=999)
    args["risks"] = RiskState(total_exposure=999)
    args["budget"] = BudgetState(variance_percent=999)
    args["issues"] = IssueState(critical_blocker_count=999)
    result = assess_project_health(**args)

    assert 0 <= result.score <= 100


# --- transform 入口 / the transform entry point -----------------------------

def test_project_health_transform_returns_a_plain_dict():
    result = project_health(
        schedule_variance_percent=25,
        resource_utilization_percent=96,
        risk_total_exposure=0.4,
        budget_variance_percent=12,
        critical_blocker_count=2,
    )

    assert set(result) == {"health_score", "health_status", "rule_scores", "reasons"}
    assert isinstance(result["health_score"], int)
    assert isinstance(result["rule_scores"], dict)


def test_project_health_transform_defaults_to_a_calm_project():
    result = project_health()
    assert result["health_score"] == 100
    assert result["health_status"] == STATUS_GREEN
    assert result["reasons"] == []
