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
from .judgment import LABEL
from .task_engine import Task, TaskEngine, filed_key

KINDS = ("judgment", "followup", "wbs", "assignment", "review", "filing", "replan")

# 項目の並び（緊急度の目安）。診断の重大度・提案の種類・再計画の tier から決める。
URGENCY = {"followup": 70, "wbs": 50, "assignment": 40, "review": 55, "filing": 30}

REMEDY_EFFECT = {
    "followup": "対応タスクを台帳に作ります（課題管理ツールには作りません）。",
    "launch": "許可リストにあるテンプレートを、診断の文脈つきで起動します。",
    "retry_agent": "失敗した役割AIの実行を、もう一度任せます。",
    "recollect": "課題管理ツールの状態を読み直します（読み取りだけ）。",
    "notify": "人へ通知します。",
}


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


def _decide_actions(ref: str) -> list[dict[str, Any]]:
    path = "/api/pmo/proposals/decide"
    return [_action("approve", "承認する", "primary", path, {"ref": ref, "decision": "approve"}),
            _action("reject", "却下する", "danger", path, {"ref": ref, "decision": "reject"})]


def _item(kind: str, ref: str, title: str, summary: str, *, project: str, urgency: int,
          created_at: str, now: datetime, sections: list[dict[str, Any]],
          actions: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": f"{kind}:{ref}", "kind": kind, "ref": ref, "title": title, "summary": summary,
            "project": project, "urgency": urgency, "created_at": created_at,
            "age_seconds": _age(created_at, now),
            "detail": {"sections": [s for s in sections if s["lines"]]},
            "actions": actions, **(extra or {})}


# -- 種類ごとの項目 / one builder per kind --------------------------------------------------

def _judgment(task: Task, now: datetime) -> dict[str, Any]:
    payload = task.payload or {}
    diagnosis = payload.get("diagnosis") or {}
    remedy = str(payload.get("remedy") or "")
    title = str(diagnosis.get("title") or task.title)
    sections = [
        _section("診断の根拠", [str(line) for line in diagnosis.get("evidence") or []]),
        _section("選んだ対処", [LABEL.get(remedy, remedy), REMEDY_EFFECT.get(remedy, "")]),
        _section("なぜこの対処か", [str(payload.get("rationale") or "")]),
        _section("承認すると", ["常駐（aipmo schedule）が次の周で実行し、結果を判断の記録に残します。",
                              "実行しても診断が続けば「効かなかった」と学習し、次の対処へ進みます。"], "do"),
        _section("しないこと", ["承認しただけでは実行されません（実行するのは常駐だけ）。",
                             "Jira など外部への書き込みはしません。"], "dont"),
        _section("却下すると", ["同じ診断が続く間は、同じ対処を再び提案しません。"], "dont"),
    ]
    return _item("judgment", task.id, title, f"提案: {LABEL.get(remedy, remedy)}",
                 project=task.project, urgency=int(diagnosis.get("severity") or 60),
                 created_at=task.first_seen, now=now, sections=sections, actions=_decide_actions(task.id),
                 extra={"remedy": remedy, "diagnosis_kind": diagnosis.get("kind")})


def _proposal(task: Task, now: datetime) -> dict[str, Any]:
    is_wbs = task.id.startswith("PMO:wb:")
    kind = "wbs" if is_wbs else "followup"
    source = task.generated_from or ""
    if is_wbs:
        code, _, node = source.removeprefix("wbs:").partition(":")
        sections = [
            _section("きっかけ", [f"WBS のずれ: {code}（作業 {node}）"]),
            _section("承認すると", ["「WBS を確かめる」という普通のタスクになります（順位に入り、担当の提案が出ます）。"], "do"),
            _section("しないこと", ["WBS ファイルは変わりません。直すのは人（PR）です。"], "dont"),
            _section("却下すると", ["同じ問題が続く間は、再び提案しません。直ってから再び起きれば、新しい提案になります。"], "dont"),
        ]
        urgency = 60 if code == "evidence_missing" else URGENCY["wbs"]
    else:
        rule = source.removeprefix("alert:").partition("|")[0]
        sections = [
            _section("きっかけ", [f"続いている警告: {rule}" if rule else ""]),
            _section("優先度・期限", [f"{task.priority or '-'} ・ 期限 {task.due_date or '-'}"]),
            _section("承認すると", ["普通のタスクになります（順位に入り、担当の提案が出ます。役割AIにも任せられます）。"], "do"),
            _section("却下すると", ["記録は残り、同じ警告の間は再び提案しません（完了実績にはしません）。"], "dont"),
        ]
        urgency = URGENCY["followup"]
    return _item(kind, task.id, task.title, f"提案（承認待ち） ・ {task.priority or '-'}",
                 project=task.project, urgency=urgency, created_at=task.first_seen, now=now,
                 sections=sections, actions=_decide_actions(task.id))


def _assignment(task: Task, now: datetime, writeback: bool, tracker: str) -> dict[str, Any]:
    who = task.suggested_assignee or ""
    sections = [
        _section("理由", [task.suggestion_reason or ""]),
        _section("タスク", [f"{task.key or task.id} ・ {task.priority or '-'} ・ 期限 {task.due_date or '-'}"
                           f" ・ 点数 {task.score}"]),
        _section("確定すると", [f"台帳の担当を {who} にします。"]
                + ([f"{tracker} の担当者も更新します（引き当てられなければ、別人に付けずに止まります）。"]
                   if writeback else []), "do"),
    ]
    return _item("assignment", task.id, task.title, f"提案: {who}", project=task.project,
                 urgency=URGENCY["assignment"] + min(20, task.score // 5),
                 created_at=task.first_seen, now=now, sections=sections,
                 actions=[_action("confirm", "担当を確定する", "primary", "/api/pmo/assignments/accept",
                                  {"ref": task.id, "writeback": writeback})],
                 extra={"suggested_assignee": who})


def _review(entry: dict[str, Any], now: datetime) -> dict[str, Any]:
    task_id = str(entry["task"])
    sections = [
        _section("成果", [str(entry.get("excerpt") or "（要約なし）")]),
        _section("実行", [f"{entry.get('agent')} ・ 実行 {entry.get('dispatch')}"]),
        _section("認めると", ["確かめた記録が残ります。タスクは完了になりません（閉じるのは人）。"], "do"),
        _section("差し戻すと", ["警告になり、人が引き取るか、もう一度任せます。理由が必要です。"
                             "役割AI自身は、確かめる人になれません。"], "dont"),
    ]
    path = "/api/pmo/agents/review"
    base = {"ref": task_id, "dispatch": entry.get("dispatch")}
    return _item("review", f"{task_id}#{entry.get('dispatch')}", str(entry["title"]),
                 f"{entry.get('agent')} の成果（レビュー待ち）", project=str(entry.get("project") or ""),
                 urgency=URGENCY["review"], created_at=str(entry.get("finished_at") or ""), now=now,
                 sections=sections,
                 actions=[_action("accept", "認める", "primary", path, {**base, "decision": "accept"}),
                          _action("reject", "差し戻す", "danger", path, {**base, "decision": "reject"},
                                  needs_note=True)],
                 extra={"agent": entry.get("agent")})


def _filing(task: Task, tracker: str, can_file: bool, now: datetime) -> dict[str, Any]:
    state = filing_state(task)
    sections = [
        _section("起票先", [tracker]),
        _section("由来", [{"recurring": "定期タスク（設定に書いたので、承認なしで作成済み）",
                         "followup": "承認済みの提案"}.get(task.origin, task.origin)]),
        _section("担当", [task.assignee or "未定（担当なしで起票します）"]),
        _section("前回の失敗", [str(state.get("error") or "")] if state.get("state") == "failed" else []),
        _section("起票すると", ["課題管理ツールに課題を作ります（外の世界を変える操作です）。",
                             "同じものは二重に作りません。以後の収集が、その課題を同じタスクとして更新します。"], "do"),
    ]
    path = "/api/pmo/filing"
    actions = []
    if can_file:
        actions.append(_action("file", "起票する", "primary", path, {"ref": task.id, "decision": "file"}))
    actions.append(_action("skip", "見送る", "secondary", path, {"ref": task.id, "decision": "skip"}))
    return _item("filing", task.id, task.title, f"{tracker} への起票待ち", project=task.project,
                 urgency=URGENCY["filing"], created_at=task.first_seen, now=now, sections=sections,
                 actions=actions, extra={"can_file": can_file})


def _replan(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    diff = row.get("diff")
    changes = diff.get("changes") if isinstance(diff, dict) else None
    label = f" [{row['option_label']}]" if row.get("option_label") else ""
    sections = [
        _section("根拠", [str(row.get("rationale") or "")]),
        _section("内容", [f"WBS ファイルへ反映できる変更 {len(changes)} 件" if isinstance(changes, list)
                        else "自由な形の提案（承認しても記録だけ）"]),
        _section("承認すると", ["反映先が設定されていれば、反映後の内容まで検証してから WBS ファイルへ書きます。"
                              if isinstance(changes, list) else "承認の記録だけが残ります。"], "do"),
    ]
    base = f"/api/wbs-proposals/{row['id']}"
    tier = int(row.get("tier") or 2)
    return _item("replan", str(row["id"]), f"WBS 再計画案{label}: {row.get('wbs_version_from')}",
                 f"tier {tier} ・ 確信度 {row.get('confidence')}", project="",
                 urgency={1: 45, 2: 75, 3: 95}.get(tier, 75), created_at=str(row.get("created_at") or ""),
                 now=now, sections=sections,
                 actions=[_action("approve", "承認する", "primary", f"{base}/approve", {}),
                          _action("reject", "却下する", "danger", f"{base}/reject", {})])


# -- 集める / gathering ---------------------------------------------------------------------

def build_inbox(engine: TaskEngine, *, members: list[Any] | None = None, filing: Any = None,
                can_act: bool = True, allowed: set[str] | None = None, confined: bool = False,
                writable: set[str] | None = None, can_file: bool = False,
                replans: list[dict[str, Any]] | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    """台帳から、人の判断を待っているものを集める（読むだけ）。

    `allowed` はこの呼び出しが見てよいプロジェクト（小文字）、`confined` は閲覧者がプロジェクトに
    限定されているとき。`writable` は担当を書き戻せるトラッカー、`can_file` は起票先のアダプタがあるか。
    """
    # 循環 import を避けるため、ここで読む
    from .pmo_core import PmoCore

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
                items.append(_judgment(task, now))
        elif visible(task.project):
            items.append(_proposal(task, now))

    for task in engine.tasks.values():
        if (task.suggested_assignee and not task.assignee and not task.done and not task.proposed
                and task.origin != "judgment" and visible(task.project)):
            tracker = task.tracker or ("jira" if str(task.id).startswith("JIRA:") else "")
            writeback = bool(tracker and tracker in writable
                             and (task.external_id or (task.key if tracker == "jira" else "")))
            items.append(_assignment(task, now, writeback, tracker))

    if members and any(getattr(m, "is_agent", False) for m in members):
        core = PmoCore(task_engine=engine, members=members)
        for entry in core.reviews_pending(engine.ranked()):
            if visible(str(entry.get("project") or "")):
                items.append(_review(entry, now))

    if filing is not None and not confined and allowed is None:
        for task in sorted((t for t in engine.tasks.values() if eligible(t, filing)),
                           key=lambda t: t.first_seen):
            if not filed_key(task):
                items.append(_filing(task, filing.tracker, can_file, now))

    if replans and not confined and allowed is None:
        items.extend(_replan(row, now) for row in replans)

    if not can_act:
        for item in items:
            item["actions"] = []
    items.sort(key=lambda i: (-i["urgency"], -i["age_seconds"], i["id"]))
    counts = {kind: sum(1 for i in items if i["kind"] == kind) for kind in KINDS}
    return {"items": items, "total": len(items), "by_kind": counts, "can_act": can_act,
            "generated_at": now.isoformat()}


__all__ = ["KINDS", "build_inbox"]
