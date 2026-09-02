"""ポートフォリオ横断 Risk/Forecast 採点のテスト。

決定論的な採点なので、数値がどう動くかと、WBS と期限超過結果の対応が
崩れないことを見る。文面の要約（LLM 側）はここでは扱わない。

Deterministic scoring, so what's checked here is how the numbers move and
that a WBS stays correctly paired with its overdue-lookup result. The
narrative (handed to the LLM) is out of scope for this file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aipmo.adapters.base import Adapter, AdapterRegistry, action
from aipmo.adapters.mock import MockSlackAdapter
from aipmo.dsl import loader
from aipmo.engine.runner import Engine, PromptLibrary
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.portfolio import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    WbsSnapshot,
    assess_portfolio,
    assess_wbs,
    portfolio_risk_forecast,
)

ROOT = Path(__file__).resolve().parents[1]


def test_no_signals_scores_low():
    result = assess_wbs(WbsSnapshot(name="A"))
    assert result.level == RISK_LOW
    assert result.score == 0
    assert result.reasons == []


def test_many_overdue_and_close_deadline_scores_high():
    result = assess_wbs(WbsSnapshot(name="A", overdue_count=8, days_remaining=2))
    assert result.level == RISK_HIGH
    assert any("期限超過" in r for r in result.reasons)
    assert any("目標日" in r for r in result.reasons)


def test_past_target_date_is_always_likely_late():
    """スコアが低くても、目標日を過ぎているなら forecast は必ず遅延扱い。"""
    result = assess_wbs(WbsSnapshot(name="A", days_remaining=-1))
    assert result.forecast == "likely_late"


def test_score_increases_monotonically_with_overdue_count():
    scores = [
        assess_wbs(WbsSnapshot(name="A", overdue_count=n)).score
        for n in (0, 1, 3, 6, 20)
    ]
    assert scores == sorted(scores)


def test_blocked_count_alone_can_raise_the_level():
    clear = assess_wbs(WbsSnapshot(name="A"))
    blocked = assess_wbs(WbsSnapshot(name="A", blocked_count=5))
    assert blocked.score > clear.score


def test_score_never_exceeds_100():
    result = assess_wbs(WbsSnapshot(name="A", overdue_count=999, days_remaining=-999,
                                    blocked_count=999))
    assert result.score == 100


# --- ポートフォリオ全体 / the portfolio rollup ------------------------------

def test_portfolio_ranks_riskiest_first():
    summary = assess_portfolio([
        WbsSnapshot(name="calm"),
        WbsSnapshot(name="fire", overdue_count=10, days_remaining=1),
        WbsSnapshot(name="warm", overdue_count=2),
    ])
    assert [a["wbs"] for a in summary["assessed"]] == ["fire", "warm", "calm"]


def test_portfolio_at_risk_excludes_low_level():
    summary = assess_portfolio([
        WbsSnapshot(name="calm"),
        WbsSnapshot(name="fire", overdue_count=10, days_remaining=1),
    ])
    assert [a["wbs"] for a in summary["at_risk"]] == ["fire"]
    assert summary["at_risk_count"] == 1
    assert summary["total_count"] == 2


def test_portfolio_overall_level_is_the_worst_present():
    summary = assess_portfolio([
        WbsSnapshot(name="calm"),
        WbsSnapshot(name="warm", overdue_count=3),
    ])
    assert summary["overall_level"] == RISK_MEDIUM


def test_empty_portfolio_is_low_with_no_at_risk():
    summary = assess_portfolio([])
    assert summary["overall_level"] == RISK_LOW
    assert summary["at_risk"] == []
    assert summary["total_count"] == 0


# --- transform 入口: WBS と期限超過結果の対応 -------------------------------
# --- the transform entry point: pairing WBS with overdue-lookup results ---

def test_portfolio_risk_forecast_pairs_wbs_with_overdue_results_in_order():
    wbs = [{"name": "A", "days_remaining": 10}, {"name": "B", "days_remaining": 5}]
    overdue = [{"count": 1}, {"count": 9}]

    result = portfolio_risk_forecast(wbs, overdue)

    by_name = {a["wbs"]: a for a in result["assessed"]}
    assert by_name["A"]["overdue_count"] == 1
    assert by_name["B"]["overdue_count"] == 9


def test_portfolio_risk_forecast_survives_a_failed_lookup_without_misattribution():
    """途中の1件が失敗しても、後続の結果が別の WBS に付かないこと。

    for_each の `results` は失敗した要素を飛ばして詰まるので、素朴に
    位置で対応づけると2番目の overdue 結果が3番目の WBS に付いてしまう。
    """
    wbs = [
        {"name": "A", "days_remaining": 10},
        {"name": "B", "days_remaining": 10},
        {"name": "C", "days_remaining": 10},
    ]
    # B (index 1) の取得が失敗した想定。results には A・C の分だけが残る。
    overdue = [{"count": 1}, {"count": 9}]
    overdue_errors = [{"index": 1, "error": "boom"}]

    result = portfolio_risk_forecast(wbs, overdue, overdue_errors)

    by_name = {a["wbs"]: a for a in result["assessed"]}
    assert by_name["A"]["overdue_count"] == 1
    assert by_name["C"]["overdue_count"] == 9
    assert by_name["B"]["overdue_count"] == 0
    assert by_name["B"]["note"] is not None


def test_portfolio_risk_forecast_falls_back_to_project_key_when_name_is_missing():
    result = portfolio_risk_forecast([{"project": "PROJ"}], [{"count": 0}])
    assert result["assessed"][0]["wbs"] == "PROJ"


# --- テンプレートを通しての実行 / running through the template -------------

class FakePerProjectJira(Adapter):
    """MockJiraAdapter と違い、project ごとに件数を出し分ける。

    ポートフォリオが複数 WBS で本当に違う結果を返すことを確かめたいので、
    共有の mock (project を無視する) では足りない。
    """

    name = "jira"

    def __init__(self, overdue_by_project: dict[str, int], **config: Any) -> None:
        super().__init__(**config)
        self.overdue_by_project = overdue_by_project

    @action()
    def find_overdue(self, project: str, as_of: str | None = None) -> dict[str, Any]:
        count = self.overdue_by_project.get(project, 0)
        return {"items": [{"key": f"{project}-{i}"} for i in range(count)],
                "count": count}


def test_the_template_produces_a_ranked_at_risk_report():
    template = loader.load_file(ROOT / "templates/examples/portfolio_risk_forecast.yaml")

    adapters = AdapterRegistry()
    adapters.register(FakePerProjectJira({"CORE": 8, "PORTAL": 0}))
    slack = MockSlackAdapter()
    adapters.register(slack)
    llms = LLMRegistry()
    llms.register("default", EchoProvider(canned=(
        '{"headline": "1件が危険", '
        '"items": [{"wbs": "基幹システム刷新", "summary": "期限超過8件", '
        '"suggestion": "確認する"}]}'
    )))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(template)

    assert ctx.results["risk"].output["at_risk_count"] == 1
    assert ctx.results["risk"].output["assessed"][0]["wbs"] == "基幹システム刷新"
    assert slack.posted, "危険な WBS があるので通知が飛ぶはず"
    assert "1" in slack.posted[0]["text"]


def test_the_template_sends_nothing_when_the_whole_portfolio_is_calm():
    template = loader.load_file(ROOT / "templates/examples/portfolio_risk_forecast.yaml")

    adapters = AdapterRegistry()
    adapters.register(FakePerProjectJira({"CORE": 0, "PORTAL": 0}))
    slack = MockSlackAdapter()
    adapters.register(slack)

    ctx = Engine(adapters, LLMRegistry()).run(template)

    assert ctx.results["risk"].output["at_risk_count"] == 0
    assert ctx.results["notify"].status == "skipped"
    assert slack.posted == []
