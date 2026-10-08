"""自己学習サイクルの、オプトイン自動承認（aipmo/self_learning.py）のテスト。

対象は `attach()` が登録する Engine の完了フックだけ。テンプレート自体
（templates/examples/self_learning_cycle.yaml）の実行は、ほかの例と同じく
ロードできることだけ test_guides.py 相当の検証で足りる——ローカル LLM を
実際に呼ぶ確認はここでは行わない（docs/PROVIDERS.md の範囲）。

Only the Engine completion hook `attach()` registers. The template itself
just needs to load like any other example; actually calling a local LLM is
out of scope here.
"""
from __future__ import annotations

from pathlib import Path

from aipmo.adapters.base import AdapterRegistry
from aipmo.engine.context import RunContext, StepResult
from aipmo.llm.embeddings import HashEmbedder
from aipmo.self_learning import TEMPLATE_NAME, attach, set_trust_rag, trust_rag_enabled

from tests.test_data_adapters import FakeQdrantClient, QdrantAdapter


class FakeEngine:
    def __init__(self, adapters: AdapterRegistry) -> None:
        self.adapters = adapters
        self.run_listeners: list = []


def build(tmp_path: Path):
    client = FakeQdrantClient()
    adapter = QdrantAdapter(tenant="acme", embedder=HashEmbedder(), client=client)
    adapters = AdapterRegistry()
    adapters.register(adapter, name="vector_store")
    engine = FakeEngine(adapters)
    control = tmp_path / "control.json"
    return engine, adapter, client, control


def submit_and_build_ctx(adapter, *, template_name: str = TEMPLATE_NAME,
                         text: str = "架空の課題への対応方針") -> RunContext:
    result = adapter.invoke("submit_candidate", {"knowledge": {"text": text}})
    ctx = RunContext(template_name=template_name)
    ctx.results["submit"] = StepResult(id="submit", status="success", output=result)
    ctx.step_adapters["submit"] = "vector_store"
    return ctx, result["id"]


def test_trust_rag_is_off_by_default(tmp_path):
    assert trust_rag_enabled(tmp_path / "control.json") is False


def test_enabling_and_disabling_round_trips(tmp_path):
    control = tmp_path / "control.json"
    set_trust_rag(control, True)
    assert trust_rag_enabled(control) is True
    set_trust_rag(control, False)
    assert trust_rag_enabled(control) is False


def test_does_nothing_when_trust_rag_is_disabled(tmp_path):
    engine, adapter, _client, control = build(tmp_path)
    attach(engine, control)
    ctx, candidate_id = submit_and_build_ctx(adapter)

    engine.run_listeners[0](TEMPLATE_NAME, ctx)

    assert adapter.get_candidate(candidate_id)["payload"]["review_status"] == "pending"


def test_auto_approves_when_trust_rag_is_enabled(tmp_path):
    engine, adapter, client, control = build(tmp_path)
    set_trust_rag(control, True)
    attach(engine, control)
    ctx, candidate_id = submit_and_build_ctx(adapter)

    engine.run_listeners[0](TEMPLATE_NAME, ctx)

    record = adapter.get_candidate(candidate_id)
    assert record["payload"]["review_status"] == "approved"
    assert "self_learning" in record["payload"]["reviewed_by"]
    assert any(c == "public_pmo_knowledge" for c, _ in client.upserts)


def test_never_touches_candidates_from_other_templates(tmp_path):
    engine, adapter, _client, control = build(tmp_path)
    set_trust_rag(control, True)
    attach(engine, control)
    ctx, candidate_id = submit_and_build_ctx(adapter, template_name="generalize_knowledge")

    engine.run_listeners[0]("generalize_knowledge", ctx)

    assert adapter.get_candidate(candidate_id)["payload"]["review_status"] == "pending"


def test_disabling_stops_future_approvals_but_keeps_the_past_decision(tmp_path):
    engine, adapter, _client, control = build(tmp_path)
    set_trust_rag(control, True)
    attach(engine, control)
    ctx, first_id = submit_and_build_ctx(adapter)
    engine.run_listeners[0](TEMPLATE_NAME, ctx)
    assert adapter.get_candidate(first_id)["payload"]["review_status"] == "approved"

    set_trust_rag(control, False)
    ctx2, second_id = submit_and_build_ctx(adapter, text="別の架空の課題への対応方針")
    engine.run_listeners[0](TEMPLATE_NAME, ctx2)

    assert adapter.get_candidate(second_id)["payload"]["review_status"] == "pending"
    # 以前に自動承認した判断そのものは消えない / the earlier decision is untouched
    assert adapter.get_candidate(first_id)["payload"]["review_status"] == "approved"


def test_a_crashing_adapter_never_breaks_the_run(tmp_path):
    """on_run_complete は run_listener——失敗しても元の実行を止めない。"""
    engine, adapter, _client, control = build(tmp_path)
    set_trust_rag(control, True)
    attach(engine, control)
    ctx = RunContext(template_name=TEMPLATE_NAME)
    ctx.results["submit"] = StepResult(
        id="submit", status="success",
        output={"id": "nope", "review_status": "pending"})
    ctx.step_adapters["submit"] = "vector_store"

    engine.run_listeners[0](TEMPLATE_NAME, ctx)   # does not raise, despite no such candidate
