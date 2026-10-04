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
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .side_store import DECISIONS, WBS_PROPOSALS, FileSide, SideStore
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
    # 反映の記録の置き場。ファイルの場所（Path）か、台帳の隣の置き場（SideStore）。
    decisions: Path | SideStore | None = None


class LedgerProposalStore:
    """WBS 変更提案の**扱い**（一覧・承認・却下・反映）を PostgreSQL 無しで行う。

    `fetch`・`decide`・`approve`・`apply_approved`（この下）は、`pg.query(name, params)` /
    `pg.execute(name, params, idempotency_key=...)` という形だけを相手に書いてある
    （PostgresAdapter と区別しない）。この型は同じ形を、台帳の隣（`SideStore`）に置いた
    1つの JSON 文書で実装する——`task_engine.backend: postgres` が無い構成でも、
    `aipmo wbs proposals` がそのまま動く。

    **含まないもの**: `wbs_replan` テンプレートによる提案の**新規作成**は、直前の
    `risk_forecast` 予測スナップショット（`latest_forecast_snapshot` / PostgreSQL の別表）を
    必須とするため、ここでは扱わない——作成は引き続き PostgreSQL が要る。ここが扱うのは
    「すでにある提案を見る・決める・反映する」という、人が日常に触れる側だけ。

    Handles WBS change proposals — listing, approving, rejecting, applying — without
    PostgreSQL. `fetch`/`decide`/`approve`/`apply_approved` (below) only ever call
    `pg.query(name, params)` / `pg.execute(name, params, idempotency_key=...)`, never caring
    whether `pg` is a PostgresAdapter; this class implements the same shape over a single JSON
    document beside the ledger (`SideStore`), so `aipmo wbs proposals` works even without
    `task_engine.backend: postgres`.

    **Not covered**: *creating* a proposal via the `wbs_replan` template still needs
    PostgreSQL, since it requires the latest `risk_forecast` snapshot
    (`latest_forecast_snapshot`, a separate PostgreSQL-only table) to assign a tier. This class
    only covers the side a human actually touches day to day: viewing, deciding, and applying
    proposals that already exist.
    """

    name = "wbs_proposals_ledger"

    def __init__(self, side: SideStore) -> None:
        self.side = side
        self._lock = threading.Lock()   # 同一プロセス内の競合だけ防ぐ（決定は稀なので十分）
                                         # guards same-process races only — decisions are rare enough

    def health_check(self) -> bool:
        return True

    def _read_all(self) -> dict[str, dict[str, Any]]:
        raw = self.side.read_doc(WBS_PROPOSALS)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, rows: dict[str, dict[str, Any]]) -> None:
        self.side.write_doc(WBS_PROPOSALS, json.dumps(rows, ensure_ascii=False))

    def query(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        with self._lock:
            rows = self._read_all()
            if name == "pending_wbs_proposals":
                matched = [dict(r) for r in rows.values()
                          if r.get("tenant") == params.get("tenant")
                          and r.get("status") == "pending"]
                matched.sort(key=lambda r: (r.get("tier") if r.get("tier") is not None else 99,
                                            r.get("wbs_version_from") or "",
                                            r.get("option_label") or "",
                                            r.get("created_at") or ""))
                return {"rows": matched, "count": len(matched)}
            if name == "get_wbs_proposal":
                row = rows.get(params.get("id", ""))
                if row is None or row.get("tenant") != params.get("tenant"):
                    return {"rows": [], "count": 0}
                return {"rows": [dict(row)], "count": 1}
            raise KeyError(f"wbs_proposals_ledger: 未知のクエリ / unknown query: {name}")

    def execute(self, name: str, params: dict[str, Any] | None = None,
               idempotency_key: str | None = None) -> dict[str, Any]:
        params = params or {}
        with self._lock:
            rows = self._read_all()
            if name == "save_wbs_proposal":
                # ON CONFLICT (source_key) DO UPDATE ... WHERE status = 'pending' と同じ:
                # 既存の承認待ち提案と同じ idempotency_key（= source_key）なら上書きし、
                # 決定済みの行には触れない。一致が無ければ新規行。
                # Mirrors ON CONFLICT (source_key) DO UPDATE ... WHERE status = 'pending': a
                # match on idempotency_key (source_key) among pending rows is overwritten;
                # decided rows are left untouched; no match inserts a new row.
                existing = next((r for r in rows.values()
                                 if r.get("source_key") == idempotency_key
                                 and r.get("tenant") == params.get("tenant")), None)
                if existing is not None and existing.get("status") != "pending":
                    return {"affected": 0, "rows": []}
                row_id = existing["id"] if existing is not None else str(uuid.uuid4())
                record = {
                    "id": row_id, "tenant": params.get("tenant"),
                    "wbs_version_from": params.get("wbs_version_from"),
                    "diff": params.get("diff"), "rationale": params.get("rationale"),
                    "assumptions": params.get("assumptions") or {},
                    "tier": params.get("tier"), "confidence": params.get("confidence"),
                    "option_label": params.get("option_label"), "source_key": idempotency_key,
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "decided_by": None, "decided_at": None, "decision_note": None,
                }
                rows[row_id] = record
                self._write_all(rows)
                return {"affected": 1, "rows": [{"id": row_id}]}
            if name == "decide_wbs_proposal":
                row = rows.get(params.get("id", ""))
                if (row is None or row.get("tenant") != params.get("tenant")
                        or row.get("status") != "pending"):
                    return {"affected": 0, "rows": []}
                row = dict(row)
                row["status"] = params["status"]
                row["decided_by"] = params.get("decided_by")
                row["decided_at"] = datetime.now(timezone.utc).isoformat()
                row["decision_note"] = params.get("decision_note")
                rows[row["id"]] = row
                self._write_all(rows)
                return {"affected": 1, "rows": [{"id": row["id"], "status": row["status"]}]}
            raise KeyError(f"wbs_proposals_ledger: 未知のクエリ / unknown query: {name}")


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


def _log_of(decisions: Path | SideStore | None) -> SideStore | None:
    if decisions is None or isinstance(decisions, SideStore):
        return decisions
    return FileSide(decisions, {DECISIONS: decisions})


def applied_before(decisions: Path | SideStore | None, proposal_id: str) -> bool:
    """この提案を、すでに反映したか（判断ログから）。"""
    log = _log_of(decisions)
    if log is None:
        return False
    needle = f'"{APPLIED_KIND}"'
    for line in log.read_log(DECISIONS, 0)[0]:
        if needle in line:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") == APPLIED_KIND and entry.get("proposal") == proposal_id:
                return True
    return False


def _record(target: Target, proposal_id: str, by: str, plan: Plan) -> None:
    log = _log_of(target.decisions)
    if log is None:
        return
    entry = {"at": datetime.now(timezone.utc).isoformat(), "kind": APPLIED_KIND,
             "proposal": proposal_id, "file": str(target.file), "by": by,
             "changes": plan.report}
    log.append(DECISIONS, json.dumps(entry, ensure_ascii=False))


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


__all__ = ["APPLIED_KIND", "LedgerProposalStore", "ProposalError", "Target", "apply_approved",
           "applied_before", "approve", "changes_of", "decide", "fetch", "plan_for",
           "validate_changes"]
