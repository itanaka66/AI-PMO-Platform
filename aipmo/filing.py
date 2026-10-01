"""承認したタスクを、課題管理ツールにも起票する。

PMO Core が自分で作ったタスク — 運用者が設定した定期タスクと、警告から起こして
人が承認した対応タスク — は、これまで台帳にしか無かった。チームが日々見ているのは
課題管理ツール（Jira・GitHub Projects・Plane・OpenProject・Azure DevOps）なので、
そこにも課題として作れるようにする。

**外の世界を変える操作なので、承認の上でだけ行う。** 対応タスクの提案を承認した
（＝仕事にしてよい）ことと、課題管理ツールに課題を作ってよいことは別の許可で、
後者は起票のときに改めて人が決める（`aipmo file`・Web の「起票」ボタン）。
運用者が `auto` に書いた由来（既定は無し）だけは、常駐の PMO Core が承認なしで起票する。

起票は冪等：タスクの id から決めた冪等キーを使うので、書き込みの途中で落ちて
やり直しても、課題は二重にならない（各アダプタが同じキーでの再作成をしない）。
起票したら台帳のタスクに課題への参照を結び付ける。以後はその課題の状態・担当・
完了が収集で台帳に反映され（同じ課題が別のタスクとして増えることはない）、
台帳の側で勝手に完了にはできない。

Files approved PMO-made tasks into the issue tracker as well. Only on approval:
approving a follow-up proposal ("this is work") is a different permission from
"create an issue in the tracker", which is decided again at filing time. Only origins
the operator lists under `auto` are filed by the resident PMO Core without asking.
Filing is idempotent (the key derives from the task id), and afterwards the ledger
task is tied to the issue, so collection updates it instead of adding a duplicate.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import AdapterRegistry
from .task_engine import Task
from .trackers import TRACKERS

# 起票してよい由来 / origins that can be filed
FILEABLE_ORIGINS = ("followup", "recurring")


class FilingError(RuntimeError):
    """起票できなかった。`kind` で原因の種類が分かる。

    kind:
      - `adapter` … そのトラッカーのアダプタが設定されていない／課題を作れない
      - `target`  … このタスクは起票の対象ではない（状態・由来）
      - `remote`  … トラッカーに送ったが、課題が作られなかった
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class FilingConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FilingConfig:
    tracker: str                                  # アダプタ名 / adapter name
    origins: tuple[str, ...] = FILEABLE_ORIGINS   # 起票の対象にする由来
    auto: tuple[str, ...] = ()                    # 承認なしで起票してよい由来(既定は無し)
    params: dict[str, Any] = field(default_factory=dict)   # create_issues に渡す(project など)
    labels: tuple[str, ...] = ()
    max_auto_per_cycle: int = 5                   # 常駐が一周で起票する上限


def load_filing(raw: Any) -> FilingConfig | None:
    """`pmo_core.filing` を読む。書かれていなければ None（機能なし）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FilingConfigError("filing はマッピングで書いてください / must be a mapping")
    tracker = str(raw.get("tracker") or "").strip()
    if tracker not in TRACKERS or tracker == "wbs_file":
        choices = ", ".join(sorted(k for k in TRACKERS if k != "wbs_file"))
        raise FilingConfigError(f"tracker は {choices} のどれかです（'{tracker}'）"
                                f" / tracker must be one of {choices}")

    def origins(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = raw.get(key)
        if value is None:
            return default
        items = tuple(str(v) for v in value) if isinstance(value, list) else (str(value),)
        bad = [v for v in items if v not in FILEABLE_ORIGINS]
        if bad:
            raise FilingConfigError(
                f"{key}: {', '.join(bad)} は使えません。{' / '.join(FILEABLE_ORIGINS)} のどれか "
                f"/ unknown origin")
        return items

    selected = origins("origins", FILEABLE_ORIGINS)
    auto = origins("auto", ())
    outside = [a for a in auto if a not in selected]
    if outside:
        raise FilingConfigError(
            f"auto の {', '.join(outside)} は origins に含まれていません "
            f"/ auto origins must also be in origins")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise FilingConfigError("params はマッピングで書いてください / params must be a mapping")
    forbidden = {"issues", "idempotency_key"} & set(params)
    if forbidden:
        raise FilingConfigError(
            f"params に {', '.join(sorted(forbidden))} は書けません（PMO Core が決めます）")
    return FilingConfig(
        tracker=tracker, origins=selected, auto=auto, params=dict(params),
        labels=tuple(str(label) for label in (raw.get("labels") or [])),
        max_auto_per_cycle=max(1, int(raw.get("max_auto_per_cycle", 5))))


def filing_state(task: Task) -> dict[str, Any]:
    """そのタスクの起票の記録（無ければ空）。"""
    value = task.payload.get("filing")
    return value if isinstance(value, dict) else {}


def is_filed(task: Task) -> bool:
    return filing_state(task).get("state") == "filed"


def eligible(task: Task, config: FilingConfig, *, declined: bool = False) -> bool:
    """起票の対象か。承認済みの（仕事になった）、まだ起票していない、開いているタスク。

    見送った(`declined`)ものは、人が改めて起票するとき(`declined=True`)だけ対象。
    """
    skipped = ("filed",) if declined else ("filed", "declined")
    return (task.origin in config.origins and not task.proposed and not task.done
            and filing_state(task).get("state") not in skipped)


def idempotency_key(task: Task) -> str:
    """タスク id から決める冪等キー。同じタスクは何度やっても同じ課題。"""
    return "pmo-" + hashlib.sha1(task.id.encode("utf-8")).hexdigest()[:16]


def description_of(task: Task) -> str:
    lines = [f"PMO AI が起こしたタスクです / created by the PMO AI ({task.id})"]
    if task.generated_from:
        lines.append(f"きっかけ / trigger: {task.generated_from}")
    if task.origin:
        lines.append(f"由来 / origin: {task.origin}")
    return "\n".join(lines)


def tracker_key(tracker: str, ref: Any) -> tuple[str, str]:
    """作られた課題の参照から、台帳での (キー, 識別子) を作る。"""
    spec = TRACKERS[tracker]
    text = str(ref)
    if spec.adapter == "jira":
        return text.upper(), text.upper()
    return f"{spec.prefix}:{text}", text


def make_filer(adapters: AdapterRegistry, members: list[Any],
               config: FilingConfig) -> Callable[[Task], dict[str, Any]]:
    """`PmoCore.file_task(..., file=...)` に渡す、起票関数を作る。

    戻り値は {tracker, key, external_id, account, unassigned, reused}。
    """

    def file(task: Task) -> dict[str, Any]:
        if task.origin not in FILEABLE_ORIGINS:
            raise FilingError(f"{task.id} は起票の対象ではありません（PMO Core が作った"
                              f"タスクだけです） / not a PMO-made task", "target")
        if task.proposed:
            raise FilingError(f"{task.id} は承認待ちです。先に承認してください "
                              f"/ approve the proposal first", "target")
        name = config.tracker
        if not adapters.has(name):
            raise FilingError(f"{name} アダプタが設定されていません "
                              f"/ the {name} adapter is not configured", "adapter")
        adapter = adapters.get(name)
        if "create_issues" not in adapter.actions():
            raise FilingError(f"{name} アダプタには create_issues がありません "
                              f"/ the {name} adapter cannot create issues", "adapter")

        issue: dict[str, Any] = {
            "summary": task.title, "title": task.title,
            "description": description_of(task),
            "labels": list(config.labels), "tags": list(config.labels),
        }
        if task.due_date:
            issue["due_date"] = task.due_date
        if name == "jira" and task.priority:
            issue["priority"] = task.priority

        # 担当者。名前の推測はしない：アカウントが分かるときだけ付け、分からなければ
        # 担当なしで起票する（別人に割り当てるより、割り当てない方がよい）。
        # The assignee: never guessed. Set only when the account is known (Jira's adapter
        # resolves names itself); otherwise the issue is filed unassigned.
        account = None
        unassigned = None
        who = (task.assignee or "").strip()
        member = next((m for m in members if m.name.lower() == who.lower()), None) if who else None
        if member is not None and member.is_agent:
            pass                                  # 役割AIはトラッカーに居ない
        elif who:
            account = member.account(name) if member is not None else None
            if not account and name == "jira":
                account = who
            if account:
                issue["assignee"] = account
            else:
                unassigned = who

        result = adapter.invoke("create_issues", {
            **config.params, "issues": [issue], "idempotency_key": idempotency_key(task)})
        created = result.get("created") if isinstance(result, dict) else None
        if not created:
            detail = ""
            if isinstance(result, dict) and result.get("failed"):
                detail = f": {str(result['failed'])[:200]}"
            raise FilingError(f"{name} に課題が作られませんでした{detail} "
                              f"/ {name} created no issue{detail}", "remote")
        key, external_id = tracker_key(name, created[0])
        return {"tracker": name, "key": key, "external_id": external_id,
                "account": account, "unassigned": unassigned,
                "reused": bool(isinstance(result, dict) and result.get("skipped"))}

    return file
