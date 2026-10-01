"""PMO AI Core — タスク台帳の上に載る統括層。

[aipmo/task_engine.py](aipmo/task_engine.py) が「何があり、どの順か」を持つ
のに対し、ここは「それをどう動かすか」を決める。1周（cycle）ごとに:

  1. 順位を最新にする            (TaskEngine.refresh)
  2. 担当者のいないタスクに担当者を提案する   (Assigner)
  3. 進捗ルールを全タスクに当てる            (Rule / evaluate_rules)
  4. 新しく出た違反だけを通知し、判断を記録する
  5. 全体の状況を 1 枚のブリーフィングにまとめる

すべて数えれば決まる材料だけで判断し、言語モデルは使わない。同じ台帳と
同じ設定から毎回同じ結論が出ることが、「なぜこの人に提案したのか」
「なぜこれが警告なのか」を後から説明できる前提だから。判断の一つひとつは
追記専用のログ（decisions ファイル）に残る。

担当割当は**提案まで**。確定は人が `aipmo assign --apply` で行い、その時
初めて Jira に書く。書き込みを AI の判断だけで行わせない、というこの
プロジェクトの方針（`agent` の `allow_writes` や WBS 再計画の承認）と同じ。

The PMO Core sits on top of the task ledger. The ledger knows what exists and
in what order; the Core decides what to do about it. Each cycle refreshes the
ranking, proposes assignees for unowned tasks, applies the progress rules,
notifies only *newly raised* violations, and rolls everything into one
briefing. Everything is deterministic — no language model — so any conclusion
can be explained later, and every decision is appended to a log. Assignment
stops at a *proposal*: a human confirms it (`aipmo assign --apply`), and only
then is Jira written to, in line with the project's rule that a write never
rests on the AI's judgement alone.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .agent_roles import (KEEP_DISPATCHES, SETTLED_BAD, excerpt_of, fit, latest_dispatch,
                          params_for)
from .generation import (GenerationConfig, due_date, followup_id, period_key,
                         recurring_id)
from .pmo_learning import (DEFAULT_MAX_ESTIMATE_ERROR, DEFAULT_MIN_SAMPLES, LearnedModel,
                           learn)
from .task_engine import IN_PROGRESS_STATUSES, Task, TaskEngine, _parse_date, side_path

logger = logging.getLogger("aipmo.pmo_core")

SEVERITY_ORDER = {"critical": 3, "high": 2, "medium": 1, "low": 0}

_IN_PROGRESS = IN_PROGRESS_STATUSES
_NOT_STARTED = {"to do", "todo", "open", "backlog", "new", "selected for development",
                "未着手", "未対応", "オープン"}


# -- 進捗ルール / progress rules ---------------------------------------------

@dataclass(frozen=True)
class Rule:
    id: str
    kind: str          # overdue | stalled | blocked_long | unassigned | not_started_near_due
    days: int
    severity: str = "medium"
    enabled: bool = True


# 既定のルール。config.yaml の pmo_core.rules で id 単位に上書きできる。
# Defaults; override per id under pmo_core.rules in config.yaml.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("overdue", "overdue", days=1, severity="high"),
    Rule("overdue_severe", "overdue", days=7, severity="critical"),
    Rule("stalled", "stalled", days=5, severity="medium"),
    Rule("blocked_long", "blocked_long", days=3, severity="high"),
    Rule("unassigned", "unassigned", days=2, severity="medium"),
    Rule("not_started_near_due", "not_started_near_due", days=3, severity="high"),
    # 役割AIの実行が失敗・不適合・時間切れで終わったタスク（人が引き取る）。
    # A task whose role-AI run failed, did not fit or timed out: a human takes over.
    Rule("agent_attention", "agent_attention", days=0, severity="medium"),
)


class RuleError(ValueError):
    pass


def load_rules(overrides: list[dict[str, Any]] | None) -> list[Rule]:
    """既定ルールに config の上書き・追加を重ねる / defaults plus config overrides."""
    rules = {r.id: r for r in DEFAULT_RULES}
    kinds = {r.kind for r in DEFAULT_RULES}
    for raw in overrides or []:
        rule_id = raw.get("id")
        if not rule_id:
            raise RuleError("pmo_core.rules の各項目に id が必要です / each rule needs an id")
        base = rules.get(rule_id)
        kind = raw.get("kind") or (base.kind if base else None)
        if kind not in kinds:
            raise RuleError(f"ルール '{rule_id}': 不明な kind {kind!r} "
                            f"(使える種類: {', '.join(sorted(kinds))})")
        severity = raw.get("severity") or (base.severity if base else "medium")
        if severity not in SEVERITY_ORDER:
            raise RuleError(f"ルール '{rule_id}': 不明な severity {severity!r}")
        try:
            days = int(raw.get("days", base.days if base else 1))
        except (TypeError, ValueError) as exc:
            raise RuleError(f"ルール '{rule_id}': days は整数 / days must be an integer") from exc
        rules[rule_id] = Rule(rule_id, kind, days, severity,
                              bool(raw.get("enabled", True)))
    return list(rules.values())


@dataclass(frozen=True)
class Violation:
    rule: str
    task_id: str
    severity: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.task_id}"


def _days_since(stamp: str | None, now: datetime) -> int | None:
    if not stamp:
        return None
    return (now - datetime.fromisoformat(stamp)).days


def evaluate_rule(rule: Rule, task: Task, now: datetime) -> Violation | None:
    status = (task.status or "").strip().lower()
    message: str | None = None

    if rule.kind == "overdue":
        due = _parse_date(task.due_date)
        if due is not None:
            late = (now.date() - due).days
            if late >= rule.days:
                message = f"期限を {late} 日超過（基準 {rule.days} 日）"
    elif rule.kind == "stalled":
        if status in _IN_PROGRESS:
            held = _days_since(task.status_since, now)
            if held is not None and held >= rule.days:
                message = f"「{task.status}」のまま {held} 日動きなし（基準 {rule.days} 日）"
    elif rule.kind == "blocked_long":
        held = _days_since(task.blocked_since, now) if task.blocked else None
        if held is not None and held >= rule.days:
            message = f"ブロックが {held} 日続いている（基準 {rule.days} 日）"
    elif rule.kind == "unassigned":
        if not task.assignee:
            held = _days_since(task.first_seen, now)
            if held is not None and held >= rule.days:
                message = f"担当者が {held} 日決まっていない（基準 {rule.days} 日）"
    elif rule.kind == "not_started_near_due":
        due = _parse_date(task.due_date)
        if due is not None and status in _NOT_STARTED:
            remaining = (due - now.date()).days
            if 0 <= remaining <= rule.days:
                message = f"期限まで残り {remaining} 日だが未着手（基準 {rule.days} 日）"

    elif rule.kind == "agent_attention":
        latest = latest_dispatch(task)
        if (latest is not None and latest.get("status") in SETTLED_BAD
                and (task.assignee or "").lower() == str(latest.get("agent", "")).lower()):
            reason = latest.get("error") or ""
            message = (f"役割AI {latest['agent']} の実行が {latest['status']} で終わりました"
                       + (f": {reason}" if reason else "") + "（人が引き取ってください）")

    if message is None:
        return None
    return Violation(rule.id, task.id, rule.severity, message)


def evaluate_rules(rules: list[Rule], tasks: list[Task], now: datetime) -> list[Violation]:
    """全タスクに全ルールを当てる。同じ種類が複数の段階で当たるとき
    （overdue と overdue_severe）は、より重い方だけを残す。

    When one kind fires at several tiers (overdue and overdue_severe), only the
    heaviest is kept so a task is not warned about twice for the same thing.
    """
    found: dict[tuple[str, str], Violation] = {}
    for task in tasks:
        for rule in rules:
            if not rule.enabled:
                continue
            violation = evaluate_rule(rule, task, now)
            if violation is None:
                continue
            slot = (rule.kind, task.id)
            held = found.get(slot)
            if held is None or (SEVERITY_ORDER[violation.severity]
                                > SEVERITY_ORDER[held.severity]):
                found[slot] = violation
    return sorted(found.values(),
                  key=lambda v: (-SEVERITY_ORDER[v.severity], v.task_id, v.rule))


# -- 担当割当 / assignment ---------------------------------------------------

@dataclass(frozen=True)
class Member:
    name: str
    capacity: int = 5                    # 同時に持てる未完了タスク数 / open tasks at once
    skills: tuple[str, ...] = ()         # タスクのラベルと突き合わせる / matched to labels
    # 担当してよいプロジェクト（小文字）。空なら全プロジェクト。
    # Projects they may be assigned to (lower-case); empty means all.
    projects: tuple[str, ...] = ()
    # トラッカーごとのアカウント（アダプタ名 → ログイン名／ID／表示名）。
    # 担当を書き戻すときだけ使う。Jira は名前を引き当てられるので省略できる。
    # The member's account in each tracker (adapter name → login / id / display
    # name), used only for write-back. Jira resolves names itself, so it may be omitted.
    accounts: tuple[tuple[str, str], ...] = ()
    # 人か、役割AI（`templates/roles/` のテンプレート）か。役割AIは `template` を
    # 持ち、ラベルがスキルに一致するタスクだけを候補にする。人を優先し、
    # `prefer` を立てると役割AIを優先する。`auto_confirm` を立てると、提案を
    # 人の確定なしに自動で確定する（既定は人が確定）。
    # A human or a role AI (a template under templates/roles/). A role AI is a
    # candidate only for tasks whose labels match its skills; humans win ties
    # unless `prefer`; `auto_confirm` skips the human confirmation (off by default).
    kind: str = "human"
    template: str | None = None
    params: tuple[tuple[str, str], ...] = ()
    trackers: tuple[str, ...] = ()
    auto_confirm: bool = False
    prefer: bool = False

    def account(self, tracker: str) -> str | None:
        return dict(self.accounts).get(tracker)

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"


def load_members(raw: list[Any] | None) -> list[Member]:
    members = []
    for item in raw or []:
        if isinstance(item, str):
            members.append(Member(name=item))
        elif isinstance(item, dict) and item.get("name"):
            kind = str(item.get("kind") or "human").lower()
            if kind not in ("human", "agent"):
                raise RuleError(f"メンバー '{item['name']}': kind は human か agent: {kind!r}")
            if kind == "agent" and not item.get("template"):
                raise RuleError(f"役割AI '{item['name']}' には template（templates/roles/ の"
                                f"テンプレート名）が必要です / a role AI needs a template")
            members.append(Member(
                kind=kind,
                template=str(item["template"]) if item.get("template") else None,
                params=tuple((str(k), str(v)) for k, v in (item.get("params") or {}).items()),
                trackers=tuple(str(t) for t in item.get("trackers") or []),
                auto_confirm=bool(item.get("auto_confirm", False)),
                prefer=bool(item.get("prefer", False)),
                name=str(item["name"]),
                capacity=max(1, int(item.get("capacity", 5))),
                skills=tuple(str(s).lower() for s in item.get("skills") or []),
                projects=tuple(str(p).lower() for p in item.get("projects") or []),
                accounts=tuple((str(k), str(v)) for k, v in
                               (item.get("accounts") or {}).items() if v),
            ))
    return members


def _member_of(name: str | None, members: list[Member]) -> Member | None:
    if not name:
        return None
    lowered = name.strip().lower()
    return next((m for m in members if m.name.lower() == lowered), None)


def member_loads(tasks: list[Task], members: list[Member]) -> dict[str, int]:
    """メンバーごとの、いま持っている未完了タスク数。"""
    loads = {m.name: 0 for m in members}
    for task in tasks:
        member = _member_of(task.assignee, members)
        if member is None:
            continue
        if member.is_agent:
            # 役割AIの負荷は「実行中」だけ。実行が済んだ（成功・失敗とも）タスクは、
            # 人が引き取るまで台帳に残るが、役割AIの枠は塞がない。
            # A role AI's load is what is *running*; finished work waits for a
            # human but does not hold the role AI's capacity.
            latest = latest_dispatch(task, member.name)
            if latest is not None and latest.get("status") != "running":
                continue
        loads[member.name] += 1
    return loads


def suggest_assignee(task: Task, members: list[Member],
                     loads: dict[str, int]) -> tuple[Member, str] | None:
    """空いている人を選ぶ。スキル（ラベル）が合う人がいればその中から。

    負荷の比率（持っている数 / 上限）が低い順、同率は名前順で決める。
    上限に達している人には割り当てない。全員が上限なら提案しない —
    黙って誰かに積み増すより、割り当て先が無いと知らせる方が正しい。

    Picks whoever has the most room, from among skill matches when there are
    any. Lowest load ratio wins, ties by name. Nobody at capacity is offered
    more; if everyone is full, nothing is proposed — telling the PM there is no
    room is better than silently piling more on someone.
    """
    labels = {label.lower() for label in task.labels}
    # 担当してよいプロジェクトを限られた人には、その外のタスクを提案しない。
    # プロジェクトの分からないタスクは、制限の無い人にだけ。
    # Someone limited to certain projects is never offered a task outside them;
    # a task with no known project goes only to the unrestricted.
    project = (getattr(task, "project", "") or "").lower()
    eligible = [m for m in members if not m.projects or project in m.projects]
    # 役割AIは、ラベルがスキルに一致するときだけ候補にする。何でも引き受ける
    # 役割AIが、人の空きを横取りしないように。
    # A role AI is a candidate only on a skill match, so it cannot swallow work
    # that nothing says it suits.
    eligible = [m for m in eligible if not m.is_agent or labels & set(m.skills)]
    open_members = [m for m in eligible if loads.get(m.name, 0) < m.capacity]
    if not open_members:
        return None

    def rank(m: Member) -> tuple[int, float, str]:
        # 既定は人が先、役割AIはその次。`prefer` の役割AIだけが人より先。
        # Humans first by default; only a `prefer` role AI goes ahead of them.
        group = 0 if (m.is_agent and m.prefer) else (2 if m.is_agent else 1)
        return (group, loads.get(m.name, 0) / m.capacity, m.name)

    matched = [m for m in open_members if labels & set(m.skills)]
    pool, basis = (matched, "スキル一致") if matched else (open_members, "空き状況")
    chosen = min(pool, key=rank)
    load = loads.get(chosen.name, 0)
    reason = f"{basis}で選定（現在 {load}/{chosen.capacity} 件）"
    if matched:
        reason += f"、一致: {', '.join(sorted(labels & set(chosen.skills)))}"
    return chosen, reason


# -- 高リスク時の応答 / responses to high risk -------------------------------

_LEVELS = ["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class Response:
    """「こういう状況になったら、このテンプレートを走らせる」という宣言。

    運用者が config.yaml に書いたものだけが起動できる（許可リスト）。
    書かれていないテンプレートを、AI の判断で走らせることはない。
    起動されたテンプレート自身の承認（agent の require_approval など）は
    そのまま効く。

    A declaration: "in this situation, run that template". Only what the
    operator wrote into config.yaml can be launched (an allow-list); the Core
    never picks a template on its own. The launched template's own gates
    (an agent's require_approval, say) still apply.
    """

    id: str
    template: str                           # テンプレート名（templates/ 配下）
    params: dict[str, Any] = field(default_factory=dict)
    min_level: str = "high"                 # 全体レベルがこれ以上
    rules: tuple[str, ...] = ()             # 指定があれば、このルールの警告が出ている
    min_alerts: int = 1                     # 警告の件数がこれ以上
    cooldown_hours: float = 24.0
    max_per_day: int = 3

    def matches(self, briefing: dict[str, Any]) -> list[str] | None:
        """条件を満たすなら理由の一覧、満たさなければ None。"""
        level = briefing["overall_level"]
        if _LEVELS.index(level) < _LEVELS.index(self.min_level):
            return None
        alerts = briefing["alerts"]
        if self.rules:
            alerts = [a for a in alerts if a["rule"] in self.rules]
            if not alerts:
                return None
        if len(alerts) < self.min_alerts:
            return None
        reasons = [f"全体レベル {level}（基準 {self.min_level} 以上）",
                   f"該当する警告 {len(alerts)} 件"]
        if self.rules:
            reasons.append(f"ルール: {', '.join(sorted({a['rule'] for a in alerts}))}")
        return reasons


def load_responses(raw: list[dict[str, Any]] | None) -> list[Response]:
    responses, seen = [], set()
    for item in raw or []:
        rid, template = item.get("id"), item.get("template")
        if not rid or not template:
            raise RuleError("pmo_core.responses の各項目に id と template が必要です "
                            "/ each response needs an id and a template")
        if rid in seen:
            raise RuleError(f"応答 id '{rid}' が重複しています / duplicate response id")
        seen.add(rid)
        min_level = item.get("min_level", "high")
        if min_level not in _LEVELS:
            raise RuleError(f"応答 '{rid}': 不明な min_level {min_level!r}")
        try:
            response = Response(
                id=str(rid), template=str(template),
                params=dict(item.get("params") or {}), min_level=min_level,
                rules=tuple(item.get("rules") or ()),
                min_alerts=int(item.get("min_alerts", 1)),
                cooldown_hours=float(item.get("cooldown_hours", 24)),
                max_per_day=int(item.get("max_per_day", 3)),
            )
        except (TypeError, ValueError) as exc:
            raise RuleError(f"応答 '{rid}': 数値の設定が不正です / bad number: {exc}") from exc
        if response.max_per_day < 1 or response.cooldown_hours < 0:
            raise RuleError(f"応答 '{rid}': max_per_day は 1 以上、cooldown_hours は 0 以上")
        responses.append(response)
    return responses


# -- プロジェクトでの絞り込み / project scoping ---------------------------------

def _priority_item(t: Task) -> dict[str, Any]:
    return {"id": t.id, "title": t.title, "score": t.score, "assignee": t.assignee,
            "due_date": t.due_date, "reasons": t.reasons, "project": t.project}


def _agent_runs(active: list[Task], limit: int = 10) -> list[dict[str, Any]]:
    """役割AIの直近の実行（新しい順）/ the most recent role-AI runs, newest first."""
    runs = []
    for task in active:
        for entry in task.dispatches:
            runs.append({"task": task.id, "title": task.title, "project": task.project,
                         "agent": entry.get("agent"), "status": entry.get("status"),
                         "at": entry.get("at"), "run_id": entry.get("run_id"),
                         "error": entry.get("error"), "excerpt": entry.get("excerpt")})
    runs.sort(key=lambda r: str(r["at"] or ""), reverse=True)
    return runs[:limit]


def _level_of(severities: list[str]) -> str:
    if "critical" in severities:
        return "critical"
    if "high" in severities:
        return "high"
    return "medium" if severities else "low"


def _project_summary(active: list[Task], violations: list[Violation]) -> list[dict[str, Any]]:
    """プロジェクトごとの件数・警告・レベル / per-project counts, alerts, level."""
    by_id = {t.id: t for t in active}
    names = sorted({t.project for t in active if t.project}, key=str.lower)
    summary = []
    for name in names:
        mine = [v for v in violations
                if v.task_id in by_id and by_id[v.task_id].project == name]
        summary.append({
            "project": name,
            "active_count": sum(1 for t in active if t.project == name),
            "alert_count": len(mine),
            "level": _level_of([v.severity for v in mine]),
        })
    return summary


def scope_briefing(briefing: dict[str, Any], active: list[Task],
                   projects: set[str], *, redact_org: bool,
                   top: int = 5) -> dict[str, Any]:
    """ブリーフィングを、指定したプロジェクトのぶんだけに絞る。

    `active` は絞り込み済みの未完了タスク（順位順）。全体レベル・件数・
    優先順位は、絞った範囲で作り直す。`redact_org` が真のとき（閲覧者を
    プロジェクトに限定しているとき）は、ほかのプロジェクトや組織全体が
    透けるもの — メンバーの負荷、学習した補正、応答、ほかのプロジェクトの
    一覧 — を取り除く。

    Narrows a briefing to the given projects. `active` is the already-filtered
    open tasks (ranked); level, count and priorities are rebuilt from them.
    With `redact_org` (a viewer confined to some projects) everything that
    would reveal other projects or the organisation as a whole — member load,
    learned adjustments, responses, the other projects' list — is removed.
    """
    allowed = {p.lower() for p in projects}

    def mine(item: dict[str, Any]) -> bool:
        return (item.get("project") or "").lower() in allowed

    alerts = [a for a in briefing["alerts"] if mine(a)]
    scoped = {
        **briefing,
        "overall_level": _level_of([a["severity"] for a in alerts]),
        "active_count": len(active),
        "top_priorities": [_priority_item(t) for t in active[:top]],
        "alerts": alerts,
        "assignment_proposals": [p for p in briefing["assignment_proposals"] if mine(p)],
        "unassignable": [u for u in briefing["unassignable"] if mine(u)],
        "projects": [p for p in briefing.get("projects", [])
                     if p["project"].lower() in allowed],
        "agent_runs": [r for r in briefing.get("agent_runs", []) if mine(r)],
        "generated": {**briefing.get("generated", {"created": []}),
                      "pending": [p for p in (briefing.get("generated") or {}).get("pending", [])
                                  if mine(p)]},
        "scope": sorted(allowed),
    }
    if redact_org:
        scoped.update(member_loads=[], overloaded_members=[], responses=[],
                      learning=None, agents=[], agent_dispatch=[], collection=None)
    return scoped


# -- 統括 / the core ---------------------------------------------------------

@dataclass
class PmoCore:
    task_engine: TaskEngine
    rules: list[Rule] = field(default_factory=lambda: list(DEFAULT_RULES))
    members: list[Member] = field(default_factory=list)
    state_path: Path | None = None
    decisions_path: Path | None = None
    briefing_path: Path | None = None
    # 通知。None なら通知しない（判断と記録だけ行う）。
    # Notification; None means decide and record only.
    notify: Callable[[str], None] | None = None
    renotify_hours: int = 24
    # 学習（過去の実績からの重み調整）。aipmo/pmo_learning.py
    # Learning from track record.
    learning: bool = True
    min_samples: int = DEFAULT_MIN_SAMPLES
    # 優先度の重みの補正と、見積り精度・ペースの学習は、それぞれ切れる。
    # 見積り誤差の中央値がこれを超えるなら、ペースは順位に使わない。
    # Each can be switched off. Pace is not used for ranking when the median
    # estimate error exceeds this.
    learn_priority: bool = True
    learn_estimates: bool = True
    max_estimate_error: float = DEFAULT_MAX_ESTIMATE_ERROR
    learned_path: Path | None = None
    # 高リスク時のテンプレート自動起動。launcher(response, trigger) が
    # 実際に走らせる。None なら起動せず「起動するはず」だけを報告する。
    # Auto-launch of templates on high risk. `launcher(response, trigger)` does
    # the running; with None nothing is launched and the briefing only reports
    # what *would* launch.
    responses: list[Response] = field(default_factory=list)
    launcher: Callable[[Response, dict[str, Any]], Any] | None = None
    # True なら別スレッドで走らせ、周（スケジューラのループ）を塞がない。
    # Run in a worker thread so the scheduler loop is never blocked.
    background: bool = True
    # 役割AIへの依頼。実行中のまま戻らないものを、この分を過ぎたら「時間切れ」にする。
    # 1 日に任せる件数（役割AIごと）の上限。
    # Role-AI dispatch: a run still "running" past this many minutes is declared
    # timed out; and a per-role-AI daily ceiling.
    agent_timeout_minutes: int = 60
    agent_max_per_day: int = 20
    # 進捗の自動収集（aipmo/collector.py）。None なら収集しない。常駐のときだけ渡される。
    # Automatic progress collection; None means none. Given only to the resident process.
    collector: Any = None
    # タスクの生成（aipmo/generation.py）。既定は何も作らない。
    # Task generation; by default nothing is generated.
    generation: GenerationConfig = field(default_factory=GenerationConfig)

    def __post_init__(self) -> None:
        ledger = self.task_engine.path
        self.state_path = self.state_path or side_path(ledger, "pmo-core-state.json")
        self.decisions_path = self.decisions_path or side_path(ledger, "pmo-decisions.jsonl")
        self.briefing_path = self.briefing_path or side_path(ledger, "pmo-briefing.json")
        self.learned_path = self.learned_path or side_path(ledger, "pmo-learned.json")
        self._state = self._load_state()
        self._running: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._model = LearnedModel()
        self._lock = threading.Lock()
        self._dispatching: set[str] = set()

    # -- 状態と判断ログ / state and decision log --------------------------

    def _load_state(self) -> dict[str, Any]:
        assert self.state_path is not None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("open_alerts", {})
        return data

    def _save_state(self) -> None:
        self._write_json(self.state_path, self._state)

    def _write_json(self, path: Path | None, payload: Any) -> None:
        assert path is not None
        # 常駐の `aipmo schedule` と、`aipmo assign|agents|pmo` のような CLI は、
        # 別プロセスで同じファイルを書く。一時ファイル名を共有すると、Windows では
        # 互いの置き換えが「別のプロセスが使用中」で失敗する（実測）。名前をプロセスごとに
        # 分け、読まれている最中の置き換えは短く再試行する。
        # The resident scheduler and CLIs such as `aipmo assign|agents|pmo` write the
        # same files from separate processes. A shared temp name made their
        # replacements collide on Windows ("in use by another process", seen in
        # practice), so the name is per process and a replace that races a reader is
        # retried briefly.
        temporary = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            for attempt in range(5):
                try:
                    temporary.replace(path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            logger.warning("保存できません / cannot save %s: %s", path, exc)
            try:
                temporary.unlink()
            except OSError:
                pass

    def _log(self, kind: str, at: datetime, **detail: Any) -> None:
        """判断を1行ずつ追記する。過去は書き換えない / append-only."""
        assert self.decisions_path is not None
        line = json.dumps({"at": at.isoformat(), "kind": kind, **detail},
                          ensure_ascii=False)
        try:
            self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.decisions_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("判断ログを書けません / cannot write decision log: %s", exc)

    # -- 1周 / one cycle -------------------------------------------------

    def refresh(self) -> dict[str, Any]:
        """スケジューラのループから呼ばれる入口 / entry point for the scheduler loop."""
        return self.cycle()

    def cycle(self) -> dict[str, Any]:
        engine = self.task_engine
        now = engine.now()
        # 収集は課題管理ツールを読むので、台帳の取引の外で、最初に行う。
        # Collection reads trackers, so it runs first and outside any ledger transaction.
        collection = self._collect(now)
        engine.sync()             # ほかのプロセスの更新（実績を含む）を取り込む
        self._learn(now)          # 順位付けの前に。加点が順位に効くため
        # 台帳を書き換える部分は1つの取引にまとめる。通知やテンプレート起動
        # （ネットワーク）は取引の外で行い、書き込みロックを握ったまま待たない。
        # All ledger writes form one transaction. Notifications and template
        # launches (network) happen outside it, so the write lock is never
        # held while waiting on something slow.
        with engine.transaction():
            engine.refresh()
            self._expire_dispatches(now)
            self._generate_recurring(now)
            active = engine.ranked()
            self._propose_assignments(active, now)
        # 役割AIへ任せる（起動は取引の外で）。記録は自分の取引で行う。
        # Hand work to role AIs (launching outside the transaction; each record
        # is written in its own).
        dispatch_report = self._dispatch_agents(active, now)
        fresh = engine.ranked()      # 役割AIへ任せた記録も含む最新の状態
        violations = evaluate_rules(self.rules, fresh, now)
        self._track_alerts(violations, fresh, now)
        created = self._generate_followups(violations, fresh, now)

        briefing = self.briefing(fresh, violations, now)
        briefing["collection"] = collection
        briefing["generated"] = {
            "pending": [{"id": t.id, "title": t.title, "project": t.project,
                         "priority": t.priority, "due_date": t.due_date,
                         "origin": t.origin, "generated_from": t.generated_from}
                        for t in engine.proposals()],
            "created": created}
        briefing["agent_dispatch"] = dispatch_report
        briefing["responses"] = self._respond(briefing, now)
        briefing["learning"] = self._model.as_dict() if self.learning else None
        self._write_json(self.briefing_path, briefing)
        return briefing

    # -- 学習 / learning ---------------------------------------------------

    def _team(self) -> list[Member]:
        """実績で補正したキャパシティのメンバー / members at learned capacity."""
        if not self._model.member_factor:
            return self.members
        return [
            replace(m, capacity=max(1, round(
                m.capacity * self._model.member_factor.get(m.name.lower(), 1.0))))
            for m in self.members
        ]

    def apply_learning(self) -> None:
        """最新の実績から学習し、台帳の採点に反映する。周を回さずに順位を出す
        コマンド（`aipmo tasks`）が、学習なしの点数を台帳に書き込まないように。

        Learn from the latest outcomes and hand the result to the ledger's
        scoring, so a command that ranks without running a cycle (`aipmo tasks`)
        never writes scores computed without what was learned.
        """
        self.task_engine.sync()
        self._learn(self.task_engine.now())

    def _learn(self, now: datetime) -> None:
        if not self.learning:
            self.task_engine.label_bonus = {}
            self.task_engine.priority_delta = {}
            self.task_engine.pace = {}
            return
        model = learn(self.task_engine.outcomes, self.min_samples,
                      priority=self.learn_priority, estimates=self.learn_estimates,
                      max_error=self.max_estimate_error)
        if model.signature() != self._model.signature():
            self._log("model_updated", now, samples=model.samples,
                      member_factor=model.member_factor,
                      label_bonus=model.label_bonus,
                      priority_delta=model.priority_delta,
                      pace_team=model.pace.get("team"),
                      pace_reliable=model.pace.get("reliable"),
                      estimate_error=model.pace.get("median_error"),
                      baseline_late_rate=model.baseline_late_rate)
            self._write_json(self.learned_path, {"generated_at": now.isoformat(),
                                                 **model.as_dict()})
        self._model = model
        self.task_engine.label_bonus = model.label_bonus
        self.task_engine.priority_delta = model.priority_delta
        self.task_engine.pace = model.pace

    # -- 高リスク時のテンプレート自動起動 / auto-launching templates -----------

    def _respond(self, briefing: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        """条件を満たした応答を、上限の範囲で起動する。結果を報告に載せる。

        起動できるのは config.yaml の `pmo_core.responses` に運用者が書いた
        テンプレートだけ。条件（全体レベル・ルール・件数）、クールダウン、
        1日の上限、同じ応答の重複起動禁止、の順に絞る。
        """
        report = []
        with self._lock:
            history: dict[str, list[str]] = self._state.setdefault("responses", {})
            for response in self.responses:
                fired = [datetime.fromisoformat(t) for t in history.get(response.id, [])]
                fired = [t for t in fired if (now - t).total_seconds() < 86400]
                history[response.id] = [t.isoformat() for t in fired]

                reasons = response.matches(briefing)
                if reasons is None:
                    status = "not_triggered"
                elif response.id in self._running:
                    status = "running"
                elif fired and (now - max(fired)).total_seconds() < response.cooldown_hours * 3600:
                    status = "cooldown"
                elif len(fired) >= response.max_per_day:
                    status = "daily_limit"
                elif self.launcher is None:
                    status = "would_launch"
                else:
                    self._launch(response, briefing, reasons, now)
                    history[response.id].append(now.isoformat())
                    status = "launched"
                report.append({"id": response.id, "template": response.template,
                               "status": status})
            self._save_state()
        return report

    def _launch(self, response: Response, briefing: dict[str, Any],
                reasons: list[str], now: datetime) -> None:
        trigger = {
            "type": "pmo_core",
            "response": response.id,
            "overall_level": briefing["overall_level"],
            "reasons": reasons,
            "alerts": briefing["alerts"],
            "top_priorities": briefing["top_priorities"],
        }
        self._running.add(response.id)
        self._log("response_launched", now, response=response.id,
                  template=response.template, reasons=reasons)

        def work() -> None:
            outcome = "finished"
            try:
                assert self.launcher is not None
                self.launcher(response, trigger)
            except Exception as exc:
                # 失敗しても再試行の連打はしない（クールダウンは有効なまま）。
                # A failure is not hammered: the cooldown still applies.
                outcome = "failed"
                logger.warning("応答 %s の起動に失敗 / response %s failed: %s",
                               response.id, response.id, exc)
                self._log("response_failed", self.task_engine.now(),
                          response=response.id, error=f"{type(exc).__name__}: {exc}")
            else:
                self._log("response_finished", self.task_engine.now(),
                          response=response.id)
            finally:
                self._running.discard(response.id)
            logger.info("response %s %s", response.id, outcome)

        if self.background:
            worker = threading.Thread(target=work, name=f"pmo-response-{response.id}",
                                      daemon=False)
            self._workers = [w for w in self._workers if w.is_alive()] + [worker]
            worker.start()
        else:
            work()

    def wait(self, timeout: float | None = None) -> None:
        """起動した応答の完了を待つ（テストと終了処理用）/ wait for launched runs."""
        for worker in list(self._workers):
            worker.join(timeout)

    def _propose_assignments(self, active: list[Task], now: datetime) -> None:
        if not self._team():
            return
        loads = member_loads(active, self._team())
        # すでに出している提案は、その人の負荷に数えてから残りを決める。
        # 周ごとに提案先が入れ替わるのを避けるため。
        # Standing proposals count toward load first, so proposals do not
        # shuffle from one cycle to the next.
        for task in active:
            held = _member_of(task.suggested_assignee, self._team())
            if not task.suggested_assignee:
                continue
            if (not task.assignee and held is not None
                    and loads[held.name] < held.capacity):
                loads[held.name] += 1
            else:
                task.suggested_assignee = task.suggestion_reason = None

        for task in active:   # 順位の高い順に先に選ばせる / highest-ranked picks first
            if task.assignee or task.suggested_assignee:
                continue
            picked = suggest_assignee(task, self._team(), loads)
            if picked is None:
                continue
            member, reason = picked
            loads[member.name] += 1
            if member.is_agent and member.auto_confirm:
                # 運用者が「この役割AIは人の確定なしでよい」と設定したときだけ。
                # Only where the operator said this role AI needs no human confirmation.
                task.assignee = member.name
                self._log("assignment_auto_confirmed", now, task=task.id, title=task.title,
                          assignee=member.name, reason=reason, score=task.score)
                continue
            task.suggested_assignee, task.suggestion_reason = member.name, reason
            self._log("assignment_proposed", now, task=task.id, title=task.title,
                      assignee=member.name, reason=reason, score=task.score)

    # -- 進捗の自動収集 / automatic progress collection ------------------------------

    def _collect(self, now: datetime) -> dict[str, Any] | None:
        """間隔が来ていれば収集する。結果（直近のもの）を返す。"""
        state = self._state.setdefault("collector", {})
        if self.collector is None:
            return state.get("last")
        last_run = state.get("last_run")
        if last_run and (now - datetime.fromisoformat(last_run)).total_seconds() \
                < self.collector.interval_minutes * 60:
            return state.get("last")
        return self.collect_now()

    def collect_now(self) -> dict[str, Any]:
        """いま収集する(間隔を待たない)。失敗は結果に残し、周を止めない。"""
        if self.collector is None:
            raise RuntimeError("収集が設定されていません（pmo_core.collect）"
                               " / collection is not configured")
        try:
            report = self.collector.run_once()
        except Exception as exc:                           # noqa: BLE001
            report = {"at": self.task_engine.now().isoformat(), "sources": [],
                      "refreshed": 0, "failed": 0, "missing": [], "completed": 0,
                      "error": f"{type(exc).__name__}: {exc}"}
            logger.warning("収集に失敗 / collection failed", exc_info=True)
        now = self.task_engine.now()
        with self._lock:
            self._state["collector"] = {"last_run": now.isoformat(), "last": report}
            self._save_state()
        self._log("collected", now,
                  sources={s["id"]: (s["error"] or s["items"]) for s in report["sources"]},
                  refreshed=report["refreshed"], failed=report["failed"],
                  missing=len(report["missing"]), completed=report["completed"],
                  error=report.get("error"))
        return report

    # -- タスクの生成 / task generation ------------------------------------------------

    def _generate_recurring(self, now: datetime) -> None:
        """運用者が設定した定期タスクを、期間ごとに一度だけ作る(取引の中で呼ぶ)。"""
        for rec in self.generation.recurring:
            period = period_key(rec, now)
            if period is None:
                continue
            task = self.task_engine.create_task(
                recurring_id(rec, period), rec.title, origin="recurring", proposed=False,
                project=rec.project, priority=rec.priority, assignee=rec.assignee,
                labels=rec.labels, generated_from=f"recurring:{rec.id}",
                due_date=due_date(now, rec.due_in_days, rec.timezone))
            if task is not None:
                self._log("task_generated", now, task=task.id, title=task.title,
                          origin="recurring", period=period)

    def _generate_followups(self, violations: list[Violation], active: list[Task],
                            now: datetime) -> list[str]:
        """続いている重大な警告から、対応を決めるタスクを**提案**する(承認待ち)。"""
        if not self.generation.followups:
            return []
        rules = {f.rule: f for f in self.generation.followups}
        open_alerts: dict[str, dict[str, str]] = self._state["open_alerts"]
        by_id = {t.id: t for t in active}
        created: list[str] = []
        for violation in violations:
            follow = rules.get(violation.rule)
            task = by_id.get(violation.task_id)
            # 生成したタスク自身の警告からは、さらに提案を作らない（連鎖を断つ）。
            # No proposals from a generated task's own alerts: that would chain.
            if follow is None or task is None or task.origin:
                continue
            raised = (open_alerts.get(violation.key) or {}).get("raised_at")
            if not raised or (now - datetime.fromisoformat(raised)).days < follow.after_days:
                continue
            proposal = self.task_engine.create_task(
                followup_id(violation.rule, task.id, raised),
                f"対応を決める: {task.title} — {violation.message}",
                origin="followup", proposed=True, project=task.project,
                priority=follow.priority, due_date=due_date(now, follow.due_in_days),
                labels=(*follow.labels, violation.rule),
                generated_from=f"alert:{violation.key}")
            if proposal is None:
                continue
            created.append(proposal.id)
            self._log("task_generated", now, task=proposal.id, title=proposal.title,
                      origin="followup", alert=violation.key, pending=True)
            if self.notify is not None:
                try:
                    self.notify(f"[提案] {proposal.title}（承認待ち: aipmo generated）")
                except Exception:                          # noqa: BLE001
                    logger.warning("提案の通知に失敗 / proposal notice failed", exc_info=True)
        return created

    def decide_proposal(self, ref: str, approve: bool) -> Task:
        """提案を承認する／却下する。決定は判断ログに残る。"""
        task = self.task_engine.decide_proposal(ref, approve)
        self._log("proposal_approved" if approve else "proposal_rejected",
                  self.task_engine.now(), task=task.id, title=task.title)
        return task

    def complete_task(self, ref: str) -> Task:
        """台帳だけのタスクを完了にする(実績に残る)。"""
        task = self.task_engine.complete(ref)
        self._log("task_completed", self.task_engine.now(), task=task.id, title=task.title)
        return task

    # -- 役割AIへ任せる / handing work to role AIs ------------------------------

    def _agent_members(self) -> dict[str, Member]:
        return {m.name.lower(): m for m in self.members if m.is_agent}

    def _expire_dispatches(self, now: datetime) -> None:
        """戻ってこない実行を「時間切れ」にする。取引の中で呼ぶ。

        プロセスが落ちた・スレッドが止まったなどで「実行中」のまま残ると、
        その役割AIの枠も、人が気づくきっかけも失われる。
        A run stuck as "running" (a crash, a stalled thread) would hold the role
        AI's slot and hide the task from the humans who should take over.
        """
        limit = timedelta(minutes=self.agent_timeout_minutes)
        for task in self.task_engine.tasks.values():
            for entry in task.dispatches:
                if entry.get("status") != "running":
                    continue
                try:
                    started = datetime.fromisoformat(str(entry.get("at")))
                except ValueError:
                    continue
                if now - started > limit and task.id not in self._dispatching:
                    entry["status"] = "abandoned"
                    entry["error"] = f"{self.agent_timeout_minutes} 分以内に終わりませんでした"
                    entry["finished_at"] = now.isoformat()
                    self._log("agent_abandoned", now, task=task.id, agent=entry.get("agent"),
                              dispatch=entry.get("id"))

    def _dispatched_today(self, agent: str, now: datetime) -> int:
        log = self._state.setdefault("agent_dispatches", {}).get(agent, [])
        return sum(1 for stamp in log
                   if (now - datetime.fromisoformat(stamp)).total_seconds() < 86400)

    def _note_dispatch(self, agent: str, now: datetime) -> None:
        with self._lock:
            log = self._state.setdefault("agent_dispatches", {}).setdefault(agent, [])
            log[:] = [s for s in log
                      if (now - datetime.fromisoformat(s)).total_seconds() < 86400]
            log.append(now.isoformat())
            self._save_state()

    def _agent_summary(self, active: list[Task], now: datetime) -> list[dict[str, Any]]:
        summary = []
        for member in self._agent_members().values():
            mine = [t for t in active if (t.assignee or "").lower() == member.name.lower()]
            running = sum(1 for t in mine
                          if (latest_dispatch(t, member.name) or {}).get("status") == "running")
            summary.append({
                "member": member.name, "template": member.template,
                "open_assigned": len(mine), "running": running,
                "waiting": sum(1 for t in mine if latest_dispatch(t, member.name) is None),
                "dispatched_today": self._dispatched_today(member.name, now),
                "capacity": member.capacity, "auto_confirm": member.auto_confirm,
            })
        return summary

    def _dispatch_agents(self, active: list[Task], now: datetime) -> list[dict[str, Any]]:
        """役割AIに割り当てられ、まだ任せていないタスクを、役割AIに任せる。

        1 つのタスクを 1 つの役割AIに任せるのは一度きり（失敗しても自動では再試行しない
        — 再試行は人が `aipmo agents run` で決める）。役割AIの枠（同時に走らせる数）と
        1 日の上限を守る。向かないタスクは走らせず、理由を記録して警告にする。

        Each (task, role AI) is handed over once; a failure is not retried
        automatically — retrying is a human's call (`aipmo agents run`). Honours
        the role AI's parallel slots and a daily ceiling; a task that does not fit
        is not run, and the reason is recorded and raised as an alert.
        """
        agents = self._agent_members()
        if not agents:
            return []
        running = {name: sum(1 for t in active for d in t.dispatches
                             if str(d.get("agent", "")).lower() == name
                             and d.get("status") == "running") for name in agents}
        report = []
        for task in active:
            member = agents.get((task.assignee or "").lower())
            if member is None or latest_dispatch(task, member.name) is not None:
                continue
            outcome = self._start_dispatch(task, member, now, running[member.name.lower()])
            if outcome["status"] == "started":
                running[member.name.lower()] += 1
            report.append(outcome)
        return report

    def _start_dispatch(self, task: Task, member: Member, now: datetime, running: int,
                        *, force: bool = False, background: bool | None = None) -> dict[str, Any]:
        base = {"task": task.id, "agent": member.name}
        ok, why = fit(member, task)
        if not ok:
            self._record_dispatch(task.id, {
                "id": uuid.uuid4().hex[:10], "agent": member.name, "template": member.template,
                "at": now.isoformat(), "status": "skipped", "error": why,
                "finished_at": now.isoformat()})
            self._log("agent_skipped", now, task=task.id, agent=member.name, reason=why)
            return {**base, "status": "skipped", "reason": why}

        if not force:
            if running >= member.capacity:
                return {**base, "status": "waiting", "reason": "同時に走らせる上限です"}
            if self._dispatched_today(member.name, now) >= self.agent_max_per_day:
                return {**base, "status": "daily_limit"}
        if self.launcher is None:
            # 表示専用（`aipmo pmo`）。起動はしない / display-only: nothing is launched
            return {**base, "status": "would_dispatch"}

        params = params_for(member, task)
        entry: dict[str, Any] = {"id": uuid.uuid4().hex[:10], "agent": member.name,
                                 "template": member.template,
                 "at": now.isoformat(), "status": "running", "params": params}
        self._record_dispatch(task.id, entry)
        self._note_dispatch(member.name, now)
        self._log("agent_dispatched", now, task=task.id, agent=member.name,
                  template=member.template, dispatch=entry["id"], params=params)

        response = Response(id=f"agent:{member.name}", template=str(member.template),
                            params=params)
        trigger = {"type": "pmo_core_agent", "agent": member.name,
                   "task": {"id": task.id, "key": task.key, "title": task.title,
                            "project": task.project, "tracker": task.tracker,
                            "external_id": task.external_id, "labels": list(task.labels),
                            "due_date": task.due_date}}
        self._dispatching.add(task.id)

        def work() -> None:
            status, error, run_id, excerpt = "done", None, None, ""
            try:
                assert self.launcher is not None
                result = self.launcher(response, trigger)
                run_id = getattr(result, "run_id", None)
                excerpt = excerpt_of(result)
            except Exception as exc:               # noqa: BLE001
                status, error = "failed", f"{type(exc).__name__}: {exc}"
                logger.warning("役割AI %s の実行に失敗 / role AI %s failed: %s",
                               member.name, member.name, exc)
            finally:
                self._dispatching.discard(task.id)
            self._finish_dispatch(task.id, entry["id"], member.name, status, run_id,
                                  error, excerpt)

        use_thread = self.background if background is None else background
        if use_thread:
            worker = threading.Thread(target=work, name=f"pmo-agent-{member.name}",
                                      daemon=False)
            self._workers = [w for w in self._workers if w.is_alive()] + [worker]
            worker.start()
        else:
            work()
        return {**base, "status": "started", "dispatch": entry["id"]}

    def _record_dispatch(self, task_id: str, entry: dict[str, Any]) -> None:
        with self.task_engine.transaction():
            task = self.task_engine.tasks.get(task_id)
            if task is not None:
                task.dispatches.append(entry)
                del task.dispatches[:-KEEP_DISPATCHES]

    def _finish_dispatch(self, task_id: str, dispatch_id: str, agent: str, status: str,
                         run_id: str | None, error: str | None, excerpt: str) -> None:
        finished = self.task_engine.now()
        try:
            with self.task_engine.transaction():
                task = self.task_engine.tasks.get(task_id)
                for entry in (task.dispatches if task is not None else []):
                    if entry.get("id") == dispatch_id:
                        entry.update(status=status, finished_at=finished.isoformat(),
                                     run_id=run_id, error=error, excerpt=excerpt)
        except Exception:                           # noqa: BLE001
            logger.warning("役割AIの結果を台帳に書けません / cannot record the result of %s",
                           dispatch_id, exc_info=True)
        self._log("agent_finished" if status == "done" else "agent_failed", finished,
                  task=task_id, agent=agent, dispatch=dispatch_id, run_id=run_id, error=error)

    def dispatch_now(self, ref: str) -> dict[str, Any]:
        """役割AIに割り当てられたタスクを、いま任せる（失敗の再試行にも使う）。

        上限・1 日の枠は無視する（人が明示的に頼んだので）が、向かないタスクは
        走らせない。完了まで待って結果を返す。
        Hands an assigned task over now (also how a failure is retried). Ignores
        the slots and daily ceiling — a human asked — but never forces a task
        through a role that does not fit it. Waits for the result.
        """
        engine = self.task_engine
        task = engine.find(ref)
        if task is None:
            raise KeyError(f"タスクが見つかりません / no such task: {ref}")
        member = self._agent_members().get((task.assignee or "").lower())
        if member is None:
            raise ValueError(f"{task.id} は役割AIに割り当てられていません"
                             f"（担当: {task.assignee or '未定'}）/ not assigned to a role AI")
        if self.launcher is None:
            raise RuntimeError("テンプレートを起動できません（設定に pmo_core.members の"
                               "役割AIとテンプレートが必要です）/ no launcher configured")
        outcome = self._start_dispatch(task, member, engine.now(), 0, force=True,
                                       background=False)
        fresh = engine.find(ref)
        outcome["latest"] = latest_dispatch(fresh, member.name) if fresh else None
        return outcome

    def _track_alerts(self, violations: list[Violation], active: list[Task],
                      now: datetime) -> None:
        open_alerts: dict[str, dict[str, str]] = self._state["open_alerts"]
        by_id = {t.id: t for t in active}
        current = {v.key: v for v in violations}

        for key in [k for k in open_alerts if k not in current]:
            rule, _, task_id = key.partition("|")
            self._log("alert_resolved", now, rule=rule, task=task_id)
            del open_alerts[key]

        for key, violation in current.items():
            entry = open_alerts.get(key)
            task = by_id.get(violation.task_id)
            title = task.title if task else violation.task_id
            if entry is None:
                entry = open_alerts[key] = {"raised_at": now.isoformat()}
                self._log("alert_raised", now, rule=violation.rule,
                          task=violation.task_id, severity=violation.severity,
                          message=violation.message)
            if self._due_for_notice(entry, now):
                self._notice(violation, title, entry, now)
        self._save_state()

    def _due_for_notice(self, entry: dict[str, str], now: datetime) -> bool:
        sent = entry.get("notified_at")
        if sent is None:
            return True
        return (now - datetime.fromisoformat(sent)).total_seconds() >= self.renotify_hours * 3600

    def _notice(self, violation: Violation, title: str,
                entry: dict[str, str], now: datetime) -> None:
        if self.notify is None:
            return
        text = f"[{violation.severity}] {title} — {violation.message} ({violation.rule})"
        try:
            self.notify(text)
        except Exception:
            # 通知の失敗で周を止めない。次の周で再送する（notified_at を進めない）。
            # A failed notice does not stop the cycle; it retries next time.
            logger.warning("通知に失敗 / notification failed: %s", text, exc_info=True)
            return
        entry["notified_at"] = now.isoformat()
        self._log("alert_notified", now, rule=violation.rule, task=violation.task_id)

    # -- ブリーフィング / briefing -------------------------------------------

    def briefing(self, active: list[Task], violations: list[Violation],
                 now: datetime, top: int = 5) -> dict[str, Any]:
        severities = [v.severity for v in violations]
        if "critical" in severities:
            level = "critical"
        elif "high" in severities:
            level = "high"
        elif violations:
            level = "medium"
        else:
            level = "low"

        loads = member_loads(active, self._team())
        overloaded = [
            {"member": m.name, "load": loads[m.name], "capacity": m.capacity}
            for m in self._team() if loads[m.name] > m.capacity
        ]
        by_id = {t.id: t for t in active}
        return {
            "generated_at": now.isoformat(),
            "overall_level": level,
            "active_count": len(active),
            "top_priorities": [_priority_item(t) for t in active[:top]],
            "alerts": [
                {"rule": v.rule, "task": v.task_id,
                 "title": by_id[v.task_id].title if v.task_id in by_id else v.task_id,
                 "project": by_id[v.task_id].project if v.task_id in by_id else "",
                 "severity": v.severity, "message": v.message}
                for v in violations
            ],
            "assignment_proposals": [
                {"task": t.id, "title": t.title, "assignee": t.suggested_assignee,
                 "reason": t.suggestion_reason, "project": t.project}
                for t in active if t.suggested_assignee
            ],
            "unassignable": [
                {"task": t.id, "title": t.title, "project": t.project} for t in active
                if self._team() and not t.assignee and not t.suggested_assignee
            ],
            "projects": _project_summary(active, violations),
            "agents": self._agent_summary(active, now),
            "agent_runs": _agent_runs(active),
            "overloaded_members": overloaded,
            "member_loads": [
                {"member": m.name, "load": loads[m.name], "capacity": m.capacity}
                for m in self._team()
            ],
        }

    # -- 担当の確定 / confirming an assignment ---------------------------------

    def accept_assignment(self, ref: str,
                          write: Callable[[Task, str], Any] | None = None) -> Task:
        """提案された担当を確定する。`ref` はタスク id、または課題のキー。

        `write(task, assignee)` を渡すと、そのタスクのトラッカーへ書き込む
        （aipmo/writeback.py の `make_writer`）。書き込みが失敗したら台帳は
        変えない — 台帳だけ先に進むと、実際とずれる。

        Confirms a proposal. `ref` is a task id or an issue key. With
        `write(task, assignee)` the task's own tracker is updated first; if that
        fails the ledger is left untouched, since a ledger that ran ahead of
        reality would be wrong.
        """
        engine = self.task_engine
        found = engine.find(ref)
        if found is None:
            raise KeyError(f"タスクが見つかりません / no such task: {ref}")
        if not found.suggested_assignee:
            raise ValueError(f"提案がありません / no proposal for {ref}")

        assignee, task_id = found.suggested_assignee, found.id
        # 外部への書き込み（ネットワーク）は台帳の取引の外で行う。
        # The external write (network) happens outside the ledger transaction.
        self.last_writeback: dict[str, Any] | None = None
        if write is not None:
            outcome = write(found, assignee)
            self.last_writeback = outcome if isinstance(outcome, dict) else None

        with engine.transaction():
            task = engine.tasks.get(task_id)
            if task is None:
                raise KeyError(f"タスクが見つかりません / no such task: {ref}")
            if task.assignee and task.assignee != assignee:
                # 待っている間に、別の人が別の担当を確定していた。
                # Someone confirmed a different assignee while we were writing.
                raise ValueError(
                    f"すでに {task.assignee} に確定されています "
                    f"/ already assigned to {task.assignee}")
            task.assignee = assignee
            task.suggested_assignee = task.suggestion_reason = None
        self._log("assignment_accepted", engine.now(), task=task_id, assignee=assignee)
        return task


def slack_notifier(slack: Any, channel: str) -> Callable[[str], None]:
    def send(text: str) -> None:
        slack.invoke("post_message", {"channel": channel, "text": text})
    return send
