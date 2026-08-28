"""YAML → Template の読み込みと検証。

検証はロード時に済ませる。実行が始まってから
「そんなステップ ID は無い」と落ちるのが一番たちが悪いため。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .expr import PLACEHOLDER
from .schema import (
    AgentSpec,
    LLMSpec,
    OutputFormat,
    RetrySpec,
    Step,
    StepKind,
    Template,
    TriggerSpec,
)

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class TemplateError(Exception):
    """テンプレートの記述が不正。ユーザーに見せる前提のメッセージを持つ。"""


def load_file(path: str | Path) -> Template:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TemplateError(f"{path}: トップレベルはマッピングである必要があります")
    return load_dict(raw, source=str(path))


def load_dict(raw: dict[str, Any], source: str = "<inline>") -> Template:
    if "name" not in raw:
        raise TemplateError(f"{source}: name は必須です")
    if not raw.get("steps"):
        raise TemplateError(f"{source}: steps が空です")

    template = Template(
        name=str(raw["name"]),
        version=str(raw.get("version", "1")),
        industry=str(raw.get("industry", "generic")),
        description=str(raw.get("description", "")),
        trigger=_parse_trigger(raw.get("trigger"), source),
        params=dict(raw.get("params") or {}),
        steps=[_parse_step(s, i, source) for i, s in enumerate(raw["steps"])],
    )
    _validate_references(template, source)
    return template


def _parse_trigger(raw: Any, source: str) -> TriggerSpec:
    if raw is None:
        return TriggerSpec()
    if isinstance(raw, str):
        # 短縮記法: "schedule:0 9 * * MON-FRI" / "event:teams:meeting_ended"
        kind, _, rest = raw.partition(":")
        if kind == "schedule":
            return TriggerSpec(type="schedule", cron=rest)
        if kind == "event":
            return TriggerSpec(type="event", event=rest)
        if kind == "manual":
            return TriggerSpec(type="manual")
        raise TemplateError(f"{source}: 不明な trigger 種別 '{kind}'")
    if isinstance(raw, dict):
        return TriggerSpec(
            type=raw.get("type", "manual"),
            cron=raw.get("cron"),
            event=raw.get("event"),
            timezone=raw.get("timezone", "Asia/Tokyo"),
        )
    raise TemplateError(f"{source}: trigger の形式が不正です")


def _parse_step(raw: Any, index: int, source: str) -> Step:
    where = f"{source}: steps[{index}]"
    if not isinstance(raw, dict):
        raise TemplateError(f"{where}: マッピングである必要があります")

    step_id = raw.get("id")
    if not step_id or not ID_RE.match(str(step_id)):
        raise TemplateError(
            f"{where}: id は英小文字で始まる snake_case にしてください (現在: {step_id!r})"
        )

    kind = _infer_kind(raw, where)

    step = Step(
        id=str(step_id),
        kind=kind,
        adapter=raw.get("adapter"),
        action=raw.get("action"),
        prompt=raw.get("prompt"),
        prompt_inline=raw.get("prompt_inline"),
        expression=raw.get("expression"),
        for_each=raw.get("for_each"),
        as_name=str(raw.get("as", "item")),
        where=raw.get("where"),
        inputs=dict(raw.get("inputs") or {}),
        config=dict(raw.get("config") or {}),
        output_schema=raw.get("output_schema"),
        when=raw.get("when"),
        continue_on_error=bool(raw.get("continue_on_error", False)),
    )

    fmt = raw.get("output_format", "text")
    try:
        step.output_format = OutputFormat(fmt)
    except ValueError:
        raise TemplateError(f"{where}: output_format は text / json のいずれかです") from None

    if kind is StepKind.LLM:
        llm_raw = raw.get("llm")
        if isinstance(llm_raw, str):
            step.llm = LLMSpec(profile=llm_raw)
        elif isinstance(llm_raw, dict):
            step.llm = LLMSpec(
                profile=llm_raw.get("profile", "default"),
                temperature=float(llm_raw.get("temperature", 0.2)),
                max_tokens=int(llm_raw.get("max_tokens", 4096)),
            )
        else:
            step.llm = LLMSpec()
        if not (step.prompt or step.prompt_inline):
            raise TemplateError(f"{where}: LLM ステップには prompt が必要です")

    if kind is StepKind.AGENT:
        agent_raw = raw.get("agent")
        agent_raw = dict(agent_raw) if isinstance(agent_raw, dict) else {}
        tools = agent_raw.get("tools") or []
        if isinstance(tools, str):
            tools = [tools]
        if not tools:
            raise TemplateError(
                f"{where}: agent には tools の列挙が必要です "
                f"/ an agent step must enumerate its tools"
            )
        step.agent = AgentSpec(
            tools=[str(x) for x in tools],
            allow_writes=bool(agent_raw.get("allow_writes", False)),
            max_iterations=int(agent_raw.get("max_iterations", 5)),
            max_tokens_total=int(agent_raw.get("max_tokens_total", 60000)),
        )
        if step.agent.max_iterations < 1 or step.agent.max_iterations > 25:
            raise TemplateError(
                f"{where}: max_iterations は 1〜25 にしてください "
                f"/ max_iterations must be between 1 and 25"
            )
        if not (step.prompt or step.prompt_inline):
            raise TemplateError(
                f"{where}: agent には prompt が必要です / an agent step needs a prompt"
            )
        llm_raw = raw.get("llm")
        if isinstance(llm_raw, str):
            step.llm = LLMSpec(profile=llm_raw)
        elif isinstance(llm_raw, dict):
            step.llm = LLMSpec(
                profile=llm_raw.get("profile", "default"),
                temperature=float(llm_raw.get("temperature", 0.2)),
                max_tokens=int(llm_raw.get("max_tokens", 4096)),
            )
        else:
            step.llm = LLMSpec()

    if step.for_each is not None:
        max_items = raw.get("max_items", 50)
        if not isinstance(max_items, int) or not 1 <= max_items <= 500:
            raise TemplateError(
                f"{where}: max_items は 1〜500 にしてください "
                f"/ max_items must be between 1 and 500"
            )
        step.max_items = max_items
        if step.as_name in ("steps", "params", "run", "trigger"):
            raise TemplateError(
                f"{where}: as に '{step.as_name}' は使えません "
                f"/ '{step.as_name}' is reserved"
            )

    if step.where is not None and step.for_each is None:
        raise TemplateError(
            f"{where}: where は for_each と一緒にしか使えません "
            f"/ where requires for_each"
        )

    if kind is StepKind.ADAPTER and not step.action:
        raise TemplateError(f"{where}: adapter ステップには action が必要です")

    retry_raw = raw.get("retry")
    if isinstance(retry_raw, dict):
        step.retry = RetrySpec(
            max_attempts=int(retry_raw.get("max_attempts", 1)),
            backoff_seconds=float(retry_raw.get("backoff_seconds", 2.0)),
        )
    elif isinstance(retry_raw, int):
        step.retry = RetrySpec(max_attempts=retry_raw)

    return step


def _infer_kind(raw: dict[str, Any], where: str) -> StepKind:
    """明示の kind があればそれを、無ければキーの存在から推論する。

    テンプレートを書く人に kind: を毎回書かせるのは冗長なので、
    adapter / llm / expression のどれがあるかで判定する。
    """
    if "kind" in raw:
        try:
            return StepKind(raw["kind"])
        except ValueError:
            raise TemplateError(f"{where}: 不明な kind '{raw['kind']}'") from None

    # agent は llm と併記されるので先に見る / agent coexists with llm, so check first
    if "agent" in raw:
        return StepKind.AGENT

    present = [k for k in ("adapter", "llm", "expression") if k in raw]
    if len(present) != 1:
        raise TemplateError(
            f"{where}: adapter / llm / expression / agent のいずれか 1 つを指定してください "
            f"(検出: {present or 'なし'})"
        )
    return {"adapter": StepKind.ADAPTER, "llm": StepKind.LLM,
            "expression": StepKind.TRANSFORM}[present[0]]


def _validate_references(template: Template, source: str) -> None:
    """前方参照と重複 ID を検出する。"""
    seen: set[str] = set()
    for step in template.steps:
        if step.id in seen:
            raise TemplateError(f"{source}: ステップ ID '{step.id}' が重複しています")

        available = {"params", "trigger", "run"} | {f"steps.{s}" for s in seen}
        # 繰り返しの要素名は、実行時にその工程の中でだけ束縛される。
        # 前方参照の検査対象にすると、正しいテンプレートが弾かれる。
        # The loop variable is bound at run time, inside this step only.
        # Treating it as a forward reference would reject valid templates.
        bound = {"params", "trigger", "run"}
        if step.for_each is not None:
            # 要素そのものと、位置情報 / the element itself and its position
            bound = bound | {step.as_name, "loop"}

        for ref in _collect_refs(step):
            root = ref.split(".")[0]
            if root in bound:
                continue
            if root != "steps":
                raise TemplateError(
                    f"{source}: ステップ '{step.id}' の参照 '{ref}' の起点が不正です "
                    f"(params / trigger / run / steps.* のみ)"
                )
            target = ".".join(ref.split(".")[:2])
            if target not in available:
                raise TemplateError(
                    f"{source}: ステップ '{step.id}' が未定義または後方のステップ "
                    f"'{target}' を参照しています"
                )
        seen.add(step.id)


def _collect_refs(step: Step) -> list[str]:
    refs: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            refs.extend(m.group(1).strip() for m in PLACEHOLDER.finditer(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(step.inputs)
    walk(step.config)
    walk(step.for_each)
    walk(step.prompt_inline)
    walk(step.expression)
    if step.where:
        walk(step.where)
        for m in re.finditer(r"([A-Za-z_][\w.]*)", step.where):
            token = m.group(1)
            if token.startswith(("steps.", "params.", "run.", "trigger.")):
                refs.append(token)

    if step.when:
        walk(step.when)
        # when は裸のパス表記も許すため補完
        for m in re.finditer(r"([A-Za-z_][\w.]*)", step.when):
            token = m.group(1)
            if token.startswith(("steps.", "params.", "run.", "trigger.")):
                refs.append(token)
    return refs
