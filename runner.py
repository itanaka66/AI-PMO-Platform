"""テンプレート実行エンジン。

ステップを上から順に実行し、各ステップの出力を
後続ステップから参照できる形でコンテキストに積んでいく。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import AdapterRegistry
from ..dsl.expr import evaluate_condition, render
from ..dsl.schema import OutputFormat, Step, StepKind, Template
from ..llm.base import LLMRequest
from ..llm.registry import LLMRegistry
from .agent import run_agent
from .context import RunContext, StepResult

logger = logging.getLogger("aipmo.engine")


class StepFailure(Exception):
    """途中で失敗した実行。そこまでの結果を保持する。

    失敗そのものより「どの工程で落ちたか」が知りたい情報なので、
    例外に実行コンテキストを持たせる。これが無いと、呼び出し側は
    工程ごとの結果を捨てることになる。

    Carries the run context, not just the message: what the caller needs is
    which step failed and what ran before it. Without this the per-step results
    are discarded exactly when they matter most.
    """

    def __init__(self, step_id: str, message: str,
                 context: "RunContext | None" = None) -> None:
        super().__init__(f"ステップ '{step_id}' が失敗しました: {message}")
        self.step_id = step_id
        self.context = context


class PromptLibrary:
    """prompts/ 配下のプロンプトテンプレートを名前で引く。

    プロンプトを YAML 本体から分離しているのは、
    業界別ノウハウの差分がほぼプロンプトに集中するため。
    テンプレート構造は共通のまま、プロンプトだけ差し替えれば
    別業界向けテンプレートになる。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else None
        self._inline: dict[str, str] = {}

    def add(self, name: str, text: str) -> None:
        self._inline[name] = text

    def get(self, name: str) -> str:
        if name in self._inline:
            return self._inline[name]
        if self.root:
            for suffix in (".md", ".txt", ".prompt"):
                candidate = self.root / f"{name}{suffix}"
                if candidate.exists():
                    return candidate.read_text(encoding="utf-8")
        raise KeyError(f"プロンプト '{name}' が見つかりません")


class Engine:
    def __init__(
        self,
        adapters: AdapterRegistry,
        llms: LLMRegistry,
        prompts: PromptLibrary | None = None,
        transforms: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.adapters = adapters
        self.llms = llms
        self.prompts = prompts or PromptLibrary()
        self.transforms = transforms or {}

    def run(
        self,
        template: Template,
        params: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> RunContext:
        merged = {**template.params, **(params or {})}
        ctx = RunContext(
            template_name=template.name,
            params=merged,
            trigger=trigger or {},
        )
        logger.info("run %s start (template=%s)", ctx.run_id, template.name)

        for step in template.steps:
            result = self._run_step(step, ctx)
            ctx.results[step.id] = result

            if result.status == "failed" and not step.continue_on_error:
                logger.error("run %s aborted at step %s", ctx.run_id, step.id)
                raise StepFailure(step.id, result.error or "不明なエラー", context=ctx)

        logger.info("run %s finished", ctx.run_id)
        return ctx

    # ------------------------------------------------------------------

    def _run_step(self, step: Step, ctx: RunContext) -> StepResult:
        scope = ctx.scope()

        if step.when is not None:
            try:
                if not evaluate_condition(step.when, scope):
                    logger.info("step %s skipped (when=false)", step.id)
                    return StepResult(id=step.id, status="skipped")
            except Exception as exc:
                return StepResult(id=step.id, status="failed", error=str(exc))

        started = time.monotonic()
        last_error: str | None = None

        for attempt in range(1, max(1, step.retry.max_attempts) + 1):
            try:
                output = self._dispatch(step, ctx, scope)
                return StepResult(
                    id=step.id,
                    status="success",
                    output=output,
                    attempts=attempt,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "step %s attempt %d/%d failed: %s",
                    step.id, attempt, step.retry.max_attempts, last_error,
                )
                if attempt < step.retry.max_attempts:
                    time.sleep(step.retry.backoff_seconds * attempt)

        return StepResult(
            id=step.id,
            status="failed",
            error=last_error,
            attempts=step.retry.max_attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _dispatch(self, step: Step, ctx: RunContext, scope: dict[str, Any]) -> Any:
        if step.kind is StepKind.ADAPTER:
            return self._run_adapter(step, ctx, scope)
        if step.kind is StepKind.LLM:
            return self._run_llm(step, scope)
        if step.kind is StepKind.TRANSFORM:
            return self._run_transform(step, scope)
        if step.kind is StepKind.AGENT:
            return self._run_agent(step, scope)
        raise ValueError(f"未対応のステップ種別: {step.kind}")

    def _run_adapter(self, step: Step, ctx: RunContext, scope: dict[str, Any]) -> Any:
        adapter = self.adapters.get(step.adapter or "")
        payload: dict[str, Any] = render({**step.config, **step.inputs}, scope)

        # 書き込み系アクションが受け付ける場合のみ冪等キーを注入する
        actions = adapter.actions()
        func = actions.get(step.action or "")
        if func is not None:
            import inspect

            signature = inspect.signature(func)
            if "idempotency_key" in signature.parameters and "idempotency_key" not in payload:
                payload["idempotency_key"] = ctx.idempotency_key(step.id)

        return adapter.invoke(step.action or "", payload)

    def _run_llm(self, step: Step, scope: dict[str, Any]) -> Any:
        spec = step.llm
        assert spec is not None
        provider = self.llms.get(spec.profile)

        raw_prompt = step.prompt_inline or self.prompts.get(step.prompt or "")
        prompt = render(raw_prompt, {**scope, "inputs": render(step.inputs, scope)})

        want_json = step.output_format is OutputFormat.JSON
        response = provider.complete(
            LLMRequest(
                prompt=prompt,
                system=step.config.get("system"),
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                json_mode=want_json,
            )
        )

        if not want_json:
            return response.text

        data = response.as_json()
        if step.output_schema:
            _check_schema(data, step.output_schema, step.id)
        return data

    def _run_agent(self, step: Step, scope: dict[str, Any]) -> Any:
        spec = step.agent
        assert spec is not None and step.llm is not None

        provider = self.llms.get(step.llm.profile)
        raw_prompt = step.prompt_inline or self.prompts.get(step.prompt or "")
        prompt = render(raw_prompt, {**scope, "inputs": render(step.inputs, scope)})

        result = run_agent(
            provider=provider,
            adapters=self.adapters,
            spec=spec,
            prompt=prompt,
            system=step.config.get("system"),
            temperature=step.llm.temperature,
            max_tokens=step.llm.max_tokens,
        )

        output: Any = {
            "answer": result.answer,
            "iterations": result.iterations,
            "stopped_because": result.stopped_because,
            "tool_calls": [
                {"name": r.name, "arguments": r.arguments,
                 "ok": r.ok, "error": r.error}
                for r in result.tool_calls
            ],
            "tokens": {"input": result.input_tokens, "output": result.output_tokens},
        }

        if step.output_format is OutputFormat.JSON:
            from ..llm.base import LLMResponse

            data = LLMResponse(text=result.answer, model="agent").as_json()
            if step.output_schema:
                _check_schema(data, step.output_schema, step.id)
            output["answer"] = data

        return output

    def _run_transform(self, step: Step, scope: dict[str, Any]) -> Any:
        name = step.expression or ""
        if name in self.transforms:
            return self.transforms[name](**render(step.inputs, scope))
        # 組み込み変換が無ければ、単なる値の組み立てとして扱う
        return render(step.inputs or name, scope)


def _check_schema(data: Any, schema: dict[str, Any], step_id: str) -> None:
    """最小限の構造検証。

    JSON Schema をフルに実装せず、required キーの存在確認に留める。
    LLM 出力の欠落は「後続ステップで謎の KeyError」になるのが最悪なので、
    そこだけ早期に潰す。
    """
    required = schema.get("required") or []
    if required and not isinstance(data, dict):
        raise ValueError(f"ステップ '{step_id}': オブジェクトを期待しましたが {type(data).__name__} でした")
    missing = [key for key in required if key not in (data or {})]
    if missing:
        raise ValueError(
            f"ステップ '{step_id}': LLM 出力に必須キーがありません: {', '.join(missing)}"
        )
