"""WBS ファイルの読み込み・検証・進捗の算出。

PMO AI 自身の開発を、このリポジトリの `wbs/aipmo.yaml`（人が書き、PR で
レビューする）で管理するための部品。**進捗の根拠は証拠（evidence）**:
「完了」と書かれた作業は、それを示すファイル（と、その中の語句）が実在しなければ
ならない。書いた人の申告だけでは完了にならない — WBS が現実から静かに離れていく
のが、WBS 運用でいちばん起きやすい失敗だから。

- 構造の誤り（id の重複、存在しない依存先、循環、日付の誤り）→ **error**
- 完了と書いてあるのに証拠が無い（ファイルが無い／語句が無い）→ **error**
- 証拠が無い完了、証拠が揃っているのに未完了、完了なのに依存先が未完了、
  期限超過、見積りなし → **warning**

集計はすべて決定論的で、言語モデルは使わない（[aipmo/portfolio.py]
(aipmo/portfolio.py) と同じ理由）。残工数・遅延予測・クリティカルパスは、既存の
`risk_forecast` の計算をそのまま使う。

A WBS kept in a file (`wbs/aipmo.yaml`, written by people, reviewed in PRs) for
managing this project's own development. **Progress rests on evidence**: a
node marked done must have the files — and the phrases inside them — that show
it. A claim alone does not make it done, because a WBS quietly drifting from
reality is the commonest way WBS practice fails. Everything is deterministic;
forecast and critical path reuse `risk_forecast` unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .adapters.risk_forecast import RiskForecastAdapter
from .messages import translate

STATUSES = ("done", "in_progress", "todo", "blocked")
_DISPLAY = {"done": "Done", "in_progress": "In Progress", "todo": "To Do",
            "blocked": "Blocked"}
_PRIORITY_RANK = {"highest": 0, "high": 1, "medium": 2, "low": 3, "lowest": 4}

MAX_WBS_BYTES = 1_000_000
MAX_EVIDENCE_BYTES = 2_000_000
DEFAULT_VELOCITY_WINDOW_DAYS = 28


class WbsError(Exception):
    """WBS ファイルを読めない／形が違う / the file cannot be read or is malformed."""


@dataclass
class Problem:
    level: str                  # "error" | "warning"
    code: str
    node: str | None
    message: str
    # 言語ごとの文章にするための差し込み値（`aipmo/messages.py` の `wp_<code>`）。無いものは message のまま。
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "node": self.node,
                "message": self.message}


@dataclass
class Node:
    id: str
    name: str
    status: str = "todo"
    effort: float | None = None
    depends_on: list[str] = field(default_factory=list)
    owner: str | None = None
    priority: str | None = None
    due: date | None = None
    done_on: date | None = None
    evidence: list[str] = field(default_factory=list)
    notes: str | None = None
    children: list[Node] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def done(self) -> bool:
        return self.status == "done"

    def path_names(self) -> list[str]:
        names, node = [], self
        while node is not None:
            names.append(node.name)
            node = node.parent        # type: ignore[assignment]
        return list(reversed(names))

    def leaves(self) -> list[Node]:
        if self.is_leaf:
            return [self]
        return [leaf for child in self.children for leaf in child.leaves()]


@dataclass
class Wbs:
    id: str
    name: str
    deadline: date | None
    velocity_per_day: float | None
    velocity_window_days: int
    roots: list[Node]

    def all_nodes(self) -> list[Node]:
        out: list[Node] = []

        def walk(node: Node) -> None:
            out.append(node)
            for child in node.children:
                walk(child)

        for root in self.roots:
            walk(root)
        return out

    def leaves(self) -> list[Node]:
        return [leaf for root in self.roots for leaf in root.leaves()]


# =============================================================================
# 読み込み / loading
# =============================================================================

def _as_date(value: Any, where: str, problems: list[Problem]) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        problems.append(Problem("error", "bad_date", where,
                                f"日付を読めません / cannot read the date: {value!r}"))
        return None


def _build_node(raw: Any, parent: Node | None, problems: list[Problem]) -> Node | None:
    if not isinstance(raw, dict):
        problems.append(Problem("error", "bad_node", None,
                                f"ノードがマッピングではありません: {raw!r}"))
        return None
    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        # `1.10` と書くと YAML は数値 1.1 として読む。黙って別の id にしない。
        # YAML reads an unquoted 1.10 as the number 1.1; never silently re-id.
        problems.append(Problem(
            "error", "bad_id", None,
            f"id は文字列で書いてください（\"1.10\" のように引用符で囲む）: {raw_id!r} "
            f"/ ids must be quoted strings"))
        return None
    node_id = raw_id.strip()
    name = str(raw.get("name") or "").strip()
    if not name:
        problems.append(Problem("error", "empty_name", node_id, "name がありません"))

    status = str(raw.get("status") or "todo").strip().lower()
    node = Node(id=node_id, name=name or node_id, status=status, parent=parent)
    if status not in STATUSES:
        problems.append(Problem(
            "error", "bad_status", node_id,
            f"status は {', '.join(STATUSES)} のいずれか: {status!r}"))
        node.status = "todo"

    effort = raw.get("effort")
    if effort is not None:
        if isinstance(effort, bool) or not isinstance(effort, (int, float)) or effort < 0:
            problems.append(Problem("error", "bad_effort", node_id,
                                    f"effort は 0 以上の数値: {effort!r}"))
        else:
            node.effort = float(effort)

    depends = raw.get("depends_on") or []
    node.depends_on = [str(d).strip() for d in (depends if isinstance(depends, list)
                                                else [depends])]
    node.owner = str(raw["owner"]).strip() if raw.get("owner") else None
    node.priority = str(raw["priority"]).strip() if raw.get("priority") else None
    node.due = _as_date(raw.get("due"), node_id, problems)
    node.done_on = _as_date(raw.get("done_on"), node_id, problems)
    node.notes = str(raw["notes"]).strip() if raw.get("notes") else None
    evidence = raw.get("evidence") or []
    node.evidence = [str(e).strip() for e in (evidence if isinstance(evidence, list)
                                              else [evidence]) if str(e).strip()]

    for child_raw in raw.get("children") or []:
        child = _build_node(child_raw, node, problems)
        if child is not None:
            node.children.append(child)
    return node


def load_wbs(path: Path) -> tuple[Wbs, list[Problem]]:
    """WBS ファイルを読む。読めなければ WbsError、内容の誤りは問題の一覧で返す。"""
    try:
        if path.stat().st_size > MAX_WBS_BYTES:
            raise WbsError(f"WBS ファイルが大きすぎます / too large: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WbsError(f"WBS ファイルを読めません / cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise WbsError(f"YAML として読めません / not valid YAML: {exc}") from exc

    section = data.get("wbs") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise WbsError("トップレベルに `wbs:` のマッピングが必要です / a top-level "
                       "`wbs:` mapping is required")

    problems: list[Problem] = []
    wbs_id = str(section.get("id") or "").strip()
    if not wbs_id:
        problems.append(Problem("error", "missing_id", None, "wbs.id がありません"))

    velocity = section.get("velocity_per_day")
    if velocity is not None and (isinstance(velocity, bool)
                                 or not isinstance(velocity, (int, float)) or velocity < 0):
        problems.append(Problem("error", "bad_velocity", None,
                                f"velocity_per_day は 0 以上の数値: {velocity!r}"))
        velocity = None
    window = section.get("velocity_window_days", DEFAULT_VELOCITY_WINDOW_DAYS)
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        problems.append(Problem("error", "bad_window",
                                None, f"velocity_window_days は 1 以上の整数: {window!r}"))
        window = DEFAULT_VELOCITY_WINDOW_DAYS

    roots = [n for raw in section.get("nodes") or []
             if (n := _build_node(raw, None, problems)) is not None]
    wbs = Wbs(
        id=wbs_id or "wbs", name=str(section.get("name") or wbs_id or "WBS"),
        deadline=_as_date(section.get("deadline"), "wbs.deadline", problems),
        velocity_per_day=float(velocity) if velocity is not None else None,
        velocity_window_days=window, roots=roots,
    )
    problems.extend(validate_structure(wbs))
    return wbs, problems


# =============================================================================
# 検証 / validation
# =============================================================================

def validate_structure(wbs: Wbs) -> list[Problem]:
    problems: list[Problem] = []
    nodes = wbs.all_nodes()
    seen: dict[str, Node] = {}
    for node in nodes:
        if node.id in seen:
            problems.append(Problem("error", "duplicate_id", node.id,
                                    f"id が重複しています: {node.id}"))
        seen[node.id] = node

    for node in nodes:
        if not node.is_leaf:
            if node.depends_on:
                problems.append(Problem(
                    "warning", "dependency_on_parent", node.id,
                    "親ノードの depends_on は使われません（葉に書いてください）"))
            if node.effort is not None or node.status != "todo" or node.evidence:
                problems.append(Problem(
                    "warning", "parent_has_leaf_fields", node.id,
                    "親ノードの status / effort / evidence は無視されます"
                    "（子の集計で決まります）"))
            continue
        for dep in node.depends_on:
            if dep not in seen:
                problems.append(Problem(
                    "error", "unknown_dependency", node.id,
                    f"depends_on の {dep!r} が存在しません"))
            elif not seen[dep].is_leaf:
                problems.append(Problem(
                    "error", "dependency_on_parent_node", node.id,
                    f"depends_on の {dep!r} は葉ではありません（子を指定してください）"))
            elif dep == node.id:
                problems.append(Problem("error", "self_dependency", node.id,
                                        "自分自身には依存できません"))

    # 循環（Kahn 法）/ cycles
    leaf_ids = {n.id for n in nodes if n.is_leaf}
    graph = {n.id: [d for d in n.depends_on if d in leaf_ids and d != n.id]
             for n in nodes if n.is_leaf}
    indegree = {k: len(v) for k, v in graph.items()}
    dependents: dict[str, list[str]] = {k: [] for k in graph}
    for key, deps in graph.items():
        for dep in deps:
            dependents[dep].append(key)
    queue = [k for k, d in indegree.items() if d == 0]
    resolved = 0
    while queue:
        key = queue.pop()
        resolved += 1
        for nxt in dependents[key]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if resolved != len(graph):
        stuck = sorted(k for k, d in indegree.items() if d > 0)
        problems.append(Problem("error", "dependency_cycle", None,
                                f"循環依存があります: {', '.join(stuck)}"))
    return problems


def _safe_relative(spec_path: str) -> bool:
    if not spec_path or spec_path.startswith(("/", "\\")):
        return False
    parts = Path(spec_path.replace("\\", "/")).parts
    return ".." not in parts and not (len(spec_path) > 1 and spec_path[1] == ":")


def evidence_detail(spec: str, root: Path) -> tuple[bool, str, dict[str, Any]]:
    """証拠 1 件を確かめる。戻り値は (満たしたか, 理由のキー, 差し込み値)。

    キーは `aipmo/messages.py` の `ev_*`。満たしていれば キーは空。
    Returns (satisfied, message key, params); the key is empty when satisfied.
    """
    path_part, _, phrase = spec.partition("::")
    path_part = path_part.strip()
    if not _safe_relative(path_part):
        return False, "ev_outside_root", {"path": repr(path_part)}
    root = root.resolve()
    try:
        matches = (sorted(root.glob(path_part.replace("\\", "/"))) if "*" in path_part
                   else [root / path_part])
    except (OSError, ValueError):
        return False, "ev_bad_path", {"path": repr(path_part)}
    matches = [m for m in matches if m.exists()
               and m.resolve().is_relative_to(root)]
    if not matches:
        return False, "ev_not_found", {"path": path_part}
    needle = phrase.strip()
    if not needle:
        return True, "", {}
    for match in matches:
        if not match.is_file():
            continue
        try:
            if match.stat().st_size > MAX_EVIDENCE_BYTES:
                continue
            if needle in match.read_text(encoding="utf-8", errors="replace"):
                return True, "", {}
        except OSError:
            continue
    return False, "ev_phrase_missing", {"path": path_part, "phrase": needle}


def check_evidence(spec: str, root: Path) -> tuple[bool, str]:
    """証拠 1 件を確かめる。`path` か `path::語句`（語句がそのファイルに含まれる）。

    `*` を含む path は glob（1 件以上に一致すればよい）。ルートの外は見ない。
    Returns (satisfied, why-not). `path` or `path::phrase`; a path with `*` is
    a glob. Nothing outside the root is ever read.
    """
    ok, key, params = evidence_detail(spec, root)
    return ok, ("" if ok else translate("ja", key, **params))


def validate_evidence(wbs: Wbs, root: Path, as_of: date) -> list[Problem]:
    problems: list[Problem] = []
    leaf_by_id = {leaf.id: leaf for leaf in wbs.leaves()}
    for leaf in wbs.leaves():
        results: list[tuple[str, bool, str, dict[str, Any]]] = []
        for spec in leaf.evidence:
            path_part = spec.partition("::")[0].strip()
            if not _safe_relative(path_part):
                problems.append(Problem(
                    "error", "bad_evidence_path", leaf.id,
                    f"証拠のパスはリポジトリ内の相対パスだけ: {path_part!r}",
                    {"path": repr(path_part)}))
                results.append((spec, False, "bad path", {}))
                continue
            ok, key, params = evidence_detail(spec, root)
            results.append((spec, ok, key, params))

        if leaf.done:
            if not leaf.evidence:
                problems.append(Problem(
                    "warning", "done_without_evidence", leaf.id,
                    "完了ですが証拠（evidence）がありません。現実と合っているか"
                    "確かめられません"))
            for spec, ok, key, params in results:
                if not ok and key != "bad path":
                    problems.append(Problem(
                        "error", "evidence_missing", leaf.id,
                        f"完了と書かれていますが証拠がありません: {translate('ja', key, **params)}",
                        {"why_key": key, "why_params": params}))
            blockers = [d for d in leaf.depends_on
                        if d in leaf_by_id and not leaf_by_id[d].done]
            if blockers:
                problems.append(Problem(
                    "warning", "done_before_dependency", leaf.id,
                    f"完了ですが依存先が未完了です: {', '.join(blockers)}",
                    {"deps": ", ".join(blockers)}))
        else:
            if results and all(ok for _, ok, _, _ in results):
                problems.append(Problem(
                    "warning", "maybe_done", leaf.id,
                    "証拠がすべて揃っています。完了にできるか確認してください"))
            if leaf.effort is None:
                problems.append(Problem("warning", "unestimated", leaf.id,
                                        "見積り（effort）がありません"))
            if leaf.due is not None and leaf.due < as_of:
                problems.append(Problem(
                    "warning", "overdue", leaf.id,
                    f"期限 {leaf.due.isoformat()} を過ぎています", {"due": leaf.due.isoformat()}))
    if wbs.deadline is not None and wbs.deadline < as_of and any(
            not leaf.done for leaf in wbs.leaves()):
        problems.append(Problem("warning", "deadline_passed", None,
                                f"全体の期限 {wbs.deadline.isoformat()} を過ぎています",
                                {"due": wbs.deadline.isoformat()}))
    return problems


# =============================================================================
# 集計 / analysis
# =============================================================================

def _rollup(node: Node) -> dict[str, Any]:
    leaves = node.leaves()
    done = [leaf for leaf in leaves if leaf.done]
    estimated = [leaf for leaf in leaves if leaf.effort is not None]
    effort_total = sum(leaf.effort or 0.0 for leaf in estimated)
    effort_done = sum(leaf.effort or 0.0 for leaf in estimated if leaf.done)
    if effort_total > 0:
        percent = round(effort_done / effort_total * 100)
    else:
        percent = round(len(done) / len(leaves) * 100) if leaves else 0
    if leaves and len(done) == len(leaves):
        status = "done"
    elif any(leaf.status in ("in_progress", "done") for leaf in leaves):
        status = "in_progress"
    elif any(leaf.status == "blocked" for leaf in leaves):
        status = "blocked"
    else:
        status = "todo"
    return {"id": node.id, "name": node.name, "status": status,
            "leaves": len(leaves), "done": len(done), "percent": percent,
            "remaining_effort": effort_total - effort_done}


def velocity(wbs: Wbs, as_of: date) -> dict[str, Any]:
    """速度（工数/暦日）。実績（done_on の入った完了）から。無ければ申告値。

    直近 `velocity_window_days` 日に完了した作業の見積り合計 ÷ 日数。
    根拠が無いときは None — 0 や楽観的な値で予測を作らない。

    Effort completed in the recent window ÷ days, from `done_on`; else the
    declared value; else None. A forecast is never built on a made-up speed.
    """
    window = wbs.velocity_window_days
    start = as_of - timedelta(days=window)
    recent = [leaf for leaf in wbs.leaves()
              if leaf.done and leaf.done_on is not None and leaf.effort is not None
              and start < leaf.done_on <= as_of]
    done_effort = sum(leaf.effort or 0.0 for leaf in recent)
    if done_effort > 0:
        return {"per_day": round(done_effort / window, 3), "basis": "history",
                "window_days": window, "samples": len(recent)}
    if wbs.velocity_per_day:
        return {"per_day": wbs.velocity_per_day, "basis": "declared",
                "window_days": window, "samples": 0}
    return {"per_day": None, "basis": "none", "window_days": window, "samples": 0}


def _forecast_tasks(wbs: Wbs) -> list[dict[str, Any]]:
    return [{"key": leaf.id, "title": leaf.name, "done": leaf.done,
             "effort": leaf.effort, "depends_on": list(leaf.depends_on)}
            for leaf in wbs.leaves()]


def _priority_key(leaf: Node) -> int:
    return _PRIORITY_RANK.get((leaf.priority or "").lower(), 5)


def task_items(wbs: Wbs) -> list[dict[str, Any]]:
    """Task Engine が拾う形（`items`）/ the shape the Task Engine harvests."""
    items = []
    for leaf in wbs.leaves():
        items.append({
            "id": leaf.id,
            "name": f"{leaf.id} {leaf.name}",
            "status": _DISPLAY[leaf.status],
            "due": leaf.due.isoformat() if leaf.due else None,
            "priority": leaf.priority,
            "assignee": leaf.owner,
            "project": wbs.id,
            "labels": [leaf.path_names()[0]] if leaf.parent is not None else [],
            "blocked": leaf.status == "blocked",
            "done": leaf.done,
            "effort": leaf.effort,
        })
    return items


def analyse(wbs: Wbs, root: Path, as_of: date | None = None,
            problems: list[Problem] | None = None) -> dict[str, Any]:
    return _analyse(wbs, root, as_of, problems)[0]


def _analyse(wbs: Wbs, root: Path, as_of: date | None = None,
             problems: list[Problem] | None = None) -> tuple[dict[str, Any], list[Problem]]:
    as_of = as_of or date.today()
    all_problems = list(problems or [])
    all_problems.extend(validate_evidence(wbs, root, as_of))

    leaves = wbs.leaves()
    tasks = _forecast_tasks(wbs)
    speed = velocity(wbs, as_of)
    has_errors = any(p.level == "error" for p in all_problems)

    forecast: dict[str, Any] | None = None
    impact: dict[str, Any] = {"blocked": [], "cycles": [], "critical_path": [],
                              "critical_path_effort": 0.0,
                              "critical_path_has_unestimated": False,
                              "downstream_impact": {}}
    calculator = RiskForecastAdapter()
    # 構造に誤りがあるとき（循環・存在しない依存先）は、計算しても意味が無い。
    # 誤りを直すのが先 — 偽の予測を出さない。
    # With structural errors a forecast would be meaningless; fix those first.
    if not has_errors:
        impact = calculator.dependency_impact(tasks)
        if speed["per_day"] is not None:
            forecast = calculator.forecast(
                tasks, velocity_per_day=speed["per_day"],
                deadline=(wbs.deadline or as_of).isoformat(), as_of=as_of.isoformat())
            if wbs.deadline is None:
                forecast["deadline"] = None
                forecast["days_to_deadline"] = None
                forecast["drift_days"] = None

    by_id = {leaf.id: leaf for leaf in leaves}
    blocked_by_deps = set(impact["blocked"])
    ready = sorted(
        (leaf for leaf in leaves if not leaf.done and leaf.status != "blocked"
         and leaf.id not in blocked_by_deps),
        key=lambda leaf: (_priority_key(leaf),
                          -len(impact["downstream_impact"].get(leaf.id, [])),
                          leaf.effort if leaf.effort is not None else 999, leaf.id))

    done_count = sum(1 for leaf in leaves if leaf.done)
    estimated = [leaf for leaf in leaves if leaf.effort is not None]
    total_effort = sum(leaf.effort or 0.0 for leaf in estimated)
    done_effort = sum(leaf.effort or 0.0 for leaf in estimated if leaf.done)
    summary = {
        "leaves": len(leaves), "done": done_count,
        "in_progress": sum(1 for leaf in leaves if leaf.status == "in_progress"),
        "blocked": sum(1 for leaf in leaves if leaf.status == "blocked"),
        "percent_by_count": round(done_count / len(leaves) * 100) if leaves else 0,
        "percent_by_effort": round(done_effort / total_effort * 100) if total_effort else 0,
        "remaining_effort": total_effort - done_effort,
        "unestimated": [leaf.id for leaf in leaves if not leaf.done and leaf.effort is None],
    }
    result = {
        "wbs": {"id": wbs.id, "name": wbs.name,
                "deadline": wbs.deadline.isoformat() if wbs.deadline else None},
        "as_of": as_of.isoformat(),
        "summary": summary,
        "nodes": [_rollup(root_node) for root_node in wbs.roots],
        "velocity": speed,
        "forecast": forecast,
        "critical_path": [k for k in impact["critical_path"] if not by_id[k].done]
        if impact["critical_path"] else [],
        "critical_path_effort": impact["critical_path_effort"],
        "blocked_by_dependency": sorted(blocked_by_deps),
        "ready": [{"id": leaf.id, "name": leaf.name, "effort": leaf.effort,
                   "priority": leaf.priority} for leaf in ready],
        "problems": [p.as_dict() for p in all_problems],
        "error_count": sum(1 for p in all_problems if p.level == "error"),
        "warning_count": sum(1 for p in all_problems if p.level == "warning"),
        "tasks": tasks,
        "items": task_items(wbs),
    }
    result["summary_text"] = render_text(result)
    return result, all_problems


def _tree_node(node: Node, root: Path, flags: dict[str, list[str]], critical: set[str],
               blocked: set[str], lang: str | None = "ja") -> dict[str, Any]:
    """画面用の木の 1 ノード。葉は証拠 1 件ごとの確認結果を持つ。親は葉の集計。"""
    out: dict[str, Any] = {
        "id": node.id, "name": node.name, "owner": node.owner, "priority": node.priority,
        "due": node.due.isoformat() if node.due else None,
        "done_on": node.done_on.isoformat() if node.done_on else None,
        "depends_on": list(node.depends_on), "notes": node.notes,
        "flags": flags.get(node.id, []),
    }
    if node.is_leaf:
        evidence = []
        for spec in node.evidence:
            ok, key, params = evidence_detail(spec, root)
            evidence.append({"spec": spec, "ok": ok, "why": "" if ok else translate(lang, key, **params)})
        out.update({"leaf": True, "status": node.status, "effort": node.effort,
                    "evidence": evidence, "critical": node.id in critical,
                    "blocked_by_dependency": node.id in blocked})
    else:
        roll = _rollup(node)
        out.update({"leaf": False, "status": roll["status"], "percent": roll["percent"],
                    "leaves": roll["leaves"], "done": roll["done"],
                    "effort": sum(leaf.effort or 0.0 for leaf in node.leaves()),
                    "remaining_effort": roll["remaining_effort"],
                    "children": [_tree_node(c, root, flags, critical, blocked, lang)
                                 for c in node.children]})
    return out


def _localized(problem: Problem, lang: str | None) -> str:
    """注意の文章を `lang` で。差し込み値の無いもの（構造の誤りなど）は、書かれたまま。"""
    key = f"wp_{problem.code}"
    if problem.code == "evidence_missing" and problem.params:
        why = translate(lang, problem.params["why_key"], **problem.params["why_params"])
        return translate(lang, key, why=why)
    if problem.params or problem.code in ("done_without_evidence", "maybe_done", "unestimated"):
        return translate(lang, key, **problem.params)
    return problem.message


def view(wbs: Wbs, root: Path, as_of: date | None = None,
         problems: list[Problem] | None = None, lang: str | None = "ja") -> dict[str, Any]:
    """画面用: `analyse` の結果に、木（`tree`）と ID ごとの注意（`flags`）を足したもの。

    読むだけ。木の中身は WBS ファイルと証拠の確認結果だけで、ここで新しく判断しない。
    The analysis plus the tree for display; read-only, nothing is decided here.
    """
    result, found = _analyse(wbs, root, as_of, problems)
    flags: dict[str, list[str]] = {}
    for shown, source in zip(result["problems"], found, strict=True):   # 言語ごとの文章に差し替える
        shown["message"] = _localized(source, lang)
    for p in result["problems"]:
        if p["node"]:
            flags.setdefault(p["node"], []).append(p["code"])
    critical = set(result["critical_path"])
    blocked = set(result["blocked_by_dependency"])
    result["tree"] = [_tree_node(r, root, flags, critical, blocked, lang) for r in wbs.roots]
    return result


def render_text(a: dict[str, Any]) -> str:
    """人が読む状況報告（Slack・端末）。数字はすべて上の集計から。"""
    s, name = a["summary"], a["wbs"]["name"]
    lines = [f"*{name} — WBS 状況* ({a['as_of']})",
             f"進捗: {s['done']}/{s['leaves']} 件完了（工数ベース {s['percent_by_effort']}%）、"
             f"残り工数 {s['remaining_effort']:g}、進行中 {s['in_progress']}、"
             f"ブロック {s['blocked']}"]

    speed, forecast = a["velocity"], a["forecast"]
    if speed["per_day"] is None:
        lines.append("速度: 不明（完了日 done_on の実績も申告値も無いため、予測はしません）")
    else:
        basis = (f"直近 {speed['window_days']} 日の実績 {speed['samples']} 件"
                 if speed["basis"] == "history" else "申告値")
        lines.append(f"速度: {speed['per_day']:g} /日（{basis}）")
    if forecast:
        done_line = f"完了見込み: {forecast['projected_completion']}"
        if forecast["drift_days"] is not None:
            drift = forecast["drift_days"]
            done_line += (f"（期限 {forecast['deadline']} に対し "
                          f"{'+' if drift > 0 else ''}{drift:.0f} 日）")
        else:
            done_line += "（期限は未設定）"
        lines.append(done_line)
    if a["critical_path"]:
        lines.append(f"クリティカルパス: {' → '.join(a['critical_path'])}"
                     f"（残り工数 {a['critical_path_effort']:g}）")
    if a["ready"]:
        lines.append("次に着手できる: " + "、".join(
            f"{r['id']} {r['name']}" + (f"（{r['effort']:g}）" if r["effort"] is not None else "")
            for r in a["ready"][:5]))
    errors = [p for p in a["problems"] if p["level"] == "error"]
    warnings = [p for p in a["problems"] if p["level"] == "warning"]
    if errors:
        lines.append(f"要修正（error {len(errors)}）:")
        lines.extend(f"・{p['node'] or '-'}: {p['message']}" for p in errors[:8])
    if warnings:
        lines.append(f"要確認（warning {len(warnings)}）:")
        lines.extend(f"・{p['node'] or '-'}: {p['message']}" for p in warnings[:6])
    return "\n".join(lines)
