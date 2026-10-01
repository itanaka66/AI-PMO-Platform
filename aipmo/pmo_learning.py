"""過去の実績からの学習 — 重みの調整。

タスク台帳は、完了を観測するたびに「期限に対して何日遅れたか」「見積りは何点で、
実際に何日かかったか」を実績として残している（`TaskEngine.outcomes`）。ここでは
その実績から、次の 4 つだけを調整する。

  1. メンバーごとの実効キャパシティの係数
     期限内に終えた割合がチーム平均より高い人には少し多く、低い人には
     少なく割り当てる。
  2. 遅れやすいラベルへの加点
     チーム平均より遅れやすいラベルのタスクは、順位を少し上げる —
     遅れる前に手を付けるため。
  3. 優先度の重みの補正（±10 点まで）
     その優先度のタスクが、全体より遅れやすければ重みを増し、確実に間に合って
     いれば減らす。「High と書いてあるが、実際は危なくない」「Low と書いてあるが、
     いつも遅れる」を、実績で直す。
  4. 見積り精度とペース（1 点あたりの日数）
     見積り（点）と実際にかかった日数から、チーム・メンバーごとのペースを求める。
     見積りの当たり具合（1 件を除いて予測し直したときの誤差の中央値）も測り、
     **当たっているときだけ**、「見積りとペースでは期限に間に合わない」タスクの
     順位を上げる。当たっていなければ使わない。

**学習と言っても統計の補正であり、言語モデルは使わない。** 同じ実績から
毎回同じ係数が出るので、「なぜこの人の割当が減ったのか」を後から説明できる。
暴れないように、次の歯止めを置く。

  - 最低サンプル数（既定 5 件）に満たないものは、調整しない。
  - サンプルが少ないほど平均へ寄せる（n / (n + 5) の縮小）。
  - 係数は 0.5〜1.5、ラベルの加点は 0〜15、優先度の補正は −10〜+10 に丸める。
  - 期限の無いタスクは遅れの材料にしない。見積りや着手の記録が無いものは
    ペースの材料にしない。
  - ペースは平均でなく**中央値**（一つの極端な実績に引きずられない）。
  - 見積り誤差が大きい（既定: 中央値で 75% 超）なら、ペースは順位に使わない。

実日数は「着手を観測した日から完了を観測した日まで」で、観測の粒度（定期実行の
間隔）の分だけ粗い。最短は 1 日として数える。

Learning from track record. The ledger records, each time it observes a task
finishing, how late it was and how many days it took against its estimate.
From those outcomes this module adjusts exactly four things: a per-member
capacity factor; a bonus for labels that finish late; a correction to each
priority's weight (up to ±10 — a priority that slips more than average weighs
more, one that reliably lands weighs less); and the team's and members' *pace*
(days per estimated point) together with how accurate the estimates are,
raising the rank of tasks that the estimate and pace say cannot make their due
date — **only when the estimates have proved accurate enough to trust**.

This is a statistical correction, not a language model: the same outcomes give
the same numbers every time. Guards: a minimum sample count, shrinkage toward
the team average for small samples, clamping, the *median* rather than the mean
for pace, and no use of pace when the estimates miss by too much.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

DEFAULT_MIN_SAMPLES = 5
SHRINK_CONSTANT = 5
FACTOR_RANGE = (0.5, 1.5)
LABEL_BONUS_CAP = 15
LABEL_BONUS_SCALE = 30       # 遅延率が平均より 10pt 高い（十分な標本）→ +3 点
PRIORITY_DELTA_CAP = 10
PRIORITY_SCALE = 40          # 遅延率が平均より 10pt 高い（十分な標本）→ +4 点
DEFAULT_MAX_ESTIMATE_ERROR = 0.75


@dataclass
class LearnedModel:
    samples: int = 0                       # 期限つきの完了実績の数
    baseline_late_rate: float | None = None
    member_factor: dict[str, float] = field(default_factory=dict)   # 小文字の名前
    label_bonus: dict[str, int] = field(default_factory=dict)       # 小文字のラベル
    priority_delta: dict[str, int] = field(default_factory=dict)    # 小文字の優先度
    # 見積りとペース。team / members は 1 点あたりの日数。
    # Estimates and pace: team / members are days per estimated point.
    pace: dict[str, Any] = field(default_factory=lambda: {
        "samples": 0, "team": None, "members": {}, "median_error": None,
        "reliable": False})
    # 説明用：各係数の根拠（件数と遅延率）。
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def signature(self) -> tuple[Any, ...]:
        """変化を判断ログに残すかの比較用。件数の増減だけでは変えない。

        What decides whether a change is worth logging; a mere sample-count
        increase does not.
        """
        pace = self.pace
        return (self.member_factor, self.label_bonus, self.priority_delta,
                bool(pace.get("reliable")),
                None if pace.get("team") is None else round(pace["team"], 1),
                {k: round(v, 1) for k, v in pace.get("members", {}).items()})


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _shrink(n: int) -> float:
    return n / (n + SHRINK_CONSTANT)


def _pace_samples(outcomes: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    """(担当者, 見積り点, 実日数)。見積りと着手の記録があるものだけ。"""
    samples = []
    for o in outcomes:
        effort, days = o.get("effort"), o.get("duration_days")
        if (isinstance(effort, (int, float)) and not isinstance(effort, bool) and effort > 0
                and isinstance(days, (int, float)) and not isinstance(days, bool)
                and days >= 0):
            samples.append(((o.get("assignee") or "").strip().lower(), float(effort),
                            float(max(1, days))))
    return samples


def _learn_pace(outcomes: list[dict[str, Any]], min_samples: int,
                max_error: float) -> dict[str, Any]:
    pace: dict[str, Any] = {"samples": 0, "team": None, "members": {},
                            "median_error": None, "reliable": False,
                            "max_error": max_error}
    samples = _pace_samples(outcomes)
    pace["samples"] = len(samples)
    if len(samples) < min_samples:
        return pace

    ratios = [days / effort for _, effort, days in samples]
    team = median(ratios)
    pace["team"] = round(team, 3)

    # 見積り精度: 自分を除いた中央値のペースで予測し直したときの、相対誤差の中央値。
    # 自分を含めて当てると、実際より良く見えてしまう。
    # Accuracy: predict each sample from the median pace of all the *others*; using
    # itself would flatter the estimates.
    errors = []
    for i, (_, effort, days) in enumerate(samples):
        others = ratios[:i] + ratios[i + 1:]
        predicted = effort * median(others)
        errors.append(abs(days - predicted) / days)
    pace["median_error"] = round(median(errors), 3)
    pace["reliable"] = pace["median_error"] <= max_error

    by_member: dict[str, list[float]] = {}
    for who, effort, days in samples:
        if who:
            by_member.setdefault(who, []).append(days / effort)
    for who, values in sorted(by_member.items()):
        if len(values) < min_samples:
            continue
        weight = _shrink(len(values))
        pace["members"][who] = round(weight * median(values) + (1 - weight) * team, 3)
    return pace


def learn(outcomes: list[dict[str, Any]],
          min_samples: int = DEFAULT_MIN_SAMPLES, *,
          priority: bool = True, estimates: bool = True,
          max_error: float = DEFAULT_MAX_ESTIMATE_ERROR) -> LearnedModel:
    """実績から係数を作る / derive the factors from outcomes."""
    model = LearnedModel()
    if estimates:
        model.pace = _learn_pace(outcomes, min_samples, max_error)

    dated = [o for o in outcomes if o.get("late_days") is not None]
    model.samples = len(dated)
    if len(dated) < min_samples:
        return model   # 遅れの材料が足りなければ、遅れ由来の調整はしない

    baseline = sum(1 for o in dated if o["late_days"] > 0) / len(dated)
    model.baseline_late_rate = round(baseline, 4)

    by_member: dict[str, list[dict[str, Any]]] = {}
    by_label: dict[str, list[dict[str, Any]]] = {}
    by_priority: dict[str, list[dict[str, Any]]] = {}
    for outcome in dated:
        who = (outcome.get("assignee") or "").strip().lower()
        if who:
            by_member.setdefault(who, []).append(outcome)
        for label in outcome.get("labels") or []:
            by_label.setdefault(str(label).lower(), []).append(outcome)
        level = (outcome.get("priority") or "").strip().lower()
        if level:
            by_priority.setdefault(level, []).append(outcome)

    for who, items in sorted(by_member.items()):
        if len(items) < min_samples:
            continue
        late_rate = sum(1 for o in items if o["late_days"] > 0) / len(items)
        # 期限内の割合の差を、縮小したうえで係数に。
        on_time_gap = (1 - late_rate) - (1 - baseline)
        factor = _clamp(1 + on_time_gap * _shrink(len(items)), *FACTOR_RANGE)
        factor = round(factor, 3)
        if factor != 1.0:
            model.member_factor[who] = factor
            model.evidence[f"member:{who}"] = {
                "samples": len(items), "late_rate": round(late_rate, 4)}

    for label, items in sorted(by_label.items()):
        if len(items) < min_samples:
            continue
        late_rate = sum(1 for o in items if o["late_days"] > 0) / len(items)
        gap = late_rate - baseline
        if gap <= 0:
            continue   # 平均より遅れやすいものだけ / only the slower-than-average
        bonus = round(_clamp(gap * LABEL_BONUS_SCALE * _shrink(len(items)),
                             0, LABEL_BONUS_CAP))
        if bonus > 0:
            model.label_bonus[label] = bonus
            model.evidence[f"label:{label}"] = {
                "samples": len(items), "late_rate": round(late_rate, 4)}

    if priority:
        for level, items in sorted(by_priority.items()):
            if len(items) < min_samples:
                continue
            late_rate = sum(1 for o in items if o["late_days"] > 0) / len(items)
            # ラベルと違い、減らす方向にも動かす（確実に間に合う優先度は軽くする）。
            # Unlike labels this also moves down: a priority that reliably lands weighs less.
            delta = round(_clamp((late_rate - baseline) * PRIORITY_SCALE * _shrink(len(items)),
                                 -PRIORITY_DELTA_CAP, PRIORITY_DELTA_CAP))
            if delta != 0:
                model.priority_delta[level] = delta
                model.evidence[f"priority:{level}"] = {
                    "samples": len(items), "late_rate": round(late_rate, 4)}

    return model
