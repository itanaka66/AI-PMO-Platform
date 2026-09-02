import pytest
from aipmo.adapters.base import AdapterRegistry
from aipmo.dsl.schema import Template, Step, StepKind
from aipmo.engine.runner import Engine
from aipmo.llm.registry import LLMRegistry
from aipmo.llm.base import EchoProvider

class MockPostgresAdapter:
    def __init__(self):
        self.invocations = []

    def actions(self):
        return {"execute": self.execute}

    def execute(self, payload):
        self.invocations.append(payload)

    def invoke(self, action, payload):
        if action == "execute":
            return self.execute(payload)
        raise ValueError(f"Unknown action {action}")

@pytest.fixture
def registry_with_postgres():
    registry = AdapterRegistry()
    registry.register(MockPostgresAdapter(), name="postgres")
    return registry

@pytest.fixture
def llm_registry():
    registry = LLMRegistry()
    registry.register("test-llm", lambda: EchoProvider())
    return registry

def test_engine_records_run_history(registry_with_postgres, llm_registry):
    engine = Engine(registry_with_postgres, llm_registry)
    template = Template(
        name="test-template",
        version="1.0",
        steps=[
            Step(
                id="step1",
                kind=StepKind.TRANSFORM,
                expression="count",
                inputs={"items": [1, 2, 3]}
            )
        ]
    )

    ctx = engine.run(template)

    pg = registry_with_postgres.get("postgres")
    assert len(pg.invocations) == 3

    start_call = pg.invocations[0]
    assert start_call["name"] == "record_run"
    assert start_call["params"]["id"] == ctx.run_id
    assert start_call["params"]["template"] == "test-template"
    assert start_call["params"]["status"] == "running"

    step_call = pg.invocations[1]
    assert step_call["name"] == "record_step_result"
    assert step_call["params"]["run_id"] == ctx.run_id
    assert step_call["params"]["step_id"] == "step1"
    assert step_call["params"]["status"] == "success"

    # "count" returns 3
    assert step_call["params"]["output"].obj == 3

    finish_call = pg.invocations[2]
    assert finish_call["name"] == "finish_run"
    assert finish_call["params"]["id"] == ctx.run_id
    assert finish_call["params"]["status"] == "success"

def test_engine_truncates_large_output(registry_with_postgres, llm_registry):
    engine = Engine(registry_with_postgres, llm_registry)

    # Generates a payload larger than 8000 bytes
    large_payload = "A" * 10000

    # A transform step that just returns what it is given
    template = Template(
        name="large-output-template",
        version="1.0",
        steps=[
            Step(
                id="step1",
                kind=StepKind.TRANSFORM,
                inputs=large_payload
            )
        ]
    )

    engine.run(template)

    pg = registry_with_postgres.get("postgres")
    assert len(pg.invocations) == 3

    step_call = pg.invocations[1]
    assert step_call["name"] == "record_step_result"

    output = step_call["params"]["output"].obj
    # Because it is > 8000 bytes, it should be truncated
    assert "truncated" in output
    assert output["truncated"] is True
    assert output["original_size_bytes"] > 8000
    assert len(output["preview"]) <= 500
