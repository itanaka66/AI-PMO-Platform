"""テンプレート実行エンジン。

ステップを上から順に実行し、各ステップの出力を
後続ステップから参照できる形でコンテキストに積んでいく。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import AdapterRegistry
from ..dsl.expr import ResolutionError, evaluate_condition, render
from ..dsl.schema import LLMSpec, OutputFormat, Step, StepKind, Template
from ..llm.base import LLMRequest, LLMResponse
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


def _days_between(start: str, end: str) -> int | None:
    """2つの日付の間の日数 / whole days between two dates.

    テンプレートに計算の仕組みは無く、言語モデルに数えさせると間違えます。
    「公開まで何日か」のような、数えれば決まる値を渡す側で決めるための道具。

    Templates cannot do arithmetic and a language model miscounts. This settles
    countable facts — such as how many days remain until a launch — before they
    are handed over.
    """
    from datetime import date

    try:
        first = date.fromisoformat(str(start)[:10])
        second = date.fromisoformat(str(end)[:10])
    except ValueError:
        # 書式が違うだけで実行全体を止めない。
        # A malformed date does not stop the whole run.
        logger.warning("days_between: 日付を読めません / cannot read %r, %r",
                       start, end)
        return None
    return (second - first).days


def _count(items: Any) -> int:
    return len(items) if isinstance(items, (list, dict, str)) else 0


# テンプレートから使える組み込み変換 / built-in transforms available to templates
BUILTIN_TRANSFORMS: dict[str, Any] = {
    "days_between": _days_between,
    "count": _count,
}


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
        # 利用者定義のものを優先する。名前がぶつかったら上書きできる。
        # User-supplied transforms win, so a name can be overridden.
        self.transforms = {**BUILTIN_TRANSFORMS, **(transforms or {})}

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
            except ResolutionError:
                # 参照先が飛ばされていて、出力が無い。
                # 依存している工程が動かなかったのだから、この工程も動かない。
                # 綴り間違いはロード時に弾いてあるので、ここに来る未解決は
                # 「先行がスキップされた」以外にない。実行ごと止めるのは誤り。
                #
                # The step this depends on was skipped, so it has no output.
                # Typos are already rejected at load time, so an unresolved
                # reference here can only mean a skipped predecessor — and
                # aborting the whole run for that is the wrong answer.
                logger.info("step %s skipped (依存する工程が実行されていない "
                            "/ a step it depends on did not run)", step.id)
                return StepResult(id=step.id, status="skipped")
            except Exception as exc:
                return StepResult(id=step.id, status="failed", error=str(exc))

        if step.for_each is not None:
            return self._run_for_each(step, ctx, scope)

        if step.kind is StepKind.PARALLEL:
            return self._run_parallel(step, ctx)

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

    def _run_for_each(self, step: Step, ctx: RunContext,
                      scope: dict[str, Any]) -> StepResult:
        """値の並びに対して、同じ工程を1件ずつ実行する。

        1件の失敗で全体を止めない。5人に催促を送る処理で、3人目の
        Slack ID が見つからないだけで4人目と5人目に届かないのは困る。
        失敗は記録して先へ進み、結果に残す。

        One failure does not stop the rest: when chasing five people, a missing
        Slack id for the third must not prevent the fourth and fifth from being
        told. Failures are recorded and the loop continues.
        """
        started = time.monotonic()

        try:
            items = render(step.for_each, scope)
        except Exception as exc:
            return StepResult(id=step.id, status="failed",
                              error=f"for_each を解決できません / {exc}")

        if items is None:
            items = []
        if not isinstance(items, list):
            return StepResult(
                id=step.id, status="failed",
                error=f"for_each は並びである必要があります / for_each must be a "
                      f"list, got {type(items).__name__}",
            )

        if not items:
            # 空は失敗ではない。遅延が0件なら催促は0通で正しい。
            # Empty is not a failure: no overdue work means no chasing.
            return StepResult(id=step.id, status="success",
                              output={"results": [], "count": 0, "failed": 0})

        truncated = len(items) > step.max_items
        if truncated:
            # 上限を超えたぶんは実行しない。500通の DM を誤って送るより、
            # 途中で止めて気づける方がよい。
            # Beyond the ceiling nothing runs: stopping short and being noticed
            # beats accidentally sending five hundred direct messages.
            logger.warning("step %s: %d 件を %d 件に制限 / capped at %d",
                           step.id, len(items), step.max_items, step.max_items)
            items = items[:step.max_items]

        results: list[Any] = []
        failures: list[dict[str, Any]] = []

        skipped = 0
        for index, item in enumerate(items):
            item_scope = {**scope, step.as_name: item, "loop": {
                # index は 0 始まり（計算用）、number は 1 始まり（表示用）。
                # 「1 / 2」が2番目を指す表示は、読む側が必ず取り違える。
                # index counts from zero for arithmetic; number counts from one
                # for display, because "1 / 2" shown against the second item is
                # something a reader will always misread.
                "index": index, "number": index + 1, "total": len(items),
            }}

            if step.where is not None:
                try:
                    if not evaluate_condition(step.where, item_scope):
                        skipped += 1
                        continue
                except Exception as exc:
                    # 条件を判定できない要素は実行しない。
                    # 判定できないまま書き込むより、飛ばして数に残す方が安全。
                    # An element whose condition cannot be evaluated is not run:
                    # skipping and counting it beats writing on an unknown.
                    logger.warning("step %s item %d: where を評価できません / %s",
                                   step.id, index, exc)
                    skipped += 1
                    continue

            outcome = self._attempt(step, ctx, item_scope)
            if outcome[0]:
                results.append(outcome[1])
            else:
                failures.append({"index": index, "error": outcome[1]})

        # 全部失敗したなら、その工程は失敗。1件でも通れば成功として続ける。
        # All failing means the step failed; any success lets the run continue.
        status = "failed" if failures and not results else "success"

        return StepResult(
            id=step.id,
            status=status,
            output={"results": results, "count": len(results),
                    "failed": len(failures), "errors": failures,
                    "skipped": skipped, "truncated": truncated},
            error=f"{len(failures)} 件が失敗 / {len(failures)} failed" if failures else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _run_parallel(self, step: Step, ctx: RunContext) -> StepResult:
        """互いに依存しないステップを同時に実行する。

        グループの中の工程は、グループが始まる前の ctx.results だけを見る。
        誰も他人の出力を待たずに走るので、そもそも参照できてはいけない
        （ロード時にも検証済み）。結果は全員そろってから、宣言順に
        まとめて ctx.results へ書き込む — 走っている間は誰も書き込まない
        ので、途中経過を別の工程が覗き見ることもない。

        1件の失敗で全体を止めない。全滅したときだけこの工程自体が失敗になる。
        for_each と同じ考え方。

        Steps in the group only ever see ctx.results as it stood before the
        group started — nobody waits on anybody else, so nobody should be able
        to reference another's output either (checked at load time too).
        Results are written into ctx.results together, in declared order, only
        after everyone has finished; nothing is written while the group is
        still running, so there is nothing mid-flight for another step to see.

        One failure does not sink the group; it fails only when every nested
        step does — the same reasoning as `for_each`.
        """
        started = time.monotonic()
        nested_steps = step.parallel

        with ThreadPoolExecutor(max_workers=len(nested_steps)) as pool:
            futures = {pool.submit(self._run_step, nested, ctx): nested
                       for nested in nested_steps}
            results_by_id = {nested.id: future.result()
                             for future, nested in futures.items()}

        for nested in nested_steps:
            ctx.results[nested.id] = results_by_id[nested.id]

        failed = [r for r in results_by_id.values() if r.status == "failed"]
        status = "failed" if len(failed) == len(results_by_id) else "success"

        return StepResult(
            id=step.id,
            status=status,
            output={"count": len(results_by_id) - len(failed), "failed": len(failed)},
            error=f"{len(failed)} 件が失敗 / {len(failed)} failed" if failed else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _attempt(self, step: Step, ctx: RunContext,
                 scope: dict[str, Any]) -> tuple[bool, Any]:
        last_error = "不明なエラー"
        for attempt in range(1, max(1, step.retry.max_attempts) + 1):
            try:
                return True, self._dispatch(step, ctx, scope)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < step.retry.max_attempts:
                    time.sleep(step.retry.backoff_seconds * attempt)
        return False, last_error

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

        raw_prompt = step.prompt_inline or self.prompts.get(step.prompt or "")
        prompt = render(raw_prompt, {**scope, "inputs": render(step.inputs, scope)})
        want_json = step.output_format is OutputFormat.JSON

        if not spec.profiles:
            response = self._complete(spec.profile, step, spec, prompt, want_json)
            return self._extract(response, step, want_json)

        return self._run_llm_fanout(step, spec, prompt, want_json)

    def _complete(self, profile: str, step: Step, spec: LLMSpec, prompt: str,
                  want_json: bool) -> LLMResponse:
        provider = self.llms.get(profile)
        return provider.complete(
            LLMRequest(
                prompt=prompt,
                system=step.config.get("system"),
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                json_mode=want_json,
            )
        )

    def _extract(self, response: LLMResponse, step: Step, want_json: bool) -> Any:
        if not want_json:
            return response.text
        data = response.as_json()
        if step.output_schema:
            _check_schema(data, step.output_schema, step.id)
        return data

    def _run_llm_fanout(self, step: Step, spec: LLMSpec, prompt: str,
                        want_json: bool) -> Any:
        """同じプロンプトを複数の提供元に同時に投げ、比較できる形で返す。

        1件の失敗で全体を止めない。全滅したときだけ例外にして、
        通常のリトライ・失敗経路に乗せる。for_each の失敗の扱いと同じ考え方。

        One dead provider does not sink the rest: this only raises when every
        profile fails, so the normal step retry path takes over. Same reasoning
        as `for_each`.
        """
        profiles = spec.profiles
        with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
            futures = {
                pool.submit(self._complete, profile, step, spec, prompt, want_json): profile
                for profile in profiles
            }
            by_profile: dict[str, dict[str, Any]] = {}
            for future, profile in futures.items():
                try:
                    response = future.result()
                    entry: dict[str, Any] = {
                        "profile": profile, "model": response.model, "ok": True,
                    }
                    entry["data" if want_json else "text"] = self._extract(
                        response, step, want_json
                    )
                except Exception as exc:
                    entry = {
                        "profile": profile, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                by_profile[profile] = entry

        # 呼び出した順を保つ / preserve the order the template declared
        results = [by_profile[profile] for profile in profiles]
        failed = [r for r in results if not r["ok"]]

        if len(failed) == len(results):
            detail = "; ".join(f"{r['profile']}: {r['error']}" for r in failed)
            raise RuntimeError(
                f"すべての提供元が失敗しました / every profile failed: {detail}"
            )

        return {"results": results, "count": len(results) - len(failed),
                "failed": len(failed)}

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
