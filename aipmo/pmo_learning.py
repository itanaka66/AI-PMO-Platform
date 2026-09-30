"""過去の実績からの学習 — 重みの調整。

タスク台帳は、完了を観測するたびに「期限に対して何日遅れたか」を実績として
残している（`TaskEngine.outcomes`）。ここではその実績から、次の 2 つだけを
調整する。

  1. メンバーごとの実効キャパシティの係数
     期限内に終えた割合がチーム平均より高い人には少し多く、低い人には
     少なく割り当てる。
  2. 遅れやすいラベルへの加点
     チーム平均より遅れやすいラベルのタスクは、順位を少し上げる —
     遅れる前に手を付けるため。

**学習と言っても統計の補正であり、言語モデルは使わない。** 同じ実績から
毎回同じ係数が出るので、「なぜこの人の割当が減ったのか」を後から説明できる。
暴れないように、次の 4 つの歯止めを置く。

  - 最低サンプル数（既定 5 件）に満たないものは、調整しない。
  - サンプルが少ないほど平均へ寄せる（n / (n + 5) の縮小）。
  - 係数は 0.5〜1.5、ラベルの加点は 0〜15 に丸める。
  - 期限の無いタスクは材料にしない（遅れが定義できない）。

期限の無いタスクの完了、担当者不明の完了は、それぞれ該当する集計から外れる。

Learning from track record. The ledger records, each time it observes a task
finishing, how many days late it was. From those outcomes this module adjusts
exactly two things: a per-member capacity factor (people who finish on time
more often than the team gets a little more work, and vice versa) and a bonus
for labels that tend to finish late (so they are picked up before they slip).

This is a statistical correction, not a language model: the same outcomes give
the same factors every time, so "why did this person's load shrink" stays
answerable. Four guards keep it from swinging: a minimum sample count,
shrinkage toward the team average for small samples (n / (n + 5)), clamping
(factor 0.5–1.5, bonus 0–15), and ignoring tasks without a due date.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_MIN_SAMPLES = 5
SHRINK_CONSTANT = 5
FACTOR_RANGE = (0.5, 1.5)
LABEL_BONUS_CAP = 15
LABEL_BONUS_SCALE = 30   # 遅延率が平均より 10pt 高い（十分な標本）→ +3 点


@dataclass
class LearnedModel:
    samples: int = 0                       # 期限つきの完了実績の数
    baseline_late_rate: float | None = None
    member_factor: dict[str, float] = field(default_factory=dict)   # 小文字の名前
    label_bonus: dict[str, int] = field(default_factory=dict)       # 小文字のラベル
    # 説明用：各係数の根拠（件数と遅延率）。
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _shrink(n: int) -> float:
    return n / (n + SHRINK_CONSTANT)


def learn(outcomes: list[dict[str, Any]],
          min_samples: int = DEFAULT_MIN_SAMPLES) -> LearnedModel:
    """実績から係数を作る / derive the factors from outcomes."""
    dated = [o for o in outcomes if o.get("late_days") is not None]
    model = LearnedModel(samples=len(dated))
    if len(dated) < min_samples:
        return model   # 全体でも足りなければ何も調整しない / not enough to judge

    baseline = sum(1 for o in dated if o["late_days"] > 0) / len(dated)
    model.baseline_late_rate = round(baseline, 4)

    by_member: dict[str, list[dict[str, Any]]] = {}
    by_label: dict[str, list[dict[str, Any]]] = {}
    for outcome in dated:
        who = (outcome.get("assignee") or "").strip().lower()
        if who:
            by_member.setdefault(who, []).append(outcome)
        for label in outcome.get("labels") or []:
            by_label.setdefault(str(label).lower(), []).append(outcome)

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

    return model
