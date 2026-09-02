"""Risk/Forecast アダプタのテスト / risk_forecast adapter tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.postgres import PostgresAdapter
from aipmo.adapters.risk_forecast import RiskForecastAdapter

ROOT = Path(__file__).resolve().parents[1]
REAL_QUERIES = yaml.safe_load((ROOT / "queries.yaml").read_text(encoding="utf-8"))


def _tasks(*, done: int, remaining_effort: list) -> list:
    items = [{"key": f"D-{i}", "done": True, "effort": 1} for i in range(done)]
    items += [
        {"key": f"R-{i}", "done": False, "effort": effort}
        for i, effort in enumerate(remaining_effort)
    ]
    return items


def test_forecast_on_schedule():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=5, remaining_effort=[2, 2])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 2.0,
        "deadline": "2026-01-10", "as_of": "2026-01-08",
    })

    assert result["remaining_effort"] == 4
    assert result["days_to_deadline"] == 2
    assert result["projected_days_needed"] == 2.0
    assert result["projected_completion"] == "2026-01-10"
    assert result["drift_days"] == 0.0


def test_forecast_behind_schedule():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=0, remaining_effort=[10])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 1.0,
        "deadline": "2026-01-05", "as_of": "2026-01-01",
    })

    assert result["projected_days_needed"] == 10.0
    assert result["days_to_deadline"] == 4
    assert result["drift_days"] == 6.0


def test_forecast_ahead_of_schedule():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=8, remaining_effort=[1])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 5.0,
        "deadline": "2026-01-10", "as_of": "2026-01-01",
    })

    assert result["drift_days"] < 0


def test_forecast_zero_velocity_cannot_forecast():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=0, remaining_effort=[5])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 0.0,
        "deadline": "2026-01-10", "as_of": "2026-01-01",
    })

    assert result["projected_days_needed"] is None
    assert result["projected_completion"] is None
    assert result["drift_days"] is None


def test_forecast_unestimated_excluded_from_remaining_effort():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=0, remaining_effort=[3, None, None])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 1.0,
        "deadline": "2026-01-10", "as_of": "2026-01-01",
    })

    assert result["remaining_effort"] == 3
    assert set(result["unestimated"]) == {"R-1", "R-2"}


def test_forecast_all_done_has_zero_remaining_not_none():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=3, remaining_effort=[])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 1.0,
        "deadline": "2026-01-10", "as_of": "2026-01-01",
    })

    assert result["remaining_effort"] == 0.0
    assert result["percent_done"] == 100


def test_forecast_percent_done_rounds():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=1, remaining_effort=[1, 1])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 1.0,
        "deadline": "2026-01-10", "as_of": "2026-01-01",
    })

    assert result["percent_done"] == 33


def test_forecast_projected_completion_rounds_up_half_day():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=0, remaining_effort=[0.5])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 1.0,
        "deadline": "2026-01-10", "as_of": "2026-01-01",
    })

    assert result["projected_days_needed"] == 0.5
    assert result["projected_completion"] == "2026-01-02"


def test_forecast_accepts_datetime_style_dates():
    adapter = RiskForecastAdapter()
    tasks = _tasks(done=0, remaining_effort=[1])

    result = adapter.invoke("forecast", {
        "tasks": tasks, "velocity_per_day": 1.0,
        "deadline": "2026-01-10T00:00:00Z", "as_of": "2026-01-01T09:30:00Z",
    })

    assert result["deadline"] == "2026-01-10"
    assert result["as_of"] == "2026-01-01"


def test_classify_drift_none_is_unforecastable():
    adapter = RiskForecastAdapter()
    result = adapter.invoke("classify_drift", {"drift_days": None})
    assert result["tier"] is None
    assert result["should_propose"] is False


def test_classify_drift_tier1_never_proposes():
    adapter = RiskForecastAdapter(tier2_at=3.0, tier3_at=10.0)
    result = adapter.invoke("classify_drift", {"drift_days": 1.0})
    assert result["tier"] == 1
    assert result["should_propose"] is False


def test_classify_drift_tier2_boundary():
    adapter = RiskForecastAdapter(tier2_at=3.0, tier3_at=10.0)
    assert adapter.invoke("classify_drift", {"drift_days": 2.9})["tier"] == 1
    assert adapter.invoke("classify_drift", {"drift_days": 3.0})["tier"] == 2


def test_classify_drift_tier3_boundary():
    adapter = RiskForecastAdapter(tier2_at=3.0, tier3_at=10.0)
    assert adapter.invoke("classify_drift", {"drift_days": 9.9})["tier"] == 2
    assert adapter.invoke("classify_drift", {"drift_days": 10.0})["tier"] == 3


def test_classify_drift_newly_detected_above_threshold_proposes():
    adapter = RiskForecastAdapter(detect_at=3.0, clear_below=1.0)
    result = adapter.invoke("classify_drift", {
        "drift_days": 4.0, "previous_drift_days": None,
    })
    assert result["should_propose"] is True
    assert "newly detected" in result["reason"]


def test_classify_drift_below_detect_threshold_does_not_propose():
    adapter = RiskForecastAdapter(tier2_at=1.5, detect_at=3.0, clear_below=1.0)
    result = adapter.invoke("classify_drift", {
        "drift_days": 2.0, "previous_drift_days": None,
    })
    assert result["tier"] == 2
    assert result["should_propose"] is False


def test_classify_drift_hysteresis_stays_flagged_between_thresholds():
    adapter = RiskForecastAdapter(tier2_at=1.5, detect_at=3.0, clear_below=1.0)

    result = adapter.invoke("classify_drift", {
        "drift_days": 2.0, "previous_drift_days": 3.5,
    })

    assert result["should_propose"] is True
    assert "ongoing" in result["reason"]


def test_classify_drift_hysteresis_clears_below_threshold():
    adapter = RiskForecastAdapter(tier2_at=0.4, detect_at=3.0, clear_below=1.0)

    result = adapter.invoke("classify_drift", {
        "drift_days": 0.6, "previous_drift_days": 3.5,
    })

    assert result["tier"] == 2
    assert result["should_propose"] is False
    assert "resolved" in result["reason"]


def test_classify_drift_already_pending_never_proposes_again():
    adapter = RiskForecastAdapter(detect_at=3.0, clear_below=1.0)
    result = adapter.invoke("classify_drift", {
        "drift_days": 8.0, "pending_count": 2,
    })
    assert result["should_propose"] is False
    assert "already pending" in result["reason"]


def test_invalid_thresholds_rejected_at_construction():
    with pytest.raises(ValueError, match="clear_below"):
        RiskForecastAdapter(detect_at=2.0, clear_below=2.0)


class FakeCursor:
    def __init__(self, log, rows, columns) -> None:
        self._log = log
        self._rows = rows
        self.description = [(c,) for c in columns] if columns else None
        self.rowcount = len(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, values=None):
        self._log.append((sql, list(values or [])))

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, rows=None, columns=None) -> None:
        self.log: list = []
        self.rows = rows or []
        self.columns = columns or []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.log, self.rows, self.columns)

    def commit(self):
        self.commits += 1


def test_latest_forecast_snapshot_binds_wbs_and_tenant():
    connection = FakeConnection(rows=[(6.0, 2, {"x": 1}, "2026-01-01")],
                                 columns=["drift_days", "tier", "forecast", "recorded_at"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    result = adapter.invoke("query", {"name": "latest_forecast_snapshot",
                                       "params": {"wbs_id": "wbs-1"}})

    sql, values = connection.log[0]
    assert "acme" in values and "wbs-1" in values
    assert result["rows"][0]["tier"] == 2


def test_save_forecast_snapshot_upserts_on_tenant_and_wbs():
    connection = FakeConnection(rows=[], columns=[])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    adapter.invoke("execute", {
        "name": "save_forecast_snapshot",
        "params": {"wbs_id": "wbs-1", "drift_days": 6.0, "tier": 2,
                   "forecast": {"drift_days": 6.0}},
    })

    sql, values = connection.log[0]
    assert "ON CONFLICT (tenant, wbs_id) DO UPDATE" in sql
    assert "acme" in values and "wbs-1" in values
    assert connection.commits == 1


# --- テンプレートの実行 (wbs_risk_forecast.yaml) ------------------------------
#
# 単体テストの通過だけでは DSL 式の構文ミス（when の複合条件・配列添字の
# 書式など）を検知できないため、実際にエンジンで最初から最後まで動かす。
#
# Passing unit tests alone would not catch DSL expression syntax mistakes
# (compound `when` conditions, array-index formatting) — this actually
# drives the engine through the shipped template end to end.

class _RoutingCursor:
    """SQL の内容を見て、テンプレートが期待する形の行を返す。実際の
    PostgreSQL の代わりに、この経路のテンプレート実行を検証するためだけの
    もの。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._rows: list[tuple] = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, values: list | None = None) -> None:
        self.conn.log.append((sql, list(values or [])))
        if "wbs_forecast_snapshots" in sql and "UNION ALL" in sql:
            self._rows = [(None, None, None, None)]
            self.description = [("drift_days",), ("tier",), ("forecast",), ("recorded_at",)]
        elif "wbs_replan_proposals" in sql:
            self._rows = []
            self.description = [("id",), ("pattern_name",)]
        elif sql.strip().upper().startswith(("INSERT", "UPDATE")):
            self._rows = [("run-1",)]
            self.description = None
        else:
            self._rows = []
            self.description = None
        self.rowcount = len(self._rows)

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RoutingConnection:
    def __init__(self) -> None:
        self.log: list[tuple[str, list]] = []

    def cursor(self):
        return _RoutingCursor(self)

    def commit(self) -> None:
        pass


def _build_engine(connection, **risk_forecast_kwargs):
    import yaml as _yaml

    from aipmo.adapters.mock import MockSlackAdapter
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry

    adapters = AdapterRegistry()
    adapters.register(PostgresAdapter(queries=REAL_QUERIES, tenant="acme",
                                       connection=connection))
    adapters.register(RiskForecastAdapter(**risk_forecast_kwargs))
    adapters.register(MockSlackAdapter())

    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    return Engine(adapters, llms)


def _load_wbs_risk_forecast_template():
    from aipmo.dsl import loader

    path = ROOT / "templates" / "examples" / "wbs_risk_forecast.yaml"
    return loader.load_file(path)


def test_template_proposes_and_notifies_on_severe_drift():
    engine = _build_engine(_RoutingConnection(), tier2_at=1.0, tier3_at=5.0,
                            detect_at=1.0, clear_below=0.3)
    template = _load_wbs_risk_forecast_template()

    tasks = [
        {"key": "T1", "done": True, "effort": 2},
        {"key": "T2", "done": False, "effort": 10},
    ]
    ctx = engine.run(template, params={
        "wbs_id": "wbs-test", "deadline": "2026-01-05", "velocity_per_day": 1.0,
        "tasks": tasks,
    })

    assert all(r.status in ("success", "skipped") for r in ctx.results.values())
    assert ctx.results["classify"].output["tier"] == 3
    assert ctx.results["classify"].output["should_propose"] is True
    assert ctx.results["propose"].status == "success"
    assert ctx.results["notify_critical"].status == "success"


def test_template_skips_propose_and_notify_when_on_schedule():
    engine = _build_engine(_RoutingConnection())  # 既定閾値
    template = _load_wbs_risk_forecast_template()

    tasks = [
        {"key": "T1", "done": True, "effort": 8},
        {"key": "T2", "done": False, "effort": 1},
    ]
    ctx = engine.run(template, params={
        "wbs_id": "wbs-ok", "deadline": "2026-12-31", "velocity_per_day": 2.0,
        "tasks": tasks,
    })

    assert ctx.results["classify"].output["tier"] == 1
    assert ctx.results["propose"].status == "skipped"
    assert ctx.results["notify_critical"].status == "skipped"
    # snapshot は提案の有無に関わらず毎回記録する
    assert ctx.results["snapshot"].status == "success"


# --- dependency_impact() -----------------------------------------------

def test_dependency_impact_no_dependencies_is_trivial():
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": False, "effort": 3, "depends_on": []},
        {"key": "B", "done": False, "effort": 5, "depends_on": []},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["blocked"] == []
    assert result["cycles"] == []
    assert result["critical_path_effort"] == 5.0
    assert result["critical_path"] == ["B"]


def test_dependency_impact_serial_chain():
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": False, "effort": 3, "depends_on": []},
        {"key": "B", "done": False, "effort": 5, "depends_on": ["A"]},
        {"key": "C", "done": False, "effort": 2, "depends_on": ["B"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["critical_path"] == ["A", "B", "C"]
    assert result["critical_path_effort"] == 10.0
    assert result["blocked"] == ["B", "C"]
    assert result["downstream_impact"] == {"A": ["B", "C"], "B": ["C"], "C": []}


def test_dependency_impact_diamond_picks_the_heavier_branch():
    """B と C が両方 A に依存し、D が B と C 両方に依存する場合、
    重い方（C）を通る鎖がクリティカルパスになること。"""
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": True, "effort": 3, "depends_on": []},
        {"key": "B", "done": False, "effort": 5, "depends_on": ["A"]},
        {"key": "C", "done": False, "effort": 8, "depends_on": ["A"]},
        {"key": "D", "done": False, "effort": 2, "depends_on": ["B", "C"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["critical_path"] == ["A", "C", "D"]
    assert result["critical_path_effort"] == 10.0  # 0 (done) + 8 + 2
    assert result["downstream_impact"]["B"] == ["D"]
    assert result["downstream_impact"]["C"] == ["D"]


def test_dependency_impact_done_task_contributes_zero_effort():
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": True, "effort": 100, "depends_on": []},
        {"key": "B", "done": False, "effort": 1, "depends_on": ["A"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["critical_path_effort"] == 1.0  # A's 100 does not count
    assert result["blocked"] == []  # A is done, so B is not blocked


def test_dependency_impact_detects_a_blocked_task():
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": False, "effort": 1, "depends_on": []},
        {"key": "B", "done": False, "effort": 1, "depends_on": ["A"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["blocked"] == ["B"]


def test_dependency_impact_cycle_is_reported_not_crashed():
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "X", "done": False, "effort": 1, "depends_on": ["Y"]},
        {"key": "Y", "done": False, "effort": 1, "depends_on": ["X"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["cycles"] == [["X", "Y"]]
    assert result["critical_path"] == []  # cyclic keys excluded, nothing safe to report


def test_dependency_impact_cycle_does_not_block_unrelated_tasks():
    """循環はX・Yに閉じていて、無関係なZの計算まで壊さないこと。"""
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "X", "done": False, "effort": 1, "depends_on": ["Y"]},
        {"key": "Y", "done": False, "effort": 1, "depends_on": ["X"]},
        {"key": "Z", "done": False, "effort": 4, "depends_on": []},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["cycles"] == [["X", "Y"]]
    assert result["critical_path"] == ["Z"]
    assert result["critical_path_effort"] == 4.0


def test_dependency_impact_unestimated_task_counts_as_zero_but_is_flagged():
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": False, "effort": None, "depends_on": []},
        {"key": "B", "done": False, "effort": 5, "depends_on": ["A"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["critical_path_effort"] == 5.0  # A contributes 0, not an error
    assert result["critical_path_has_unestimated"] is True


def test_dependency_impact_unknown_dependency_key_is_ignored():
    """存在しない key への依存は無視する（データの綻びで全体を壊さない）。"""
    adapter = RiskForecastAdapter()
    tasks = [
        {"key": "A", "done": False, "effort": 1, "depends_on": ["ghost"]},
    ]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["blocked"] == []
    assert result["critical_path"] == ["A"]


def test_dependency_impact_missing_depends_on_means_no_dependencies():
    adapter = RiskForecastAdapter()
    tasks = [{"key": "A", "done": False, "effort": 1}]
    result = adapter.invoke("dependency_impact", {"tasks": tasks})

    assert result["blocked"] == []
    assert result["critical_path"] == ["A"]
