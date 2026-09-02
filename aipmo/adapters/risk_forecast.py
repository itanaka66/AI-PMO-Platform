"""Risk / Forecast アダプタ / Risk & Forecast adapter.

WBS の遅延予測とドリフト検出。sprint_health / jira_agile と同じ設計方針を
そのまま引き継ぐ：集計は AI にやらせない。残工数・必要日数・ドリフト日数は
ここで計算し、言語モデルには「その数字をどう伝えるか」だけを担当させる。

The aggregation is never the model's job here either. Remaining effort,
days needed, and drift are computed in this module; a language model only
ever phrases the resulting numbers, the same reasoning that kept
sprint_health's percentages and jira_agile's day counts out of the model's
hands.

外部 API を一切呼ばない、純粋な計算アダプタ / A pure calculation adapter
------------------------------------------------------------------------
入力（タスク一覧・ベロシティ・締切）は呼び出し側から渡す。このアダプタ
自身は Jira にも Postgres にも接続しない。WBS の実体はテナントによって
Jira だったり独自の表だったりするため、取得方法をここに固定しない。
純粋関数にしておくとテストが容易で、計算ロジックのドリフトを取得ロジック
の都合と切り離して検証できる。

Inputs (the task list, velocity, deadline) come from whatever produced them
upstream. This adapter never talks to Jira or Postgres directly: where a
WBS actually lives varies by tenant, and a pure function is trivial to
test exhaustively, separate from how the data got fetched.

ヒステリシスについて / On hysteresis
------------------------------------
`classify_drift` は「今回のドリフトを提案すべきか」を、直前に記録した
ドリフト量と2つの閾値（`detect_at` / `clear_below`）から決める。閾値を
1つだけにすると、値がその近くで揺れるたびに提案が乱発される。検出と解除に
別の閾値を置くことで、閾値ちょうどでの往復を吸収する。

`classify_drift` decides whether today's drift is worth proposing from the
last recorded drift and two thresholds rather than one, so a value
oscillating near the boundary does not flap between alerting and clearing
on every check.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .base import Adapter, action

DEFAULT_TIER2_AT = 3.0
DEFAULT_TIER3_AT = 10.0
DEFAULT_DETECT_AT = 3.0
DEFAULT_CLEAR_BELOW = 1.0


class RiskForecastAdapter(Adapter):
    name = "risk_forecast"

    def __init__(
        self,
        tier2_at: float = DEFAULT_TIER2_AT,
        tier3_at: float = DEFAULT_TIER3_AT,
        detect_at: float = DEFAULT_DETECT_AT,
        clear_below: float = DEFAULT_CLEAR_BELOW,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        if clear_below >= detect_at:
            raise ValueError(
                "risk_forecast: clear_below は detect_at より小さくなければ"
                "なりません / clear_below must be less than detect_at, or "
                "there is no hysteresis at all"
            )
        self.tier2_at = tier2_at
        self.tier3_at = tier3_at
        self.detect_at = detect_at
        self.clear_below = clear_below

    @action()
    def forecast(
        self,
        tasks: list[dict[str, Any]],
        velocity_per_day: float,
        deadline: str,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """残工数・必要日数・ドリフトを計算する。

        tasks の各要素は {"key": ..., "done": bool, "effort": number|None} を
        想定する。effort が None（見積もり無し）の項目は残工数の合計から
        除外し、unestimated として別に返す — 0 として扱うと「残り無し」に
        見えてしまう（jira_agile._sum_points と同じ理由）。
        """
        as_of_date = _parse_date(as_of) if as_of else datetime.now(timezone.utc).date()
        deadline_date = _parse_date(deadline)

        remaining = [t for t in tasks if not t.get("done")]
        unestimated = [t["key"] for t in remaining if t.get("effort") is None]
        estimated_remaining = [t for t in remaining if isinstance(t.get("effort"), (int, float))]
        remaining_effort = sum(t["effort"] for t in estimated_remaining) if estimated_remaining else 0.0

        days_to_deadline = (deadline_date - as_of_date).days

        if velocity_per_day > 0:
            projected_days_needed = remaining_effort / velocity_per_day
            projected_completion = as_of_date + timedelta(days=_ceil(projected_days_needed))
            drift_days = projected_days_needed - days_to_deadline
        else:
            # ベロシティが不明・ゼロのときに 0 除算や偽の楽観予測を出さない。
            # Zero/unknown velocity must not divide by zero or produce a
            # falsely optimistic forecast.
            projected_days_needed = None
            projected_completion = None
            drift_days = None

        total = len(tasks)
        done_count = total - len(remaining)

        return {
            "as_of": as_of_date.isoformat(),
            "deadline": deadline_date.isoformat(),
            "days_to_deadline": days_to_deadline,
            "total_tasks": total,
            "done_tasks": done_count,
            "percent_done": round(done_count / total * 100) if total else 0,
            "remaining_effort": remaining_effort,
            "unestimated": unestimated,
            "velocity_per_day": velocity_per_day,
            "projected_days_needed": projected_days_needed,
            "projected_completion": (
                projected_completion.isoformat() if projected_completion else None
            ),
            # 正 = 遅延、0 = ちょうど間に合う、負 = 前倒し。None = 予測不能。
            # Positive = behind schedule, 0 = on time, negative = ahead,
            # None = cannot be forecast.
            "drift_days": drift_days,
        }

    @action()
    def classify_drift(
        self,
        drift_days: float | None,
        previous_drift_days: float | None = None,
        pending_count: int = 0,
    ) -> dict[str, Any]:
        """ドリフト量から、階層と「今回提案すべきか」を決める。

        `pending_count` は、同じテナントの承認待ち提案の件数を
        呼び出し側（pending_wbs_proposals の件数）から渡す。0より大きい
        場合は既に何か承認待ちであると扱う。判定（> 0）をここで行うのは、
        DSL の `inputs:` が単純なプレースホルダ置換のみで比較演算子を
        評価できないため（`{{ x }} > 0` は文字列 "N > 0" になってしまい、
        テンプレート側では判定できない）。

        `pending_count` is the number of pending proposals for this tenant,
        supplied by the caller (from pending_wbs_proposals's row count).
        Any value above zero is treated as "something is already pending".
        The `> 0` comparison happens here rather than in the template
        because the DSL's `inputs:` only does placeholder substitution, not
        expression evaluation — `{{ x }} > 0` would render as the literal
        string "N > 0", which a template has no way to act on.
        """
        if drift_days is None:
            return {
                "tier": None,
                "should_propose": False,
                "reason": "velocity is unknown or zero; drift cannot be forecast",
            }

        if drift_days >= self.tier3_at:
            tier = 3
        elif drift_days >= self.tier2_at:
            tier = 2
        else:
            tier = 1

        if tier == 1:
            return {
                "tier": tier, "should_propose": False,
                "reason": f"drift_days={drift_days:.1f} is within the tier-1 (minor) range",
            }

        if pending_count > 0:
            return {
                "tier": tier, "should_propose": False,
                "reason": "a proposal for this drift is already pending review",
            }

        # ヒステリシス: 前回値が無い、または前回が解除域だった場合は
        # detect_at を跨いだかで新規検出とみなす。前回も既に detect_at
        # 以上だった場合は「継続中」として、clear_below を下回らない限り
        # 提案し続けてよい（悪化・横ばいを追い続けるため）。
        was_active = previous_drift_days is not None and previous_drift_days >= self.clear_below

        if was_active:
            should_propose = drift_days >= self.clear_below
            reason = (
                f"ongoing: drift_days={drift_days:.1f}, "
                f"previous={previous_drift_days:.1f}"
                if should_propose else
                f"resolved: drift_days={drift_days:.1f} fell below clear_below={self.clear_below}"
            )
        else:
            should_propose = drift_days >= self.detect_at
            reason = (
                f"newly detected: drift_days={drift_days:.1f} crossed "
                f"detect_at={self.detect_at}"
                if should_propose else
                f"drift_days={drift_days:.1f} has not yet crossed detect_at={self.detect_at}"
            )

        return {"tier": tier, "should_propose": should_propose, "reason": reason}

    @action()
    def dependency_impact(self, tasks: list[dict]) -> dict:
        """WBS のタスク間依存から、ブロック状況・波及範囲・クリティカルパスを
        機械的に計算する。AI に依存関係グラフの計算をさせない -- 「集計は
        AI にやらせない」という既存方針をここでも徹底する。

        tasks の各要素は forecast() と同じ形に、依存先の一覧
        depends_on: list[str]（他タスクの key）を加えたもの。
        depends_on の無い/空のタスクは依存無しとして扱う。

        戻り値:
          blocked            未完了で、依存先が1つでも未完了なタスクの key
          cycles             循環依存が見つかった場合、その key の集合の列挙
                              （見つかっても他の計算は止めない -- 循環に
                              関わる key はクリティカルパス計算から除外する）
          critical_path      残工数の合計が最大になる依存チェーン（key の列、
                              依存の浅い方から深い方の順）
          critical_path_effort  そのチェーンの残工数合計
          critical_path_has_unestimated  そのチェーンに見積もり無しのタスクが
                              含まれるか（含まれる場合、上の合計は下限でしか
                              ない）
          downstream_impact  未完了タスク each -> それに（直接・間接に）
                              依存している他の未完了タスクの key のリスト。
                              「このタスクが遅れたら何が道連れになるか」

        Computes blocking status, downstream impact, and the critical path
        from task dependencies mechanically -- the same "aggregation is
        never the model's job" discipline applied elsewhere in this module.

        Each task is shaped like forecast()'s input plus depends_on: a list
        of other tasks' keys. A missing/empty depends_on means no
        dependencies.

        Returns: blocked (not-done tasks with an unfinished dependency),
        cycles (any circular-dependency groups found -- detected without
        halting the rest of the computation; keys in a cycle are excluded
        from the critical-path calculation), critical_path (the key chain
        with the largest total remaining effort, shallowest dependency
        first), critical_path_effort (that chain's total), and
        critical_path_has_unestimated (whether the chain includes a task
        with no effort estimate, making the total a lower bound only).
        downstream_impact maps each not-done task to the not-done tasks
        that (directly or transitively) depend on it -- what else gets
        dragged down if this one slips.
        """
        by_key: dict[str, dict] = {t["key"]: t for t in tasks if t.get("key")}
        depends_on: dict[str, list[str]] = {
            key: [d for d in (t.get("depends_on") or []) if d in by_key]
            for key, t in by_key.items()
        }

        # ブロック状況: 未完了で、依存先が1つでも未完了。
        blocked = [
            key for key, t in by_key.items()
            if not t.get("done") and any(
                not by_key[dep].get("done") for dep in depends_on[key]
            )
        ]

        # 循環検出（Kahn 法）。処理しきれずに残った key が循環に関与している。
        in_degree = {key: 0 for key in by_key}
        dependents: dict[str, list[str]] = {key: [] for key in by_key}
        for key, deps in depends_on.items():
            for dep in deps:
                dependents[dep].append(key)
                in_degree[key] += 1

        queue = [key for key, degree in in_degree.items() if degree == 0]
        order: list[str] = []
        remaining_degree = dict(in_degree)
        while queue:
            key = queue.pop()
            order.append(key)
            for nxt in dependents[key]:
                remaining_degree[nxt] -= 1
                if remaining_degree[nxt] == 0:
                    queue.append(nxt)

        cyclic_keys = set(by_key) - set(order)
        cycles = [sorted(cyclic_keys)] if cyclic_keys else []

        # クリティカルパス: 残工数（見積もり無しは0扱い）で最長のチェーン。
        # 循環に関与する key は依存として数えない（無限再帰を避けるため、
        # 単に「そこで途切れる」扱いにする -- 循環そのものは上の cycles で
        # 別途報告済み）。
        remaining_effort: dict[str, float] = {}
        has_unestimated: dict[str, bool] = {}
        for key, t in by_key.items():
            if t.get("done"):
                remaining_effort[key] = 0.0
                has_unestimated[key] = False
            else:
                effort = t.get("effort")
                remaining_effort[key] = float(effort) if isinstance(effort, (int, float)) else 0.0
                has_unestimated[key] = not isinstance(effort, (int, float))

        longest: dict[str, float] = {}
        longest_unest: dict[str, bool] = {}
        best_predecessor: dict[str, str | None] = {}

        for key in order:
            best_dep = None
            best_value = 0.0
            best_unest = False
            for dep in depends_on[key]:
                if dep in cyclic_keys:
                    continue
                value = longest.get(dep, 0.0)
                if best_dep is None or value > best_value:
                    best_dep = dep
                    best_value = value
                    best_unest = longest_unest.get(dep, False)
            longest[key] = remaining_effort[key] + best_value
            longest_unest[key] = has_unestimated[key] or best_unest
            best_predecessor[key] = best_dep

        if longest:
            end_key = max(longest, key=lambda k: longest[k])
            critical_path_effort = longest[end_key]
            critical_path_has_unestimated = longest_unest[end_key]
            chain: list[str] = []
            cursor: str | None = end_key
            while cursor is not None:
                chain.append(cursor)
                cursor = best_predecessor.get(cursor)
            critical_path = list(reversed(chain))
        else:
            critical_path = []
            critical_path_effort = 0.0
            critical_path_has_unestimated = False

        # 波及範囲: 各未完了タスクに、それに依存している未完了タスクを
        # 逆向きに辿って集める（直接・間接とも）。
        downstream_impact: dict[str, list[str]] = {}
        for key, t in by_key.items():
            if t.get("done"):
                continue
            seen: set[str] = set()
            stack = list(dependents.get(key, []))
            while stack:
                current = stack.pop()
                if current in seen or current == key:
                    continue
                seen.add(current)
                if not by_key[current].get("done"):
                    stack.extend(dependents.get(current, []))
            downstream_impact[key] = sorted(
                k for k in seen if not by_key[k].get("done")
            )

        return {
            "blocked": sorted(blocked),
            "cycles": cycles,
            "critical_path": critical_path,
            "critical_path_effort": critical_path_effort,
            "critical_path_has_unestimated": critical_path_has_unestimated,
            "downstream_impact": downstream_impact,
        }



def _parse_date(value: str) -> date:
    text = value.strip()
    if "T" in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date()
    return date.fromisoformat(text)


def _ceil(value: float) -> int:
    """繰り上げ。残り半日を 0 日と出すと、もう終わっているように読める
    （jira_agile._days_until と同じ理由）。"""
    as_int = int(value)
    return as_int if value == as_int else as_int + 1
