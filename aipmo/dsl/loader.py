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
        steps=[_parse_step(s, f"{source}: steps[{i}]") for i, s in enumerate(raw["steps"])],
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


def _parse_step(raw: Any, where: str) -> Step:
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
        step.llm = _parse_llm_spec(raw.get("llm"), where, allow_profiles=True)
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
            require_approval=bool(agent_raw.get("require_approval", False)),
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
        step.llm = _parse_llm_spec(raw.get("llm"), where, allow_profiles=False)

    if kind is StepKind.PARALLEL:
        if any(k in raw for k in ("adapter", "llm", "expression", "agent")):
            raise TemplateError(
                f"{where}: parallel は adapter / llm / expression / agent と"
                f"同時に指定できません "
                f"/ parallel cannot be combined with adapter, llm, expression or agent"
            )
        if step.for_each is not None:
            raise TemplateError(
                f"{where}: parallel は for_each と組み合わせられません "
                f"/ parallel cannot be combined with for_each"
            )
        parallel_raw = raw.get("parallel")
        if not isinstance(parallel_raw, list) or len(parallel_raw) < 2:
            raise TemplateError(
                f"{where}: parallel には2件以上のステップの並びが必要です "
                f"/ parallel must list two or more steps"
            )
        step.parallel = [
            _parse_step(s, f"{where}.parallel[{j}]") for j, s in enumerate(parallel_raw)
        ]

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
        step.concurrent = bool(raw.get("concurrent", False))
        if step.concurrent and kind is not StepKind.AGENT:
            # adapter / llm ステップでの並行 for_each は、意図しない同時
            # 書き込みを招きかねないので許可しない。agent ステップだけが
            # 独立したサブエージェントとして安全に並行実行できる。
            #
            # Concurrent for_each on an adapter/llm step risks unintended
            # simultaneous writes; only an agent step can safely run its
            # elements as independent, concurrent subagents.
            raise TemplateError(
                f"{where}: concurrent は agent ステップでのみ使えます "
                f"/ concurrent is only allowed on an agent step"
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


def _parse_llm_spec(llm_raw: Any, where: str, allow_profiles: bool) -> LLMSpec:
    if isinstance(llm_raw, str):
        return LLMSpec(profile=llm_raw)
    if not isinstance(llm_raw, dict):
        return LLMSpec()

    profiles_raw = llm_raw.get("profiles")
    profiles: list[str] = []
    if profiles_raw is not None:
        if not allow_profiles:
            raise TemplateError(
                f"{where}: llm.profiles はエージェントステップでは使えません "
                f"/ llm.profiles is not supported on agent steps"
            )
        if "profile" in llm_raw:
            raise TemplateError(
                f"{where}: llm.profile と llm.profiles は同時に指定できません "
                f"/ specify either profile or profiles, not both"
            )
        if not isinstance(profiles_raw, list) or not profiles_raw:
            raise TemplateError(
                f"{where}: llm.profiles は1件以上の並びである必要があります "
                f"/ llm.profiles must be a non-empty list"
            )
        profiles = [str(p) for p in profiles_raw]
        if len(set(profiles)) != len(profiles):
            raise TemplateError(
                f"{where}: llm.profiles に重複があります / duplicate profiles"
            )

    return LLMSpec(
        profile=llm_raw.get("profile", "default"),
        profiles=profiles,
        temperature=float(llm_raw.get("temperature", 0.2)),
        max_tokens=int(llm_raw.get("max_tokens", 4096)),
    )


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
    structural = [k for k in ("agent", "parallel") if k in raw]
    if len(structural) > 1:
        raise TemplateError(
            f"{where}: agent と parallel は同時に指定できません "
            f"/ agent and parallel cannot both be specified"
        )
    if structural:
        return {"agent": StepKind.AGENT, "parallel": StepKind.PARALLEL}[structural[0]]

    present = [k for k in ("adapter", "llm", "expression") if k in raw]
    if len(present) != 1:
        raise TemplateError(
            f"{where}: adapter / llm / expression / agent / parallel のいずれか "
            f"1 つを指定してください (検出: {present or 'なし'})"
        )
    return {"adapter": StepKind.ADAPTER, "llm": StepKind.LLM,
            "expression": StepKind.TRANSFORM}[present[0]]


def _validate_references(template: Template, source: str) -> None:
    """前方参照と重複 ID を検出する。

    重複検出用の declared と、参照先として見えるかどうかの visible を分けて
    持つ。並列グループの中では、宣言と同時に declared へ積んで重複だけは
    即座に検出しつつ、グループ全体が終わるまで visible には積まない —
    同時に走る工程どうしは互いの出力を参照できないため。

    Two sets are tracked: `declared` catches duplicate ids the moment a step is
    seen, while `visible` gates forward references. Inside a parallel group,
    siblings are added to `declared` immediately (so duplicates among them are
    still caught) but withheld from `visible` until the whole group is done —
    steps running at the same time cannot reference each other's output.
    """
    declared: set[str] = set()
    visible: set[str] = set()
    for step in template.steps:
        visible |= _check_step(step, declared, visible, source)


def _check_step(step: Step, declared: set[str], visible: set[str],
                source: str) -> set[str]:
    """このステップ（とあれば中の全ステップ）を検証し、検証が終わった後に
    visible へ加えるべき ID の集まりを返す。

    visible そのものはここでは書き換えない。並列グループの中を再帰する間、
    このステップに渡された visible は最後まで変わらないので、兄弟どうしは
    互いを検証結果に含められない — 呼び出し元（グループの外側）が全員分の
    戻り値をまとめてから、一度に visible へ加える。

    Does not mutate `visible` itself. While recursing through a parallel
    group, the `visible` a step sees stays fixed for the whole call, so
    siblings cannot end up in each other's result — only the caller (outside
    the group) merges everyone's return value into `visible`, all at once.
    """
    if step.id in declared:
        raise TemplateError(f"{source}: ステップ ID '{step.id}' が重複しています")
    declared.add(step.id)

    available = {"params", "trigger", "run"} | {f"steps.{s}" for s in visible}
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

    if step.kind is StepKind.PARALLEL:
        revealed = {step.id}
        for nested in step.parallel:
            revealed |= _check_step(nested, declared, visible, source)
        return revealed
    return {step.id}


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
