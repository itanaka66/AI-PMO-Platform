"""受信箱 — 人の判断を待っているものを、1 か所に集める。

PMO Core は、人が決めるまで動かない仕組みを、いくつも持っている：

| 種類 (`kind`) | 何か | 決める操作 |
|---|---|---|
| `judgment` | 自律的な判断の提案（診断 → 対処） | 承認／却下（実行は常駐が、承認のあと） |
| `followup` | 続く警告から出た、対応タスクの提案 | 承認／却下 |
| `wbs` | WBS の更新漏れ・証拠の欠けの提案 | 承認／却下（WBS ファイルは変わらない） |
| `assignment` | 担当の提案 | 確定 |
| `review` | 役割AIの成果（人のレビュー待ち） | 認める／差し戻す（差し戻しは理由が必須） |
| `filing` | 承認済みで、課題管理ツールに未起票のタスク | 起票／見送り |
| `replan` | WBS 再計画 AI の提案（PostgreSQL） | 承認／却下 |

これまでは、種類ごとに別の場所に出ていた。ここは**読み出すだけ**で（台帳から今の状態を集める）、
何かを決めたり書いたりはしない。決める操作は、各項目の `actions` が指す**既存の API**（`/api/pmo/...`）
で行う — 書き込みの入口を増やさないため、権限（operator のみ）も確認も、これまでと同じ。

- 並びは `urgency`（高いほど先）、同じなら古いものが先。
- viewer には `actions` を返さない（`can_act: false`）。プロジェクトを限定された viewer には、
  プロジェクトを持たない組織全体の項目（判断・起票・再計画）を出さない。
- 項目ごとの `detail.sections` は、「なぜこれが出たか」「承認すると何が起きるか」「しないこと」を、
  画面が種類を知らなくても描けるように持つ。

The inbox gathers everything waiting for a human decision into one list. Read-only: it never decides or
writes; each item's `actions` point at the existing endpoints, so permissions and confirmations are
unchanged. Ordered by urgency then age; viewers get no actions; a confined viewer sees no org-wide items.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .filing import eligible, filing_state
from .messages import (MESSAGES, is_japanese, localized, render, suggestion_reason, task_title,
                       translator)
from .task_engine import Task, TaskEngine, filed_key

KINDS = ("judgment", "followup", "wbs", "assignment", "review", "filing", "replan")

# 項目の並び（緊急度の目安）。診断の重大度・提案の種類・再計画の tier から決める。
URGENCY = {"followup": 70, "wbs": 50, "assignment": 40, "review": 55, "filing": 30}

# 文章はサーバーの言語で組み立てる（aipmo/messages.py）。台帳に保存された文章（診断・理由・タイトル）は
# 書いた時点の言語のまま出る。
# Sentences are composed in the server's language; text stored in the ledger stays as written.
Translate = Any


def _age(stamp: str, now: datetime) -> int:
    try:
        return max(0, int((now - datetime.fromisoformat(stamp)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"method": "POST", "path": path, "body": body}


def _action(action_id: str, label: str, style: str, path: str, body: dict[str, Any],
            needs_note: bool = False) -> dict[str, Any]:
    return {"id": action_id, "label": label, "style": style, **_post(path, body),
            "needs_note": needs_note}


def _section(heading: str, lines: list[str], tone: str = "info") -> dict[str, Any]:
    """`tone`: info（説明）／do（決めると起きること）／dont（起きないこと・結果）。画面の色分けに使う。"""
    return {"heading": heading, "lines": [line for line in lines if line], "tone": tone}


def _decide_actions(t: Translate, ref: str) -> list[dict[str, Any]]:
    path = "/api/pmo/proposals/decide"
    return [_action("approve", t("act_approve"), "primary", path, {"ref": ref, "decision": "approve"}),
            _action("reject", t("act_reject"), "danger", path, {"ref": ref, "decision": "reject"})]


def _item(kind: str, ref: str, title: str, summary: str, *, project: str, urgency: int,
          created_at: str, now: datetime, sections: list[dict[str, Any]],
          actions: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": f"{kind}:{ref}", "kind": kind, "ref": ref, "title": title, "summary": summary,
            "project": project, "urgency": urgency, "created_at": created_at,
            "age_seconds": _age(created_at, now),
            "detail": {"sections": [s for s in sections if s["lines"]]},
            "actions": actions, **(extra or {})}


# -- 種類ごとの項目 / one builder per kind --------------------------------------------------

def _judgment(t: Translate, lang: str | None, task: Task, now: datetime) -> dict[str, Any]:
    payload = task.payload or {}
    diagnosis = payload.get("diagnosis") or {}
    remedy = str(payload.get("remedy") or "")
    label = t(f"remedy_{remedy}") if f"remedy_{remedy}" in MESSAGES else remedy
    effect = t(f"effect_{remedy}") if f"effect_{remedy}" in MESSAGES else ""
    node = diagnosis.get("i18n") or {}
    title = localized(lang, str(diagnosis.get("title") or task.title), node.get("title"))
    if node.get("evidence") and not is_japanese(lang):      # 診断の根拠も、構造があれば選んだ言語で
        evidence = [render(lang, e) for e in node["evidence"]]
    else:
        evidence = [str(line) for line in diagnosis.get("evidence") or []]
    sections = [
        _section(t("j_evidence"), evidence),
        _section(t("j_remedy"), [label, effect]),
        _section(t("j_why"), [localized(lang, str(payload.get("rationale") or ""),
                                        (payload.get("i18n") or {}).get("rationale"))]),
        _section(t("j_approve_h"), [t("j_approve_1"), t("j_approve_2")], "do"),
        _section(t("j_dont_h"), [t("j_dont_1"), t("j_dont_2")], "dont"),
        _section(t("j_reject_h"), [t("j_reject_1")], "dont"),
    ]
    return _item("judgment", task.id, title, t("j_summary", remedy=label),
                 project=task.project, urgency=int(diagnosis.get("severity") or 60),
                 created_at=task.first_seen, now=now, sections=sections,
                 actions=_decide_actions(t, task.id),
                 extra={"remedy": remedy, "diagnosis_kind": diagnosis.get("kind")})


def _proposal(t: Translate, lang: str | None, task: Task, now: datetime) -> dict[str, Any]:
    is_wbs = task.id.startswith("PMO:wb:")
    kind = "wbs" if is_wbs else "followup"
    source = task.generated_from or ""
    if is_wbs:
        code, _, node = source.removeprefix("wbs:").partition(":")
        sections = [
            _section(t("p_trigger"), [t("w_trigger", code=code, node=node)]),
            _section(t("j_approve_h"), [t("w_approve_1")], "do"),
            _section(t("j_dont_h"), [t("w_dont_1")], "dont"),
            _section(t("j_reject_h"), [t("w_reject_1")], "dont"),
        ]
        urgency = 60 if code == "evidence_missing" else URGENCY["wbs"]
    else:
        rule = source.removeprefix("alert:").partition("|")[0]
        sections = [
            _section(t("p_trigger"), [t("f_trigger", rule=rule) if rule else ""]),
            _section(t("f_prio"), [t("f_prio_line", priority=task.priority or "-", due=task.due_date or "-")]),
            _section(t("j_approve_h"), [t("f_approve_1")], "do"),
            _section(t("j_reject_h"), [t("f_reject_1")], "dont"),
        ]
        urgency = URGENCY["followup"]
    return _item(kind, task.id, task_title(lang, task.title, task.payload),
                 t("p_summary", priority=task.priority or "-"),
                 project=task.project, urgency=urgency, created_at=task.first_seen, now=now,
                 sections=sections, actions=_decide_actions(t, task.id))


def _assignment(t: Translate, lang: str | None, task: Task, now: datetime, writeback: bool,
                tracker: str) -> dict[str, Any]:
    who = task.suggested_assignee or ""
    sections = [
        _section(t("a_reason"), [suggestion_reason(lang, task.suggestion_reason, task.payload)]),
        _section(t("a_task"), [t("a_task_line", key=task.key or task.id, priority=task.priority or "-",
                                 due=task.due_date or "-", score=task.score)]),
        _section(t("a_confirm_h"), [t("a_confirm_1", who=who)]
                 + ([t("a_confirm_2", tracker=tracker)] if writeback else []), "do"),
    ]
    return _item("assignment", task.id, task_title(lang, task.title, task.payload),
                 t("j_summary", remedy=who), project=task.project,
                 urgency=URGENCY["assignment"] + min(20, task.score // 5),
                 created_at=task.first_seen, now=now, sections=sections,
                 actions=[_action("confirm", t("a_action"), "primary", "/api/pmo/assignments/accept",
                                  {"ref": task.id, "writeback": writeback})],
                 extra={"suggested_assignee": who})


def _review(t: Translate, entry: dict[str, Any], now: datetime) -> dict[str, Any]:
    task_id = str(entry["task"])
    sections = [
        _section(t("r_result"), [str(entry.get("excerpt") or t("r_none"))]),
        _section(t("r_run"), [t("r_run_line", agent=entry.get("agent"), dispatch=entry.get("dispatch"))]),
        _section(t("r_accept_h"), [t("r_accept_1")], "do"),
        _section(t("r_reject_h"), [t("r_reject_1")], "dont"),
    ]
    path = "/api/pmo/agents/review"
    base = {"ref": task_id, "dispatch": entry.get("dispatch")}
    return _item("review", f"{task_id}#{entry.get('dispatch')}", str(entry["title"]),
                 t("r_summary", agent=entry.get("agent")), project=str(entry.get("project") or ""),
                 urgency=URGENCY["review"], created_at=str(entry.get("finished_at") or ""), now=now,
                 sections=sections,
                 actions=[_action("accept", t("r_act_accept"), "primary", path, {**base, "decision": "accept"}),
                          _action("reject", t("r_act_reject"), "danger", path, {**base, "decision": "reject"},
                                  needs_note=True)],
                 extra={"agent": entry.get("agent")})


def _filing(t: Translate, task: Task, tracker: str, can_file: bool, now: datetime) -> dict[str, Any]:
    state = filing_state(task)
    origin = {"recurring": t("g_origin_recurring"), "followup": t("g_origin_followup")}.get(
        task.origin, task.origin)
    sections = [
        _section(t("g_target"), [tracker]),
        _section(t("g_origin"), [origin]),
        _section(t("g_assignee"), [task.assignee or t("g_unassigned")]),
        _section(t("g_failed"), [str(state.get("error") or "")] if state.get("state") == "failed" else []),
        _section(t("g_file_h"), [t("g_file_1"), t("g_file_2")], "do"),
    ]
    path = "/api/pmo/filing"
    actions = []
    if can_file:
        actions.append(_action("file", t("g_act_file"), "primary", path, {"ref": task.id, "decision": "file"}))
    actions.append(_action("skip", t("g_act_skip"), "secondary", path, {"ref": task.id, "decision": "skip"}))
    return _item("filing", task.id, task.title, t("g_summary", tracker=tracker), project=task.project,
                 urgency=URGENCY["filing"], created_at=task.first_seen, now=now, sections=sections,
                 actions=actions, extra={"can_file": can_file})


def _replan(t: Translate, row: dict[str, Any], now: datetime) -> dict[str, Any]:
    diff = row.get("diff")
    changes = diff.get("changes") if isinstance(diff, dict) else None
    label = f" [{row['option_label']}]" if row.get("option_label") else ""
    sections = [
        _section(t("l_reason"), [str(row.get("rationale") or "")]),
        _section(t("l_content"), [t("l_content_changes", n=len(changes)) if isinstance(changes, list)
                                 else t("l_content_free")]),
        _section(t("j_approve_h"), [t("l_approve_changes") if isinstance(changes, list)
                                   else t("l_approve_free")], "do"),
    ]
    base = f"/api/wbs-proposals/{row['id']}"
    tier = int(row.get("tier") or 2)
    return _item("replan", str(row["id"]), t("l_title", label=label, wbs=row.get("wbs_version_from")),
                 t("l_summary", tier=tier, confidence=row.get("confidence")), project="",
                 urgency={1: 45, 2: 75, 3: 95}.get(tier, 75), created_at=str(row.get("created_at") or ""),
                 now=now, sections=sections,
                 actions=[_action("approve", t("act_approve"), "primary", f"{base}/approve", {}),
                          _action("reject", t("act_reject"), "danger", f"{base}/reject", {})])


# -- 集める / gathering ---------------------------------------------------------------------

def build_inbox(engine: TaskEngine, *, members: list[Any] | None = None, filing: Any = None,
                can_act: bool = True, allowed: set[str] | None = None, confined: bool = False,
                writable: set[str] | None = None, can_file: bool = False,
                replans: list[dict[str, Any]] | None = None,
                now: datetime | None = None, lang: str | None = "ja") -> dict[str, Any]:
    """台帳から、人の判断を待っているものを集める（読むだけ）。

    `allowed` はこの呼び出しが見てよいプロジェクト（小文字）、`confined` は閲覧者がプロジェクトに
    限定されているとき。`writable` は担当を書き戻せるトラッカー、`can_file` は起票先のアダプタがあるか。
    `lang` は、サーバーが組み立てる文章の言語。
    """
    # 循環 import を避けるため、ここで読む
    from .pmo_core import PmoCore

    t = translator(lang)
    now = now or datetime.now(timezone.utc)
    engine.sync()
    writable = writable or set()

    def visible(project: str) -> bool:
        if allowed is None:
            return True
        return bool(project) and project.lower() in allowed

    items: list[dict[str, Any]] = []
    for task in engine.proposals():
        if task.origin == "judgment":
            if not confined and visible(task.project):
                items.append(_judgment(t, lang, task, now))
        elif visible(task.project):
            items.append(_proposal(t, lang, task, now))

    for task in engine.tasks.values():
        if (task.suggested_assignee and not task.assignee and not task.done and not task.proposed
                and task.origin != "judgment" and visible(task.project)):
            tracker = task.tracker or ("jira" if str(task.id).startswith("JIRA:") else "")
            writeback = bool(tracker and tracker in writable
                             and (task.external_id or (task.key if tracker == "jira" else "")))
            items.append(_assignment(t, lang, task, now, writeback, tracker))

    if members and any(getattr(m, "is_agent", False) for m in members):
        core = PmoCore(task_engine=engine, members=members)
        for entry in core.reviews_pending(engine.ranked()):
            if visible(str(entry.get("project") or "")):
                items.append(_review(t, entry, now))

    if filing is not None and not confined and allowed is None:
        for task in sorted((x for x in engine.tasks.values() if eligible(x, filing)),
                           key=lambda x: x.first_seen):
            if not filed_key(task):
                items.append(_filing(t, task, filing.tracker, can_file, now))

    if replans and not confined and allowed is None:
        items.extend(_replan(t, row, now) for row in replans)

    if not can_act:
        for item in items:
            item["actions"] = []
    items.sort(key=lambda i: (-i["urgency"], -i["age_seconds"], i["id"]))
    counts = {kind: sum(1 for i in items if i["kind"] == kind) for kind in KINDS}
    return {"items": items, "total": len(items), "by_kind": counts, "can_act": can_act,
            "generated_at": now.isoformat()}


__all__ = ["KINDS", "build_inbox"]
