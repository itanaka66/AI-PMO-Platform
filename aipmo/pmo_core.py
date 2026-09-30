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
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .pmo_learning import DEFAULT_MIN_SAMPLES, LearnedModel, learn
from .task_engine import Task, TaskEngine, _parse_date

logger = logging.getLogger("aipmo.pmo_core")

SEVERITY_ORDER = {"critical": 3, "high": 2, "medium": 1, "low": 0}

_IN_PROGRESS = {"in progress", "doing", "in review", "進行中", "レビュー中"}
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


def load_members(raw: list[Any] | None) -> list[Member]:
    members = []
    for item in raw or []:
        if isinstance(item, str):
            members.append(Member(name=item))
        elif isinstance(item, dict) and item.get("name"):
            members.append(Member(
                name=str(item["name"]),
                capacity=max(1, int(item.get("capacity", 5))),
                skills=tuple(str(s).lower() for s in item.get("skills") or []),
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
        if member is not None:
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
    open_members = [m for m in members if loads.get(m.name, 0) < m.capacity]
    if not open_members:
        return None

    matched = [m for m in open_members if labels & set(m.skills)]
    pool, basis = (matched, "スキル一致") if matched else (open_members, "空き状況")
    chosen = min(pool, key=lambda m: (loads.get(m.name, 0) / m.capacity, m.name))
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

    def __post_init__(self) -> None:
        base = self.task_engine.path.parent
        self.state_path = self.state_path or base / "pmo-core-state.json"
        self.decisions_path = self.decisions_path or base / "pmo-decisions.jsonl"
        self.briefing_path = self.briefing_path or base / "pmo-briefing.json"
        self.learned_path = self.learned_path or base / "pmo-learned.json"
        self._state = self._load_state()
        self._running: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._model = LearnedModel()
        self._lock = threading.Lock()

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
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            logger.warning("保存できません / cannot save %s: %s", path, exc)

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
        engine.sync()             # ほかのプロセスの更新（実績を含む）を取り込む
        self._learn(now)          # 順位付けの前に。加点が順位に効くため
        # 台帳を書き換える部分は1つの取引にまとめる。通知やテンプレート起動
        # （ネットワーク）は取引の外で行い、書き込みロックを握ったまま待たない。
        # All ledger writes form one transaction. Notifications and template
        # launches (network) happen outside it, so the write lock is never
        # held while waiting on something slow.
        with engine.transaction():
            engine.refresh()
            active = engine.ranked()
            self._propose_assignments(active, now)
        violations = evaluate_rules(self.rules, active, now)
        self._track_alerts(violations, active, now)

        briefing = self.briefing(active, violations, now)
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

    def _learn(self, now: datetime) -> None:
        if not self.learning:
            self.task_engine.label_bonus = {}
            return
        model = learn(self.task_engine.outcomes, self.min_samples)
        if (model.member_factor != self._model.member_factor
                or model.label_bonus != self._model.label_bonus):
            self._log("model_updated", now, samples=model.samples,
                      member_factor=model.member_factor,
                      label_bonus=model.label_bonus,
                      baseline_late_rate=model.baseline_late_rate)
            self._write_json(self.learned_path, {"generated_at": now.isoformat(),
                                                 **model.as_dict()})
        self._model = model
        self.task_engine.label_bonus = model.label_bonus

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
            task.suggested_assignee, task.suggestion_reason = member.name, reason
            loads[member.name] += 1
            self._log("assignment_proposed", now, task=task.id, title=task.title,
                      assignee=member.name, reason=reason, score=task.score)

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
            "top_priorities": [
                {"id": t.id, "title": t.title, "score": t.score, "assignee": t.assignee,
                 "due_date": t.due_date, "reasons": t.reasons}
                for t in active[:top]
            ],
            "alerts": [
                {"rule": v.rule, "task": v.task_id,
                 "title": by_id[v.task_id].title if v.task_id in by_id else v.task_id,
                 "severity": v.severity, "message": v.message}
                for v in violations
            ],
            "assignment_proposals": [
                {"task": t.id, "title": t.title, "assignee": t.suggested_assignee,
                 "reason": t.suggestion_reason}
                for t in active if t.suggested_assignee
            ],
            "unassignable": [
                {"task": t.id, "title": t.title} for t in active
                if self._team() and not t.assignee and not t.suggested_assignee
            ],
            "overloaded_members": overloaded,
            "member_loads": [
                {"member": m.name, "load": loads[m.name], "capacity": m.capacity}
                for m in self._team()
            ],
        }

    # -- 担当の確定 / confirming an assignment ---------------------------------

    def accept_assignment(self, ref: str,
                          write: Callable[[str, str], Any] | None = None) -> Task:
        """提案された担当を確定する。`ref` はタスク id か Jira キー。

        `write(key, assignee)` を渡すと Jira などへ書き込む。書き込みが失敗
        したら台帳は変えない — 台帳だけ先に進むと、実際とずれる。

        Confirms a proposal. `ref` is a task id or Jira key. With `write`, the
        external system is updated first; if that fails the ledger is left
        untouched, since a ledger that ran ahead of reality would be wrong.
        """
        engine = self.task_engine
        found = engine.find(ref)
        if found is None:
            raise KeyError(f"タスクが見つかりません / no such task: {ref}")
        if not found.suggested_assignee:
            raise ValueError(f"提案がありません / no proposal for {ref}")

        assignee, key, task_id = found.suggested_assignee, found.key, found.id
        # 外部への書き込み（ネットワーク）は台帳の取引の外で行う。
        # The external write (network) happens outside the ledger transaction.
        if write is not None and key:
            write(key, assignee)

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
