"""デモ用のサンプルデータを台帳（DB）に入れる。

`demo/` の設定（`demo/config.yaml` = SQLite、`demo/config.postgres.yaml` = PostgreSQL）と、
サンプルデータ（`demo/data/tasks.yaml`・`demo/data/outcomes.yaml`・`demo/wbs-demo.yaml`）から、
**「しばらく動かしていた」状態**を作る。作り方は、本物と同じ道筋を通すこと：

1. 過去の完了実績を入れる（学習の材料）。
2. タスクを、**過去の時刻で**台帳に入れる（着手・ブロックされた日が実際にさかのぼる）。
3. 時計を進めながら PMO Core の周を回す（3 日前・2 日前・1 日前・いま）。警告が続き、
   対応タスクの提案・担当の提案・自律的な判断の提案・WBS のずれの提案・役割AIへの依頼が、
   本物の判定で作られる。
4. 役割AIの成果のうち 1 件を認め、1 件を差し戻す（残り 1 件はレビュー待ち）。

作り物の行を直接書き込むことはしない（タスクと完了実績の入口だけを使う）ので、画面や CLI に出る
内容は、そのまま本物の挙動。外部サービスにはつながない（アダプタは mock、LLM は echo）。

**安全のため、`tenant: demo` の設定でだけ動く。** 別のテナントの台帳には、読み込みも消去もしない。
台帳に既にタスクがあるときは読み込まず、`--reset` で（デモのテナントの行だけを）消してからやり直す。

Loads sample data into the ledger (the database), through the real code paths: completions for
learning, tasks ingested at past times, PMO Core cycles run along a simulated clock (so alerts persist
and proposals appear from the real rules), and a review of the role AI's results. Nothing is faked
into place and no external service is touched. Works only with `tenant: demo`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

DEMO_TENANT = "demo"
REVIEWER = "デモ担当"
REJECT_NOTE = "手順案に、元に戻す手順(ロールバック)の確認が足りません"
CYCLE_DAYS = (3, 2, 1)             # 何日前に周を回すか（警告が続いている状態を作る）
DEFAULT_AGE = 4


class DemoError(RuntimeError):
    pass


# =============================================================================
# サンプルデータ / sample data
# =============================================================================

@dataclass
class Samples:
    tasks: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]


def _read_yaml(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DemoError(f"サンプルデータを読めません / cannot read {path}: {exc}") from exc
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise DemoError(f"{path} には `{key}:` の一覧が要ります / needs a non-empty `{key}:` list")
    return items


def read_samples(data_dir: Path) -> Samples:
    tasks = _read_yaml(data_dir / "tasks.yaml", "tasks")
    outcomes = _read_yaml(data_dir / "outcomes.yaml", "outcomes")
    seen: set[str] = set()
    for i, task in enumerate(tasks, 1):
        where = f"tasks.yaml の {i} 件目"
        if not isinstance(task, dict) or not task.get("key") or not task.get("title"):
            raise DemoError(f"{where}: key と title が要ります")
        if str(task["key"]) in seen:
            raise DemoError(f"{where}: key {task['key']} が重複しています")
        seen.add(str(task["key"]))
        if not isinstance(task.get("due", 0), int):
            raise DemoError(f"{where}: due は整数（今日からの日数）")
    for i, item in enumerate(outcomes, 1):
        if not isinstance(item, dict) or not item.get("assignee"):
            raise DemoError(f"outcomes.yaml の {i} 件目: assignee が要ります")
    return Samples(tasks=tasks, outcomes=outcomes)


# =============================================================================
# 消す / reset
# =============================================================================

def _require_demo(config: dict[str, Any]) -> None:
    if str(config.get("tenant") or "") != DEMO_TENANT:
        raise DemoError(
            f"デモは `tenant: {DEMO_TENANT}` の設定でだけ使えます（この設定は "
            f"'{config.get('tenant') or ''}'）。別のテナントの台帳を、読み込みでも消去でも触らないためです "
            f"/ the demo only runs with `tenant: {DEMO_TENANT}`")


def reset(config: dict[str, Any], base: Path) -> dict[str, Any]:
    """デモのテナントの台帳と、台帳の隣に置くものを消す（別のテナントの行には触れない）。"""
    from . import cli
    from .side_store import ALL_NAMES, FileSide

    _require_demo(config)
    ledger = cli.open_ledger(config, base)
    kind = ledger.backend
    removed: list[str] = []
    if kind == "postgres":
        store = ledger._store                    # テナントの行だけを消す
        conn = store._live()                     # type: ignore[attr-defined]
        for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta", "ledger_side_docs",
                      "ledger_side_log"):
            conn.execute(f"DELETE FROM {table} WHERE tenant = %s", (DEMO_TENANT,))
            removed.append(table)
        ledger.close()
        return {"backend": kind, "removed": removed}
    path = ledger.path
    side = FileSide(path)
    ledger.close()
    for target in [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
                   *[side.path_of(name) for name in ALL_NAMES]]:
        if target.exists():
            try:
                target.unlink()
                removed.append(target.name)
            except OSError as exc:
                raise DemoError(f"{target} を消せません（使用中？）/ cannot remove: {exc}") from exc
    return {"backend": kind, "removed": removed}


# =============================================================================
# 読み込む / load
# =============================================================================

def _candidate(task: dict[str, Any], today: datetime, status: str | None = None) -> dict[str, Any]:
    status = status or str(task.get("status") or "To Do")
    key = str(task["key"]).upper()
    due = (today.date() + timedelta(days=int(task.get("due", 7)))).isoformat()
    return {
        "key": key, "tracker": "jira", "external_id": key, "title": str(task["title"]),
        "assignee": task.get("assignee"), "due_date": due, "priority": task.get("priority"),
        "status": status, "blocked": status.lower() == "blocked", "done": False,
        "labels": list(task.get("labels") or []), "project": str(task.get("project") or ""),
        "effort": task.get("effort")}


def _timeline(samples: Samples, now: datetime) -> list[tuple[datetime, int, str, Any]]:
    """時刻順の出来事。取り込み（0）は、同じ時刻の周（1）より先。"""
    events: list[tuple[datetime, int, str, Any]] = []
    for task in samples.tasks:
        age = int(task.get("age", DEFAULT_AGE))
        status = str(task.get("status") or "To Do")
        started = task.get("started_days")
        blocked = task.get("blocked_days")
        first_age = max(age, int(started or 0), int(blocked or 0))
        in_progress = status.lower() in ("in progress", "in_progress")
        if in_progress and started is not None and int(started) < first_age:
            # 最初は未着手で現れ、あとで着手された
            events.append((now - timedelta(days=first_age), 0, "ingest", _candidate(task, now, "To Do")))
            events.append((now - timedelta(days=int(started)), 0, "ingest", _candidate(task, now)))
        else:
            events.append((now - timedelta(days=first_age), 0, "ingest", _candidate(task, now)))
    for days in CYCLE_DAYS:
        events.append((now - timedelta(days=days), 1, "cycle", days))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def _outcome_rows(samples: Samples, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(samples.outcomes, 1):
        done_at = now - timedelta(days=int(item.get("done_days_ago", 10)))
        late = item.get("late_days")
        duration = item.get("duration_days")
        rows.append({
            "task": f"JIRA:DEMO-H{index}", "assignee": item["assignee"],
            "labels": list(item.get("labels") or []), "priority": item.get("priority"),
            "due_date": (done_at.date() - timedelta(days=int(late))).isoformat()
            if late is not None else None,
            "first_seen": (done_at - timedelta(days=int(duration or 3) + 1)).isoformat(),
            "done_at": done_at.isoformat(),
            "late_days": late, "effort": item.get("effort"), "duration_days": duration})
    return rows


def load(config: dict[str, Any], base: Path, *, do_reset: bool = False) -> dict[str, Any]:
    from . import cli

    _require_demo(config)
    samples = read_samples(base / "data")
    if do_reset:
        reset(config, base)

    engine = cli.build_engine(config, base_dir=base)
    core = cli.attach_task_engine(engine, config, base, default=True, launch=True)
    if core is None:
        raise DemoError("台帳が無効です / the ledger is disabled")
    ledger = core.task_engine
    try:
        ledger.sync()
        if ledger.tasks:
            raise DemoError(
                f"台帳にはすでに {len(ledger.tasks)} 件のタスクがあります。デモのテナントの行を消して"
                f"やり直すなら --reset を付けてください / the ledger already holds "
                f"{len(ledger.tasks)} tasks: use --reset to clear the demo tenant and load again")

        real_now = datetime.now(timezone.utc)
        clock = {"now": real_now - timedelta(days=60)}
        ledger.now = lambda: clock["now"]
        core.background = False                        # 役割AIの実行を、その場で終えさせる
        ledger.add_outcomes(_outcome_rows(samples, real_now))

        for when, _, kind, payload in _timeline(samples, real_now):
            clock["now"] = when
            if kind == "ingest":
                ledger.ingest("demo", f"demo-{payload['key']}", [payload],
                              project=payload.get("project") or None)
            else:
                core.cycle()
        clock["now"] = real_now
        core.cycle()                                   # いま

        reviews = _review_role_ai(core)
        briefing = core.cycle()                        # 差し戻しが警告になった状態にする
        return summarize(core, briefing, reviews)
    finally:
        core.wait()
        ledger.close()


def _review_role_ai(core: Any) -> dict[str, Any]:
    """役割AIの成果: 1 件を認め、1 件を差し戻す。残りはレビュー待ち。"""
    pending = sorted(core.reviews_pending(), key=lambda r: r["task"])
    done: dict[str, Any] = {"accepted": None, "rejected": None}
    if pending:
        core.review_dispatch(pending[0]["task"], "accepted", REVIEWER, "問題なし",
                             pending[0]["dispatch"])
        done["accepted"] = pending[0]["task"]
    if len(pending) > 1:
        core.review_dispatch(pending[1]["task"], "rejected", REVIEWER, REJECT_NOTE,
                             pending[1]["dispatch"])
        done["rejected"] = pending[1]["task"]
    return done


def summarize(core: Any, briefing: dict[str, Any], reviews: dict[str, Any] | None = None) -> dict[str, Any]:
    ledger = core.task_engine
    ledger.sync()
    judgments = ledger.judgments()
    proposals = [t for t in ledger.proposals() if t.origin != "judgment"]
    return {
        "backend": ledger.backend,
        "store": ledger.describe(),
        "tasks": len(ledger.tasks),
        "open_tasks": sum(1 for t in ledger.tasks.values() if not t.done and not t.proposed
                          and t.origin != "judgment"),
        "outcomes": len(ledger.outcomes),
        "alerts": len(briefing.get("alerts", [])),
        "level": briefing.get("overall_level"),
        "proposals": len(proposals),
        "wbs_proposals": sum(1 for t in proposals if t.id.startswith("PMO:wb:")),
        "followup_proposals": sum(1 for t in proposals if t.id.startswith("PMO:fu:")),
        "assignment_proposals": len(briefing.get("assignment_proposals", [])),
        "judgments": len(judgments),
        "judgments_pending": sum(1 for t in judgments if t.proposed),
        "reviews_pending": len(core.reviews_pending()),
        "filing_pending": len(core.filing_candidates()),
        "learned_samples": (briefing.get("learning") or {}).get("samples", 0),
        "reviews": reviews or {},
    }
