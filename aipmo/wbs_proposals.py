"""承認された WBS 変更提案（wbs_replan）を、WBS ファイルへ反映する。

`wbs_replan` は、WBS 再計画 AI の提案を PostgreSQL の `wbs_replan_proposals` に**承認待ち**で
記録する。これまで、人が承認しても行の状態が変わるだけで、何にも反映されなかった。ここは
その続き：提案の `diff` が**決まった形の変更の一覧**（`diff["changes"]`、aipmo/wbs_edit.py）なら、
承認と同時に WBS ファイルへ反映する。

順序（承認は取り消せないので、確かめられることは先に確かめる）：

1. 提案を読み、まだ承認待ちか確かめる。
2. 変更を**反映後の内容まで作って検証する**（ファイルは書かない）。誤りがあれば、提案は
   承認待ちのまま、理由を返して止まる。
3. 提案を承認にする（承認待ちのときだけ。競合したら止まる）。
4. WBS ファイルへ書く（読んだ後に書き換えられていたら書かない）。

4 で失敗しても、承認は取り消さない — `apply` で、承認済みの提案を反映し直せる。反映は
「その値にする」という指定なので、何度やっても同じ。ただし、**一度反映した提案を、あとから
もう一度反映して人の後の修正を戻してしまう**ことは、反映の記録（判断ログ）で止める（`force` で上書き）。

`diff` が決まった形でない提案（自由な文章の再計画案）は、これまでどおり承認の記録だけで、
ファイルは変えない。

When a human approves a replan proposal whose `diff` carries a fixed-shape change list, the WBS
file is updated. Order: read and check pending; build and validate the *result* (no write —
on any problem the proposal stays pending); approve (only if still pending); write (refused if
the file changed since it was read). If the write fails the approval stands and `apply` retries.
Re-applying an already-applied proposal (which would undo a later human edit) is refused unless
forced. Free-form proposals only record the decision.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .wbs_edit import Plan, WbsEditError, plan_changes, validate_changes, write_plan

APPLIED_KIND = "wbs_proposal_applied"


class ProposalError(RuntimeError):
    """`kind`: not_found | not_pending | not_approved | invalid | conflict | applied."""

    def __init__(self, message: str, kind: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.problems = problems or [message]


@dataclass(frozen=True)
class Target:
    """反映先。`decisions` は反映の記録（判断ログ）。無ければ記録しない。"""
    file: Path
    root: Path
    decisions: Path | None = None


def changes_of(row: dict[str, Any]) -> Any | None:
    """提案の `diff` から、決まった形の変更の一覧を取り出す。無ければ None（自由な形）。"""
    diff = row.get("diff")
    if isinstance(diff, str):
        try:
            diff = json.loads(diff)
        except ValueError:
            return None
    if isinstance(diff, dict) and "changes" in diff:
        return diff["changes"]
    return None


def fetch(pg: Any, tenant: str, proposal_id: str) -> dict[str, Any]:
    result = pg.query("get_wbs_proposal", {"tenant": tenant, "id": proposal_id})
    if not result["rows"]:
        raise ProposalError(f"提案 {proposal_id} が見つかりません / no such proposal", "not_found")
    return dict(result["rows"][0])


def plan_for(row: dict[str, Any], target: Target, *, as_of: date | None = None) -> Plan | None:
    """提案の反映計画（ファイルは書かない）。決まった形の変更が無ければ None。"""
    raw = changes_of(row)
    if raw is None:
        return None
    try:
        return plan_changes(target.file, target.root, raw, as_of=as_of,
                            expect_wbs_id=row.get("wbs_version_from"))
    except WbsEditError as exc:
        raise ProposalError("この提案は WBS ファイルに反映できません: " + "; ".join(exc.problems),
                            "invalid", exc.problems) from exc


def applied_before(decisions: Path | None, proposal_id: str) -> bool:
    """この提案を、すでに反映したか（判断ログから）。"""
    if decisions is None or not decisions.exists():
        return False
    needle = f'"{APPLIED_KIND}"'
    for line in decisions.read_text(encoding="utf-8", errors="replace").splitlines():
        if needle in line:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") == APPLIED_KIND and entry.get("proposal") == proposal_id:
                return True
    return False


def _record(target: Target, proposal_id: str, by: str, plan: Plan) -> None:
    if target.decisions is None:
        return
    entry = {"at": datetime.now(timezone.utc).isoformat(), "kind": APPLIED_KIND,
             "proposal": proposal_id, "file": str(target.file), "by": by,
             "changes": plan.report}
    target.decisions.parent.mkdir(parents=True, exist_ok=True)
    with target.decisions.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def decide(pg: Any, tenant: str, proposal_id: str, status: str, by: str,
           note: str | None) -> dict[str, Any]:
    result = pg.execute("decide_wbs_proposal", {
        "tenant": tenant, "id": proposal_id, "status": status,
        "decided_by": by, "decision_note": note})
    if not result["rows"]:
        raise ProposalError("提案は承認待ちではありません（決定済み・存在しない・失効） "
                            "/ proposal is not pending", "not_pending")
    return dict(result["rows"][0])


def approve(pg: Any, tenant: str, proposal_id: str, by: str, note: str | None,
            target: Target | None, *, as_of: date | None = None) -> dict[str, Any]:
    """提案を承認し、決まった形の変更なら WBS ファイルへ反映する。

    戻り値: {id, status, applied: True|False|None, report, error}。
    `applied` が None は「反映の対象ではない」（自由な形、または反映先が未設定）。
    """
    row = fetch(pg, tenant, proposal_id)
    if row.get("status") != "pending":
        raise ProposalError(f"提案は承認待ちではありません（{row.get('status')}）"
                            f" / not pending", "not_pending")
    plan = plan_for(row, target, as_of=as_of) if target is not None else None
    decided = decide(pg, tenant, proposal_id, "approved", by, note)
    out: dict[str, Any] = {**decided, "applied": None, "report": [], "error": None}
    if plan is None:
        return out
    out["report"] = plan.report
    try:
        assert target is not None
        write_plan(plan)
        _record(target, proposal_id, by, plan)
        out["applied"] = True
    except WbsEditError as exc:
        out["applied"] = False
        out["error"] = "; ".join(exc.problems) + "（承認は済んでいます。確かめてから apply で反映し直せます）"
    return out


approve_and_apply = approve


def apply_approved(pg: Any, tenant: str, proposal_id: str, by: str, target: Target, *,
                   force: bool = False, as_of: date | None = None) -> dict[str, Any]:
    """承認済みの提案を、WBS ファイルへ反映（し直）す。"""
    row = fetch(pg, tenant, proposal_id)
    if row.get("status") != "approved":
        raise ProposalError(f"承認済みの提案だけを反映できます（この提案は {row.get('status')}）"
                            f" / only an approved proposal can be applied", "not_approved")
    if applied_before(target.decisions, proposal_id) and not force:
        raise ProposalError(
            "この提案はすでに反映済みです。もう一度反映すると、その後の人の修正を戻すかもしれません"
            "（それでもよければ --force） / already applied; re-applying could undo later edits",
            "applied")
    plan = plan_for(row, target, as_of=as_of)
    if plan is None:
        raise ProposalError("この提案には、反映できる決まった形の変更（diff.changes）がありません "
                            "/ no machine-applicable changes", "invalid")
    try:
        write_plan(plan)
    except WbsEditError as exc:
        raise ProposalError("反映できません: " + "; ".join(exc.problems), "conflict",
                            exc.problems) from exc
    _record(target, proposal_id, by, plan)
    return {"id": proposal_id, "status": "approved", "applied": True,
            "report": plan.report, "error": None, "changed": plan.changed}


__all__ = ["APPLIED_KIND", "ProposalError", "Target", "apply_approved", "applied_before",
           "approve", "changes_of", "decide", "fetch", "plan_for", "validate_changes"]
