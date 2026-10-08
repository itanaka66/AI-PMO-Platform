"""自己学習サイクルが提出したナレッジ候補の、オプトインの自動承認。

`templates/examples/self_learning_cycle.yaml` は、ローカル LLM で架空の
（実在しない）課題を作り、同じモデルに自己判断させ、別のモデルで検証し、
`vector_store.submit_candidate` でナレッジ候補として提出する——これを
`aipmo schedule` が繰り返す。提出された候補は、ほかの経路（たとえば
`generalize_knowledge`）が作るものとまったく同じ、承認待ちの private な
行で、既定では今までどおり人間が `aipmo knowledge` / Web の「ナレッジ」で
決める（WBS 6.38）。

ここにあるのは、それとは別の、オプトインの仕組み：運用者が「RAG を
包括的に信用する」と明示的に選んだとき（制御の文書の `learning_trust_rag`。
CLI の `aipmo learning enable` か Web のチェックボックスで立てる）だけ、
**この自己学習サイクルが提出した候補に限って**自動で承認する。ほかの
経路から提出された候補には一切触れない。いつでも無効化でき
（`aipmo learning disable`）、無効化後は新しい候補の自動承認が止まるだけで、
それまでに自動承認した判断（誰が・いつ・なぜ）は私有コレクションの行に
記録されたまま消えない——「人間の判断の記録」という原則は、ここでも
人間が『信用する』と判断したこと自体を記録する形で保たれる。

Opt-in auto-approval for knowledge candidates the self-learning cycle
submits. `templates/examples/self_learning_cycle.yaml` uses a local LLM to
invent a fictional task, has the same model judge it, a different model
verify that judgment, then submits the result as a knowledge candidate via
`vector_store.submit_candidate` — repeated by `aipmo schedule`. What lands
in the private collection is an ordinary pending row, identical to one
`generalize_knowledge` or any other template would produce, and by default
still awaits a human via `aipmo knowledge` / the web "Knowledge" screen
(WBS 6.38).

What lives here is a separate, opt-in mechanism: only when an operator
explicitly chooses to "comprehensively trust the RAG" (`learning_trust_rag`
in the control document, set via `aipmo learning enable` or a web checkbox)
are candidates *submitted by this specific self-learning cycle* auto-
approved — never candidates from any other source. It can be turned off at
any time (`aipmo learning disable`); turning it off only stops new
auto-approvals, the record of who decided to trust it (and when) stays on
the already-decided rows, preserving the same "the record is the decision"
principle this entire review workflow rests on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .judgment import read_control, write_control
from .side_store import SideStore

logger = logging.getLogger("aipmo.self_learning")

TEMPLATE_NAME = "self_learning_cycle"
REVIEWER = "auto:self_learning (operator opted in via learning_trust_rag)"


def trust_rag_enabled(control_target: Path | SideStore) -> bool:
    """運用者が「RAG を包括的に信用する」と選んでいるか。"""
    return bool(read_control(control_target).get("learning_trust_rag"))


def set_trust_rag(control_target: Path | SideStore, enabled: bool) -> dict[str, Any]:
    """オプトインの切り替え。いつでも無効化できる。"""
    stamp = datetime.now(timezone.utc).isoformat()
    key = "learning_trust_rag_enabled_at" if enabled else "learning_trust_rag_disabled_at"
    return write_control(control_target, learning_trust_rag=enabled, **{key: stamp})


def _submitted_candidate_ids(ctx: Any) -> list[tuple[str, str]]:
    """この実行の各ステップの出力から、提出された候補の (アダプタ名, id) を集める。

    `submit_candidate` の出力は `{"id": ..., "review_status": "pending", ...}`
    という形——このテンプレートだけが従う決め事ではなく、アダプタ側が
    必ずこの形で返すので、どのステップがそれを呼んだかだけを見ればよい。
    """
    found: list[tuple[str, str]] = []
    for step_id, result in ctx.results.items():
        output = result.output
        if (result.status == "success" and isinstance(output, dict)
                and output.get("review_status") == "pending" and output.get("id")):
            adapter_name = ctx.step_adapters.get(step_id)
            if adapter_name:
                found.append((adapter_name, str(output["id"])))
    return found


def attach(engine: Any, control_target: Path | SideStore) -> None:
    """Engine の完了フックとして登録する。

    `learning_trust_rag` が有効なときだけ、かつ `self_learning_cycle`
    テンプレートの実行だけを対象にする——ほかのテンプレートが提出した候補は、
    このフックが何を提出したか分かっても一切触れない。
    """

    def on_run_complete(template_name: str, ctx: Any) -> None:
        if template_name != TEMPLATE_NAME:
            return
        try:
            if not trust_rag_enabled(control_target):
                return
            for adapter_name, candidate_id in _submitted_candidate_ids(ctx):
                if not engine.adapters.has(adapter_name):
                    continue
                adapter = engine.adapters.get(adapter_name)
                decide = getattr(adapter, "decide_candidate", None)
                if decide is None:
                    continue
                result = decide(candidate_id, approve=True, reviewer=REVIEWER)
                logger.info("self_learning: auto-approved %s (%s)", candidate_id, result)
        except Exception:
            logger.warning("self_learning: auto-approval failed", exc_info=True)

    engine.run_listeners.append(on_run_complete)
