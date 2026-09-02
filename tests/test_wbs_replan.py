"""WBS 再計画 AI のテスト / WBS-replanning agent tests.

2つの層:
  1. WbsReplanAdapter 単体: tier が snapshot から機械的に決まり、LLM の
     ツール呼び出し引数からは受け取らないこと。スナップショット無しでの
     提案が拒否されること。
  2. テンプレート実行: エンジンで wbs_replan.yaml を最初から最後まで
     動かし、エージェントが propose を呼んで承認待ちの行が実際に
     作られること、tier が LLM の申告と無関係にサーバー側の値になること
     を確認する。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aipmo.adapters.base import AdapterError, AdapterRegistry
from aipmo.adapters.postgres import PostgresAdapter
from aipmo.adapters.risk_forecast import RiskForecastAdapter
from aipmo.adapters.wbs_replan import WbsReplanAdapter

ROOT = Path(__file__).resolve().parents[1]
REAL_QUERIES = yaml.safe_load((ROOT / "queries.yaml").read_text(encoding="utf-8"))


class FakeConnection:
    def __init__(self, snapshot_rows=None, snapshot_columns=None,
                 save_rows=None, save_columns=None) -> None:
        self.log: list = []
        self.snapshot_rows = snapshot_rows or []
        self.snapshot_columns = snapshot_columns or []
        self.save_rows = save_rows or []
        self.save_columns = save_columns or []
        self.commits = 0

    def cursor(self):
        return _RoutingCursor(self)

    def commit(self):
        self.commits += 1


class _RoutingCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self._rows: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, values=None):
        self.conn.log.append((sql, list(values or [])))
        if "wbs_forecast_snapshots" in sql:
            self._rows = self.conn.snapshot_rows
            self.description = [(c,) for c in self.conn.snapshot_columns] or None
        elif "wbs_replan_proposals" in sql:
            self._rows = self.conn.save_rows
            self.description = [(c,) for c in self.conn.save_columns] or None
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


def _pg(connection) -> PostgresAdapter:
    return PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)


def test_propose_derives_tier_from_snapshot_not_caller():
    connection = FakeConnection(
        snapshot_rows=[(6.0, 2, {"x": 1}, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
        save_rows=[("proposal-1",)], save_columns=["id"],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    result = adapter.invoke("propose", {
        "wbs_id": "wbs-1", "diff": {"move": ["T1"]},
        "rationale": "because", "confidence": 0.5,
    })

    assert result["proposed"] is True
    assert result["tier"] == 2

    sql, values = connection.log[-1]
    assert 2 in values


def test_propose_signature_has_no_tier_parameter():
    from aipmo.adapters.base import describe_action

    schema = describe_action(WbsReplanAdapter.propose)
    assert "tier" not in schema["parameters"]["properties"]
    assert set(schema["parameters"]["properties"]) == {
        "wbs_id", "diff", "rationale", "confidence", "assumptions", "option_label",
    }


def test_propose_without_snapshot_raises():
    connection = FakeConnection(snapshot_rows=[], snapshot_columns=[])
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    with pytest.raises(AdapterError, match="no forecast snapshot"):
        adapter.invoke("propose", {
            "wbs_id": "wbs-1", "diff": {}, "rationale": "x", "confidence": 0.5,
        })


def test_propose_with_null_tier_snapshot_raises():
    connection = FakeConnection(
        snapshot_rows=[(None, None, None, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    with pytest.raises(AdapterError, match="no forecast snapshot"):
        adapter.invoke("propose", {
            "wbs_id": "wbs-1", "diff": {}, "rationale": "x", "confidence": 0.5,
        })


def test_propose_uses_wbs_and_tier_as_idempotency_key():
    connection = FakeConnection(
        snapshot_rows=[(6.0, 2, {}, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
        save_rows=[("proposal-1",)], save_columns=["id"],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    adapter.invoke("propose", {
        "wbs_id": "wbs-9", "diff": {}, "rationale": "x", "confidence": 0.5,
    })

    sql, values = connection.log[-1]
    assert "wbs-9:tier2" in values


def test_propose_reports_not_proposed_when_no_row_returned():
    connection = FakeConnection(
        snapshot_rows=[(6.0, 2, {}, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
        save_rows=[], save_columns=[],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    result = adapter.invoke("propose", {
        "wbs_id": "wbs-1", "diff": {}, "rationale": "x", "confidence": 0.5,
    })

    assert result["proposed"] is False
    assert result["id"] is None


def test_pending_count_filters_by_wbs_id():
    class _PendingConnection(FakeConnection):
        def cursor(self_inner):
            cur = _RoutingCursor(self_inner)

            def execute(sql, values=None):
                cur.conn.log.append((sql, list(values or [])))
                if "wbs_replan_proposals" in sql:
                    cur._rows = [
                        ("p1", "wbs-a"),
                        ("p2", "wbs-b"),
                    ]
                    cur.description = [("id",), ("wbs_version_from",)]
                else:
                    cur._rows = []
                    cur.description = None
                cur.rowcount = len(cur._rows)

            cur.execute = execute
            return cur

    adapter = WbsReplanAdapter(postgres=_pg(_PendingConnection()))
    result = adapter.invoke("pending_count", {"wbs_id": "wbs-a"})
    assert result["count"] == 1

    result_all = adapter.invoke("pending_count", {})
    assert result_all["count"] == 2


# --- テンプレートの実行 (wbs_replan.yaml) --------------------------------------

class _RoutingConnection:
    def __init__(self) -> None:
        self.log: list = []
        self.latest_snapshot = None
        self.proposals: list = []

    def cursor(self):
        return _TemplateCursor(self)

    def commit(self):
        pass


class _TemplateCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self._rows: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, values=None):
        values = list(values or [])
        self.conn.log.append((sql, values))
        upper = sql.strip().upper()

        if "wbs_forecast_snapshots" in sql and upper.startswith("SELECT"):
            self._rows = [self.conn.latest_snapshot] if self.conn.latest_snapshot else [(None, None, None, None)]
            self.description = [("drift_days",), ("tier",), ("forecast",), ("recorded_at",)]
        elif "wbs_replan_proposals" in sql and upper.startswith("SELECT"):
            self._rows = []
            self.description = [("id",), ("wbs_version_from",)]
        elif "wbs_forecast_snapshots" in sql and upper.startswith(("INSERT", "UPDATE")):
            self.conn.latest_snapshot = (values[2], values[3], values[4], "now")
            self._rows = []
            self.description = None
        elif "wbs_replan_proposals" in sql and upper.startswith("INSERT"):
            self.conn.proposals.append(tuple(values))
            self._rows = [("proposal-1",)]
            self.description = [("id",)]
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


def _load_template(filename="wbs_replan.yaml"):
    from aipmo.dsl import loader

    return loader.load_file(ROOT / "templates" / "examples" / filename)


def _scripted_propose_script(wbs_id, diff, rationale, confidence, assumptions=None):
    from aipmo.llm.base import LLMResponse, ToolCall

    return [
        LLMResponse(text="", model="scripted", tool_calls=[
            ToolCall(id="c1", name="wbs_replan__propose", arguments={
                "wbs_id": wbs_id, "diff": diff, "rationale": rationale,
                "confidence": confidence, "assumptions": assumptions or {},
            }),
        ]),
        LLMResponse(text="再計画案を記録しました。", model="scripted"),
    ]


def test_template_agent_proposes_replan_on_severe_drift():
    from aipmo.adapters.mock import MockSlackAdapter
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry

    connection = _RoutingConnection()
    adapters = AdapterRegistry()
    pg = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)
    adapters.register(pg)
    adapters.register(RiskForecastAdapter(tier2_at=1.0, tier3_at=5.0,
                                          detect_at=1.0, clear_below=0.3))
    adapters.register(WbsReplanAdapter(postgres=pg))
    adapters.register(MockSlackAdapter())

    llms = LLMRegistry()
    llms.register("default", EchoProvider(script=_scripted_propose_script(
        wbs_id="wbs-test",
        diff={"move": [{"task": "T2", "new_deadline": "2026-01-15"}]},
        rationale="T2の見積もりが甘いため2週間後ろ倒しを提案",
        confidence=0.6,
        assumptions={"担当者の稼働": "未確認"},
    )))

    engine = Engine(adapters, llms)
    template = _load_template()

    tasks = [
        {"key": "T1", "done": True, "effort": 2},
        {"key": "T2", "done": False, "effort": 10},
    ]
    ctx = engine.run(template, params={
        "wbs_id": "wbs-test", "deadline": "2026-01-05", "velocity_per_day": 1.0,
        "tasks": tasks,
    })

    assert ctx.results["classify"].output["tier"] == 3
    assert ctx.results["replan"].status == "success"
    assert ctx.results["replan"].output["tool_calls"][0]["ok"] is True
    assert ctx.results["notify_critical"].status == "success"

    assert len(connection.proposals) == 1
    saved = connection.proposals[0]
    assert 3 in saved
    llm_arguments = ctx.results["replan"].output["tool_calls"][0]["arguments"]
    assert "tier" not in llm_arguments


def test_template_skips_replan_and_notify_when_on_schedule():
    from aipmo.adapters.mock import MockSlackAdapter
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry

    connection = _RoutingConnection()
    adapters = AdapterRegistry()
    pg = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)
    adapters.register(pg)
    adapters.register(RiskForecastAdapter())
    adapters.register(WbsReplanAdapter(postgres=pg))
    adapters.register(MockSlackAdapter())

    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    engine = Engine(adapters, llms)
    template = _load_template()

    tasks = [
        {"key": "T1", "done": True, "effort": 8},
        {"key": "T2", "done": False, "effort": 1},
    ]
    ctx = engine.run(template, params={
        "wbs_id": "wbs-ok", "deadline": "2026-12-31", "velocity_per_day": 2.0,
        "tasks": tasks,
    })

    assert ctx.results["classify"].output["tier"] == 1
    assert ctx.results["replan"].status == "skipped"
    assert ctx.results["notify_critical"].status == "skipped"
    assert connection.proposals == []


# --- A/B の複数案 (option_label) ------------------------------------------

def test_propose_without_option_label_keeps_old_idempotency_key():
    """option_label を省略すると、これまで通り wbs_id:tier のキーになる
    （既存テンプレートの挙動を変えない）。"""
    connection = FakeConnection(
        snapshot_rows=[(6.0, 2, {}, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
        save_rows=[("proposal-1",)], save_columns=["id"],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    adapter.invoke("propose", {
        "wbs_id": "wbs-9", "diff": {}, "rationale": "x", "confidence": 0.5,
    })

    sql, values = connection.log[-1]
    assert "wbs-9:tier2" in values
    assert not any(":tier2:" in v for v in values if isinstance(v, str))


def test_propose_with_option_label_appends_it_to_the_idempotency_key():
    connection = FakeConnection(
        snapshot_rows=[(6.0, 2, {}, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
        save_rows=[("proposal-1",)], save_columns=["id"],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    result = adapter.invoke("propose", {
        "wbs_id": "wbs-9", "diff": {}, "rationale": "x", "confidence": 0.5,
        "option_label": "reschedule",
    })

    sql, values = connection.log[-1]
    assert "wbs-9:tier2:reschedule" in values
    assert result["option_label"] == "reschedule"


def test_two_option_labels_for_the_same_wbs_and_tier_do_not_collide():
    """A/B の2案が、互いを上書きせず両方とも保存されること
    （実際の INSERT 呼び出しが2回とも行われ、source_key が異なる）。"""
    connection = FakeConnection(
        snapshot_rows=[(6.0, 2, {}, "2026-01-01")],
        snapshot_columns=["drift_days", "tier", "forecast", "recorded_at"],
        save_rows=[("proposal-1",)], save_columns=["id"],
    )
    adapter = WbsReplanAdapter(postgres=_pg(connection))

    adapter.invoke("propose", {
        "wbs_id": "wbs-1", "diff": {"a": 1}, "rationale": "reschedule",
        "confidence": 0.6, "option_label": "reschedule",
    })
    adapter.invoke("propose", {
        "wbs_id": "wbs-1", "diff": {"b": 2}, "rationale": "add resources",
        "confidence": 0.5, "option_label": "add_resources",
    })

    # save_wbs_proposal を呼んだ2回それぞれの source_key を確認
    save_calls = [values for sql, values in connection.log if "wbs_replan_proposals" in sql]
    source_keys = [v for values in save_calls for v in values if isinstance(v, str) and v.startswith("wbs-1:tier2:")]
    assert set(source_keys) == {"wbs-1:tier2:reschedule", "wbs-1:tier2:add_resources"}


def test_pending_wbs_proposals_query_selects_option_label():
    """named query が option_label を SELECT していること
    （出荷される queries.yaml に対する束縛検証）。"""
    connection = FakeConnection(
        save_rows=[("p1", "wbs-1", {}, "r", {}, 2, 0.6, "reschedule", "2026-01-01")],
        save_columns=["id", "wbs_version_from", "diff", "rationale", "assumptions",
                      "tier", "confidence", "option_label", "created_at"],
    )
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    result = adapter.invoke("query", {"name": "pending_wbs_proposals", "params": {}})

    sql, _ = connection.log[0]
    assert "option_label" in sql
    assert result["rows"][0]["option_label"] == "reschedule"


def test_save_wbs_proposal_query_binds_option_label():
    connection = FakeConnection(save_rows=[("p1",)], save_columns=["id"])
    adapter = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)

    adapter.invoke("execute", {
        "name": "save_wbs_proposal",
        "params": {
            "id": "p1", "wbs_version_from": "v3", "diff": {}, "rationale": "x",
            "assumptions": {}, "tier": 2, "confidence": 0.6,
            "option_label": "reschedule",
        },
        "idempotency_key": "wbs-1:tier2:reschedule",
    })

    sql, values = connection.log[0]
    assert "option_label" in sql
    assert "reschedule" in values


# --- テンプレートの実行 (wbs_replan_options.yaml, A/B提示) --------------------

def test_template_agent_proposes_two_distinct_alternatives():
    """依存関係情報が計算され、AI が option_label の異なる2つの提案を
    それぞれ独立した行として記録すること。"""
    from aipmo.adapters.mock import MockSlackAdapter
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider, LLMResponse, ToolCall
    from aipmo.llm.registry import LLMRegistry

    connection = _RoutingConnection()
    adapters = AdapterRegistry()
    pg = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)
    adapters.register(pg)
    adapters.register(RiskForecastAdapter(tier2_at=1.0, tier3_at=5.0,
                                          detect_at=1.0, clear_below=0.3))
    adapters.register(WbsReplanAdapter(postgres=pg))
    adapters.register(MockSlackAdapter())

    def json_call(call_id, diff, rationale, confidence, option_label):
        return LLMResponse(text="", model="scripted", tool_calls=[
            ToolCall(id=call_id, name="wbs_replan__propose", arguments={
                "wbs_id": "wbs-test", "diff": diff, "rationale": rationale,
                "confidence": confidence, "option_label": option_label,
                "assumptions": {},
            }),
        ])

    llms = LLMRegistry()
    llms.register("default", EchoProvider(script=[
        json_call("c1", {"move": [{"task": "T2", "new_deadline": "2026-01-20"}]},
                 "クリティカルパス上のT2の締切を延ばす", 0.6, "extend_deadline"),
        json_call("c2", {"add_staff": ["T3"]},
                 "並行タスクT3に要員を追加", 0.5, "add_resources"),
        LLMResponse(text="2つの代替案を記録しました。", model="scripted"),
    ]))

    engine = Engine(adapters, llms)
    template = _load_template("wbs_replan_options.yaml")

    tasks = [
        {"key": "T1", "done": True, "effort": 2, "depends_on": []},
        {"key": "T2", "done": False, "effort": 10, "depends_on": ["T1"]},
        {"key": "T3", "done": False, "effort": 3, "depends_on": ["T1"]},
    ]
    ctx = engine.run(template, params={
        "wbs_id": "wbs-test", "deadline": "2026-01-05", "velocity_per_day": 1.0,
        "tasks": tasks,
    })

    assert ctx.results["dependencies"].output["critical_path"] == ["T1", "T2"]
    assert ctx.results["dependencies"].output["critical_path_effort"] == 10.0
    assert ctx.results["classify"].output["tier"] == 3
    assert ctx.results["replan"].status == "success"
    assert ctx.results["notify_critical"].status == "success"

    assert len(connection.proposals) == 2
    option_labels = {
        v for values in connection.proposals for v in values
        if v in ("extend_deadline", "add_resources")
    }
    assert option_labels == {"extend_deadline", "add_resources"}


def test_template_skips_replan_and_notify_when_on_schedule_ab_variant():
    from aipmo.adapters.mock import MockSlackAdapter
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry

    connection = _RoutingConnection()
    adapters = AdapterRegistry()
    pg = PostgresAdapter(queries=REAL_QUERIES, tenant="acme", connection=connection)
    adapters.register(pg)
    adapters.register(RiskForecastAdapter())
    adapters.register(WbsReplanAdapter(postgres=pg))
    adapters.register(MockSlackAdapter())

    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    engine = Engine(adapters, llms)
    template = _load_template("wbs_replan_options.yaml")

    tasks = [
        {"key": "T1", "done": True, "effort": 8, "depends_on": []},
        {"key": "T2", "done": False, "effort": 1, "depends_on": ["T1"]},
    ]
    ctx = engine.run(template, params={
        "wbs_id": "wbs-ok", "deadline": "2026-12-31", "velocity_per_day": 2.0,
        "tasks": tasks,
    })

    assert ctx.results["classify"].output["tier"] == 1
    assert ctx.results["replan"].status == "skipped"
    assert ctx.results["notify_critical"].status == "skipped"
    assert connection.proposals == []
