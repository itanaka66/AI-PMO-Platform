"""PMO Core による自律的な判断 — 診断し、対処を選び、許された範囲で実行する。

これまでの PMO Core は、決まった規則（警告のルール、応答の条件）を実行する側だった。
ここでは Core が**状況を診断し、複数の対処から選ぶ**。ただし、選べるのは運用者が許可した
範囲だけで、外の世界を変える判断を AI だけに任せない、という方針は変えない。

1. **診断**（`diagnose`）… 台帳とブリーフィングから、いま何が問題かを数えて決める
   （過負荷、リスクの高いプロジェクト、役割AIの失敗、収集の停止、容量不足、見積りでは
   間に合わないタスクの増加）。言語モデルは使わない。同じ入力から同じ診断が出る。
2. **対処の選択**（`candidates`）… 診断ごとに、使える対処の中から、効用で選ぶ。
   効用は、対処の基本の見込み − リスク + **過去にその診断に効いたかの実績**
   （効いたか／効かなかったかを、解消までの経過で数えて学習する）。同じ対処が効かな
   かったら、次の対処に進む（段階的に強める）。
3. **自律度**（`autonomy`）… 対処の種類ごとに `off`（しない）／`propose`（提案して人の承認で
   実行）／`auto`（実行する）を運用者が決める。既定で `auto` なのは、**人への通知**と、
   **読み取りだけの再収集**だけ。テンプレートの起動、役割AIの再試行、対応タスクの作成は
   既定では `propose` で、人が承認したときに実行される。
4. **歯止め** … 1 日の自動実行の上限。自動実行の失敗が続くと、**遮断器**が働いて自動を
   提案に落とす（人が戻すまで／一定時間後）。止める・再開するスイッチ（`pause`）もある。
   一時停止中は診断だけを行い、何も実行しない。
5. **記録** … 判断はすべて、根拠・選んだ対処・理由・結果を台帳に残す（`origin: judgment` の
   記録。仕事ではなく記録なので、順位にも担当提案にも入らない）。

Autonomous judgment by the PMO Core: diagnose the situation, choose among remedies, act
within what the operator allowed. Diagnosis is counted, not asked of a language model.
Each remedy has an autonomy level — `off`, `propose` (a human approves, then it runs) or
`auto` — and by default only notifying people and a read-only re-collection are `auto`;
launching templates, retrying role AIs and creating follow-up tasks are `propose`.
Selection weighs a remedy's base value, its risk and whether it actually worked on this
kind of problem before; a remedy that did not work gives way to the next. Guards: a daily
ceiling on automatic actions, a circuit breaker that demotes `auto` to `propose` after
repeated failures, and a pause switch. Every judgment is recorded with its reasons.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

AUTONOMY = ("off", "propose", "auto")
REMEDIES = ("notify", "recollect", "retry_agent", "launch", "followup")
DIAGNOSES = ("overload", "project_risk", "agent_failure", "collection_failing",
             "capacity_shortage", "estimate_risk")

# 既定の自律度。外の世界に触れない／読み取りだけのものだけが auto。
# Defaults: only what touches nothing outside, or only reads, is automatic.
DEFAULT_AUTONOMY = {"notify": "auto", "recollect": "auto", "retry_agent": "propose",
                    "launch": "propose", "followup": "propose"}

LABEL = {"notify": "人へ通知", "recollect": "進捗を再収集", "retry_agent": "役割AIの再実行",
         "launch": "テンプレートの起動", "followup": "対応タスクの作成"}

# 対処の基本の見込みとリスク。効用 = 見込み − リスク + 実績による補正。
# A remedy's base value and risk; utility adds what track record says.
BASE_UTILITY = {"recollect": 70, "retry_agent": 65, "launch": 60, "followup": 40, "notify": 30}
RISK_COST = {"recollect": 0, "retry_agent": 5, "launch": 15, "followup": 0, "notify": 0}

# 診断ごとの、通知以外の対処の候補。通知は、診断が出たとき必ず添える。
# Remedies other than notifying, per diagnosis; notifying always accompanies a new one.
LADDERS: dict[str, tuple[str, ...]] = {
    "overload": ("followup",),
    "project_risk": ("launch", "followup"),
    "agent_failure": ("retry_agent", "followup"),
    "collection_failing": ("recollect", "followup"),
    "capacity_shortage": ("followup",),
    "estimate_risk": ("followup",),
}

MAX_RETRIES_PER_TASK = 2


class JudgmentError(ValueError):
    """判断の設定が不正 / a malformed judgment config."""


@dataclass(frozen=True)
class LaunchEntry:
    id: str
    template: str
    addresses: tuple[str, ...]            # どの診断に使ってよいか
    params: dict[str, str] = field(default_factory=dict)
    max_per_day: int = 1


@dataclass(frozen=True)
class JudgmentConfig:
    enabled: bool = True
    autonomy: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AUTONOMY))
    launches: tuple[LaunchEntry, ...] = ()
    min_severity: int = 40                # これ未満の診断は動かない
    cooldown_hours: float = 24.0          # 同じ診断への対処の間隔
    recheck_hours: float = 24.0           # 対処のあと、この時間たっても診断が続けば「効かなかった」
    renotify_hours: float = 72.0          # 続く診断の再通知の間隔
    max_actions_per_cycle: int = 3
    max_attempts: int = 3                 # 同じ対処を、1 回の診断の中で試す上限(失敗したとき)
    max_auto_per_day: int = 10
    breaker_failures: int = 3
    breaker_hours: float = 24.0


def load_judgment(raw: dict[str, Any] | None) -> JudgmentConfig | None:
    """設定を検証する。`pmo_core.judgment` が無ければ None（判断はしない）。"""
    if raw is None:
        return None
    raw = raw or {}

    autonomy = dict(DEFAULT_AUTONOMY)
    for remedy, level in (raw.get("autonomy") or {}).items():
        if remedy not in REMEDIES:
            raise JudgmentError(f"autonomy: 知らない対処 {remedy!r}（{', '.join(REMEDIES)}）")
        if level not in AUTONOMY:
            raise JudgmentError(f"autonomy.{remedy}: {', '.join(AUTONOMY)} のいずれか: {level!r}")
        autonomy[remedy] = level

    launches, seen = [], set()
    for item in raw.get("launch") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("template"):
            raise JudgmentError("launch の各項目に id と template が必要です")
        if item["id"] in seen:
            raise JudgmentError(f"launch の id が重複しています: {item['id']}")
        seen.add(item["id"])
        addresses = tuple(item.get("addresses") or ())
        unknown = [a for a in addresses if a not in DIAGNOSES]
        if not addresses or unknown:
            raise JudgmentError(
                f"launch '{item['id']}': addresses は {', '.join(DIAGNOSES)} から"
                + (f"（知らない: {unknown}）" if unknown else ""))
        launches.append(LaunchEntry(
            id=str(item["id"]), template=str(item["template"]), addresses=addresses,
            params={str(k): str(v) for k, v in (item.get("params") or {}).items()},
            max_per_day=max(1, int(item.get("max_per_day", 1)))))

    limits = raw.get("limits") or {}
    breaker = raw.get("circuit_breaker") or {}
    try:
        return JudgmentConfig(
            enabled=bool(raw.get("enabled", True)), autonomy=autonomy,
            launches=tuple(launches),
            min_severity=int(raw.get("min_severity", 40)),
            cooldown_hours=float(limits.get("cooldown_hours", 24)),
            recheck_hours=float(limits.get("recheck_hours", 24)),
            renotify_hours=float(limits.get("renotify_hours", 72)),
            max_actions_per_cycle=max(1, int(limits.get("max_actions_per_cycle", 3))),
            max_attempts=max(1, int(limits.get("max_attempts", 3))),
            max_auto_per_day=max(0, int(limits.get("max_auto_per_day", 10))),
            breaker_failures=max(1, int(breaker.get("failures", 3))),
            breaker_hours=float(breaker.get("hours", 24)))
    except (TypeError, ValueError) as exc:
        raise JudgmentError(f"数値の設定が不正です: {exc}") from exc


# =============================================================================
# 診断 / diagnosis
# =============================================================================

@dataclass(frozen=True)
class Diagnosis:
    kind: str
    subject: str
    severity: int
    title: str
    evidence: tuple[str, ...] = ()
    project: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.subject}"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "severity": self.severity,
                "title": self.title, "evidence": list(self.evidence), "project": self.project}


def _clamp(value: float, low: int = 0, high: int = 100) -> int:
    return int(max(low, min(high, round(value))))


def _titles(tasks: list[Any], limit: int = 3) -> str:
    return "、".join(t.title[:30] for t in tasks[:limit])


def diagnose(briefing: dict[str, Any], active: list[Any], *, now: datetime,
             has_members: bool, collect_interval_minutes: int | None) -> list[Diagnosis]:
    """いま何が問題かを、数えて決める（言語モデルは使わない）。"""
    found: list[Diagnosis] = []

    # 1. 過負荷 / overload
    for item in briefing.get("overloaded_members", []):
        name = item["member"]
        mine = [t for t in active if (t.assignee or "").lower() == name.lower()]
        risky = [t for t in mine if t.score >= 60]
        excess = item["load"] - item["capacity"]
        found.append(Diagnosis(
            "overload", name, _clamp(40 + 10 * excess + 5 * len(risky)),
            f"{name} の負荷が上限を超えています（{item['load']}/{item['capacity']}）",
            (f"未完了 {item['load']} 件に対して上限 {item['capacity']} 件",
             *([f"うち点数 60 以上の高リスクが {len(risky)} 件: {_titles(risky)}"] if risky else [])),
            data={"member": name, "tasks": [t.id for t in risky]}))

    # 2. リスクの高いプロジェクト / project at risk
    for project in briefing.get("projects", []):
        if project["level"] not in ("high", "critical") or not project["alert_count"]:
            continue
        base = 80 if project["level"] == "critical" else 60
        alerts = [a for a in briefing.get("alerts", [])
                  if (a.get("project") or "").lower() == project["project"].lower()]
        found.append(Diagnosis(
            "project_risk", project["project"],
            _clamp(base + min(20, 5 * project["alert_count"])),
            f"プロジェクト {project['project']} のリスクが {project['level']}"
            f"（警告 {project['alert_count']} 件）",
            tuple(f"{a['title'][:30]} — {a['message']}" for a in alerts[:3]),
            project=project["project"], data={"project": project["project"],
                                              "level": project["level"]}))

    # 3. 役割AIの失敗 / role AI failures
    failing: dict[str, list[Any]] = {}
    for task in active:
        for entry in reversed(task.dispatches or []):
            agent = str(entry.get("agent", ""))
            if (task.assignee or "").lower() == agent.lower():
                if entry.get("status") in ("failed", "abandoned"):
                    failing.setdefault(agent, []).append(task)
                break
    for agent, tasks in failing.items():
        found.append(Diagnosis(
            "agent_failure", agent, _clamp(50 + 10 * len(tasks), high=90),
            f"役割AI {agent} の実行が失敗しています（{len(tasks)} 件）",
            tuple(f"{t.title[:30]}: {(t.dispatches[-1].get('error') or '')[:60]}" for t in tasks[:3]),
            data={"agent": agent, "tasks": [t.id for t in tasks]}))

    # 4. 進捗の収集が失敗している／止まっている / collection failing or stalled
    collection = briefing.get("collection")
    if collection:
        sources = collection.get("sources") or []
        all_failed = bool(sources) and all(s.get("error") for s in sources)
        problems = []
        if collection.get("error"):
            problems.append(collection["error"])
        elif all_failed:
            problems.append("全ての収集元が失敗: "
                            + "; ".join(str(s["error"])[:50] for s in sources[:2]))
        if collection.get("failed", 0) >= 3:
            problems.append(f"再読み込みの失敗が {collection['failed']} 件")
        stale = False
        if collect_interval_minutes:
            try:
                age = (now - datetime.fromisoformat(collection["at"])).total_seconds() / 60
                stale = age > collect_interval_minutes * 3
                if stale:
                    problems.append(f"最後の収集から {age / 60:.0f} 時間")
            except (KeyError, ValueError):
                pass
        if problems:
            found.append(Diagnosis(
                "collection_failing", "collection", 55 if stale or collection.get("error") else 45,
                "進捗の自動収集がうまくいっていません", tuple(problems)))

    # 5. 容量不足 / capacity shortage
    unassignable = briefing.get("unassignable", [])
    if has_members and len(unassignable) >= 3:
        found.append(Diagnosis(
            "capacity_shortage", "team", _clamp(40 + 5 * len(unassignable), high=80),
            f"割り当て先の空きが無い未完了タスクが {len(unassignable)} 件あります",
            (f"例: {'、'.join(u['title'][:30] for u in unassignable[:3])}",)))

    # 6. 見積りでは間に合わないタスクの増加 / tasks the estimate says cannot make it
    pace_risky = [t for t in active if any("実績ペース" in r for r in t.reasons)]
    if len(pace_risky) >= 3:
        found.append(Diagnosis(
            "estimate_risk", "team", _clamp(40 + 3 * len(pace_risky), high=70),
            f"見積りとペースでは期限に間に合わないタスクが {len(pace_risky)} 件あります",
            (f"例: {_titles(pace_risky)}",)))
    return found


# =============================================================================
# 対処の選択 / choosing a remedy
# =============================================================================

def judgment_id(fingerprint: str, remedy: str, since: str, attempt: int = 1) -> str:
    """判断の記録の id。「どの診断の、どの回の、どの対処の、何回目」を表す（冪等）。

    失敗した対処を再び試すとき、同じ id では記録を作り直せない。試行回数を
    id に含めて、別の記録にする。
    A failed remedy retried needs a record of its own, so the attempt number is part of
    the id.
    """
    safe = "".join(c if c.isalnum() or c in ":-_." else "_" for c in fingerprint)
    base = f"PMO:jd:{safe}:{remedy}:{since[:16].replace(':', '')}"
    return base if attempt <= 1 else f"{base}:a{attempt}"


def success_rate(stats: dict[str, dict[str, int]], kind: str, remedy: str) -> float:
    """その診断に、その対処が効いた割合（ラプラス平滑。実績が無ければ 0.5）。"""
    record = stats.get(f"{kind}|{remedy}") or {}
    ok, bad = record.get("ok", 0), record.get("bad", 0)
    return (ok + 1) / (ok + bad + 2)


def remedy_score(kind: str, remedy: str, stats: dict[str, dict[str, int]]) -> float:
    return (BASE_UTILITY[remedy] - RISK_COST[remedy]
            + 40 * (success_rate(stats, kind, remedy) - 0.5))


def candidates(diagnosis: Diagnosis, available: set[str], episode: dict[str, Any],
               stats: dict[str, dict[str, int]], max_attempts: int = 3) -> list[str]:
    """この診断に使える対処を、効用の高い順に。

    除くもの: 効かなかったもの、承認待ち・実行中のもの、失敗が `max_attempts` 回に
    達したもの（尽きたら次の段へ進む）。失敗が上限に達するまでは、同じ対処を再び試せる。
    Excluded: remedies that did not work, are awaiting approval or running, or have
    failed `max_attempts` times (exhausted — the ladder moves on). Until then a failed
    remedy may be tried again.
    """
    executions = episode.get("executions", [])
    spent = {ex["remedy"] for ex in executions
             if ex.get("ineffective") or ex.get("state") in ("proposed", "approved", "executing")}
    for remedy in {ex["remedy"] for ex in executions}:
        if sum(1 for ex in executions if ex["remedy"] == remedy
               and ex.get("state") == "failed" and not ex.get("forgiven")) >= max_attempts:
            spent.add(remedy)
    options = [r for r in LADDERS.get(diagnosis.kind, ()) if r in available and r not in spent]
    return sorted(options, key=lambda r: -remedy_score(diagnosis.kind, r, stats))


def rationale(diagnosis: Diagnosis, remedy: str, stats: dict[str, dict[str, int]],
              level: str) -> str:
    rate = success_rate(stats, diagnosis.kind, remedy)
    record = stats.get(f"{diagnosis.kind}|{remedy}") or {}
    history = (f"過去の実績: 効いた {record.get('ok', 0)} 回・効かなかった {record.get('bad', 0)} 回"
               f"（効く見込み {rate:.0%}）") if record else "過去の実績: まだ無い（見込み 50%）"
    how = {"auto": "自律度 auto: 実行する", "propose": "自律度 propose: 提案して人の承認を待つ"}[level]
    return (f"{diagnosis.title}。{LABEL[remedy]}を選んだ。"
            f"根拠: {'; '.join(diagnosis.evidence) or '-'}。{history}。{how}。")


# =============================================================================
# 制御（止める・戻す）/ control: pause and reset
# =============================================================================

def read_control(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_control(path: Path, **changes: Any) -> dict[str, Any]:
    """制御ファイルを更新する。書くのは CLI だけ、読むのは常駐だけ（衝突しない）。"""
    state = read_control(path)
    state.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return state
