"""タスクの生成 — Task Engine が自分でタスクを作る。

台帳に入るタスクは、これまで外から来るものだけだった（課題管理ツール、テンプレートの
出力）。PMO Core は次の二つを、自分で作る。どちらも**課題管理ツールには作らない**
（台帳だけのタスク）。外の世界を変えるのは、人が確定した操作だけ、という方針は同じ。

1. **定期タスク**（`recurring`）… 運用者が `config.yaml` に書いたもの。毎日・毎週・毎月の
   決まった仕事（週次レビューなど）。書いてあること自体が許可なので、期間ごとに
   承認なしで作る。同じ期間には一度だけ（id が「どの定期タスクの、どの期間」）。
2. **対応タスクの提案**（`followups`）… 重大な警告が一定期間続いたとき、「対応を決める」
   タスクを**提案**する。提案は承認待ちで、承認されるまで順位にも担当提案にも入らない。
   却下しても記録が残り、同じ警告の回では出し直さない（警告が解消して再び起きたら、
   新しい回として出る）。

どちらも設定しなければ何も作らない（既定は生成なし）。

Task generation. The ledger used to receive tasks only from outside. The PMO Core
now makes two kinds itself — both ledger-only, never created in a tracker (changing
the outside world stays a human's confirmed act): recurring tasks the operator wrote
into the config (the entry is the authorisation, so they are created without
approval, once per period), and follow-up *proposals* raised from a persistent
serious alert (pending until a human approves; a rejection is kept so the same
episode is not proposed again). Nothing is generated unless configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
EVERY = ("day", "week", "month")

# `generate.followups: true` と書いたときの既定。放っておくと困る警告だけ。
# What `generate.followups: true` means: only the alerts that hurt if left alone.
DEFAULT_FOLLOWUP_RULES = ("overdue_severe", "blocked_long", "agent_attention")


class GenerationError(ValueError):
    """生成の設定が不正 / a malformed generate config."""


@dataclass(frozen=True)
class Recurring:
    id: str
    title: str
    every: str                       # day | week | month
    weekday: int | None = None       # week のとき、この曜日以降に作る(0=MON)
    day: int | None = None           # month のとき、この日以降に作る
    due_in_days: int = 3
    priority: str | None = None
    assignee: str | None = None
    labels: tuple[str, ...] = ()
    project: str = ""
    timezone: str = "UTC"


@dataclass(frozen=True)
class Followup:
    rule: str                        # 警告のルール id
    after_days: int = 1              # この日数以上続いた警告だけ
    priority: str = "High"
    due_in_days: int = 3
    labels: tuple[str, ...] = ("followup",)


# WBS の更新漏れ・証拠の欠けとして提案してよい問題(aipmo/wbs.py の Problem.code)。
# 既定は「終わっていそうなのに未完了」「完了なのに証拠が無い／消えた」。
# The WBS problems that may be proposed. Default: looks finished but isn't marked, and
# marked finished but the evidence is missing or gone.
WBS_CODES = ("maybe_done", "done_without_evidence", "evidence_missing",
             "done_before_dependency", "overdue")
DEFAULT_WBS_CODES = ("maybe_done", "done_without_evidence", "evidence_missing")


@dataclass(frozen=True)
class WbsWatch:
    """WBS ファイルを見張り、更新漏れ・証拠の欠けを対応タスクの提案にする設定。"""
    file: str = "wbs/aipmo.yaml"
    root: str = "."                        # 証拠(evidence)のパスの基準
    codes: tuple[str, ...] = DEFAULT_WBS_CODES
    priority: str = "Medium"
    due_in_days: int = 7
    interval_minutes: int = 60             # 証拠の確認はファイルを読むので、間引く
    project: str = ""                      # 空なら WBS 自身の id
    labels: tuple[str, ...] = ("wbs",)


@dataclass(frozen=True)
class GenerationConfig:
    recurring: list[Recurring] = field(default_factory=list)
    followups: list[Followup] = field(default_factory=list)
    wbs: WbsWatch | None = None


def load_generation(raw: dict[str, Any] | None) -> GenerationConfig:
    raw = raw or {}
    recurring, seen = [], set()
    for item in raw.get("recurring") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            raise GenerationError("定期タスクには id と title が必要です "
                                  "/ each recurring task needs an id and a title")
        rid = str(item["id"])
        if rid in seen:
            raise GenerationError(f"定期タスクの id が重複しています: {rid}")
        seen.add(rid)
        every = str(item.get("every") or "week").lower()
        if every not in EVERY:
            raise GenerationError(f"定期タスク '{rid}': every は {', '.join(EVERY)}: {every!r}")
        weekday = None
        if item.get("weekday") is not None:
            name = str(item["weekday"]).upper()[:3]
            if name not in WEEKDAYS:
                raise GenerationError(f"定期タスク '{rid}': weekday は MON〜SUN: {item['weekday']!r}")
            weekday = WEEKDAYS.index(name)
        day = item.get("day")
        if day is not None and (isinstance(day, bool) or not isinstance(day, int)
                                or not 1 <= day <= 28):
            raise GenerationError(f"定期タスク '{rid}': day は 1〜28: {day!r}")
        zone = str(item.get("timezone") or "UTC")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise GenerationError(f"定期タスク '{rid}': timezone を解釈できません: {zone}") from exc
        recurring.append(Recurring(
            id=rid, title=str(item["title"]), every=every, weekday=weekday, day=day,
            due_in_days=max(0, int(item.get("due_in_days", 3))),
            priority=str(item["priority"]) if item.get("priority") else None,
            assignee=str(item["assignee"]) if item.get("assignee") else None,
            labels=tuple(str(label) for label in item.get("labels") or []),
            project=str(item.get("project") or ""), timezone=zone))

    followups: list[Followup] = []
    spec = raw.get("followups")
    if spec is True:
        followups = [Followup(rule=r) for r in DEFAULT_FOLLOWUP_RULES]
    elif isinstance(spec, list):
        for item in spec:
            if isinstance(item, str):
                item = {"rule": item}
            if not isinstance(item, dict) or not item.get("rule"):
                raise GenerationError("対応タスクの提案には rule が必要です "
                                      "/ each follow-up needs a rule")
            followups.append(Followup(
                rule=str(item["rule"]), after_days=max(0, int(item.get("after_days", 1))),
                priority=str(item.get("priority") or "High"),
                due_in_days=max(0, int(item.get("due_in_days", 3))),
                labels=tuple(str(label) for label in item.get("labels") or ["followup"])))
    elif spec not in (None, False):
        raise GenerationError("followups は true か一覧 / followups must be true or a list")
    watch = None
    spec_wbs = raw.get("wbs")
    if spec_wbs is True:
        spec_wbs = {}
    if spec_wbs not in (None, False):
        if not isinstance(spec_wbs, dict):
            raise GenerationError("wbs は true かマッピング / wbs must be true or a mapping")
        codes = spec_wbs.get("codes")
        chosen = DEFAULT_WBS_CODES if codes is None else tuple(str(c) for c in codes)
        bad = [c for c in chosen if c not in WBS_CODES]
        if bad or not chosen:
            raise GenerationError(
                f"wbs.codes は {', '.join(WBS_CODES)} のどれか: {bad or '空'} "
                f"/ wbs.codes must be some of {', '.join(WBS_CODES)}")
        watch = WbsWatch(
            file=str(spec_wbs.get("file") or "wbs/aipmo.yaml"),
            root=str(spec_wbs.get("root") or "."), codes=chosen,
            priority=str(spec_wbs.get("priority") or "Medium"),
            due_in_days=max(0, int(spec_wbs.get("due_in_days", 7))),
            interval_minutes=max(1, int(spec_wbs.get("interval_minutes", 60))),
            project=str(spec_wbs.get("project") or ""),
            labels=tuple(str(label) for label in spec_wbs.get("labels") or ["wbs"]))
    return GenerationConfig(recurring=recurring, followups=followups, wbs=watch)


def period_key(rec: Recurring, now: datetime) -> str | None:
    """いま作るべき期間のキー。まだ作る時期でなければ None。

    週次で `weekday` を指定したときは、その曜日**以降**の最初の周で作る（その曜日に
    常駐が止まっていても、週のうちに作られる）。月次の `day` も同じ。
    The period to create now, or None if it is not time yet. With a weekday (or a
    day of month), it is created on the first cycle on or after it, so a resident
    process that was down on that day still creates it within the period.
    """
    local = now.astimezone(ZoneInfo(rec.timezone))
    if rec.every == "day":
        return local.date().isoformat()
    if rec.every == "week":
        if rec.weekday is not None and local.weekday() < rec.weekday:
            return None
        year, week, _ = local.isocalendar()
        return f"{year}-W{week:02d}"
    if rec.day is not None and local.day < rec.day:
        return None
    return f"{local.year}-{local.month:02d}"


def recurring_id(rec: Recurring, period: str) -> str:
    return f"PMO:rec:{rec.id}:{period}"


def due_date(now: datetime, days: int, timezone: str = "UTC") -> str:
    local: date = now.astimezone(ZoneInfo(timezone)).date()
    return (local + timedelta(days=days)).isoformat()


def wbs_drift_id(code: str, node: str, raised_at: str) -> str:
    """WBS の問題の「回」を表す id。直って再び起きれば raised_at が変わり、別の提案になる。"""
    return f"PMO:wb:{code}:{node}:{raised_at[:16].replace(':', '')}"


def followup_id(rule: str, task_id: str, raised_at: str) -> str:
    """警告の「回」を表す id。解消して再び起きれば raised_at が変わり、別の提案になる。"""
    return f"PMO:fu:{rule}:{task_id}:{raised_at[:16].replace(':', '')}"
