"""確定した担当を、そのタスクの課題管理ツールへ書き戻す。

担当の確定（`aipmo assign --apply`、Web 画面のボタン）のとき、台帳だけでなく
そのタスクが載っているトラッカー（Jira・GitHub Projects・Plane・OpenProject・
Azure DevOps）の担当者も更新できる。宛先は台帳のタスクが持つ `tracker` と
`external_id` で決まる。

**名前を推測して書かない。** トラッカーごとに担当者の指定方法が違う
（GitHub はログイン名、Azure DevOps は表示名かメール、Plane は UUID、
OpenProject は数値の ID）。Jira だけは表示名やメールをアダプタが引き当てる。
それ以外は、メンバーごとに `pmo_core.members[].accounts` へ書かれた
アカウントだけを使い、書かれていなければ**書き込まずに止める**。
別人に割り当てるくらいなら、割り当てない方がよい。

書いた結果も確かめる。トラッカーが指定を受け付けなかった（GitHub は存在しない
ログインを黙って捨てる）ときは `unresolved_assignee` が返り、成功にしない。

Writes a confirmed assignee back to the tracker that owns the task. The
destination is the task's `tracker` and `external_id`. **A name is never
guessed:** each tracker names people differently (GitHub a login, Azure DevOps
a display name or e-mail, Plane a UUID, OpenProject a numeric id). Only Jira's
adapter resolves display names itself; for the rest, only the account written
in `pmo_core.members[].accounts` is used, and without one nothing is written.
Assigning nobody beats assigning the wrong person. The result is verified too:
when the tracker did not take the assignee, it is reported, not counted a
success.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .adapters.base import AdapterRegistry
from .pmo_core import Member, _member_of
from .task_engine import Task
from .trackers import TRACKERS


class WritebackError(RuntimeError):
    """書き戻せなかった。`kind` で原因の種類が分かる。

    kind:
      - `adapter` … そのトラッカーのアダプタが設定されていない／更新できない
      - `target`  … このタスクの宛先（トラッカー・識別子）が分からない
      - `account` … その担当者のアカウントが設定されていない
      - `remote`  … トラッカーに書いたが、受け付けられなかった
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def tracker_of(task: Task) -> str:
    """そのタスクのトラッカー名。以前の Jira の行（tracker が空）も扱う。"""
    if task.tracker:
        return task.tracker
    return "jira" if task.id.startswith("JIRA:") else ""


def writable_trackers(adapters: AdapterRegistry) -> set[str]:
    """担当を書けるトラッカー（アダプタが登録されていて、update_issue がある）。"""
    return {
        name for name in TRACKERS
        if adapters.has(name) and "update_issue" in adapters.get(name).actions()
    }


def make_writer(adapters: AdapterRegistry,
                members: list[Member]) -> Callable[[Task, str], dict[str, Any]]:
    """`accept_assignment(write=...)` に渡す、書き戻し関数を作る。"""

    def write(task: Task, assignee: str) -> dict[str, Any]:
        name = tracker_of(task)
        spec = TRACKERS.get(name)
        if spec is None:
            raise WritebackError(
                f"このタスクがどのトラッカーのものか分かりません（{task.id}）。"
                f"書き戻しはせず、台帳だけ更新してください "
                f"/ cannot tell which tracker owns {task.id}", "target")
        ref = task.external_id or (task.key or "" if name == "jira" else "")
        if not ref:
            raise WritebackError(
                f"{task.id} には {name} での識別子がありません "
                f"/ no {name} identifier for {task.id}", "target")
        if not adapters.has(name):
            raise WritebackError(
                f"{name} アダプタが設定されていません "
                f"/ the {name} adapter is not configured", "adapter")
        adapter = adapters.get(name)
        if "update_issue" not in adapter.actions():
            raise WritebackError(
                f"{name} アダプタには update_issue がありません "
                f"/ the {name} adapter cannot update issues", "adapter")

        member = _member_of(assignee, members)
        account = member.account(name) if member is not None else None
        if not account:
            if name != "jira":
                raise WritebackError(
                    f"メンバー '{assignee}' の {name} のアカウントが未設定のため、"
                    f"書き込みません（名前の推測で別人に割り当てないため）。"
                    f"pmo_core.members[].accounts.{name} に書いてください "
                    f"/ no {name} account for '{assignee}': set "
                    f"pmo_core.members[].accounts.{name}", "account")
            account = assignee      # Jira のアダプタは表示名・メールを自分で引き当てる

        try:
            ident: Any = spec.id_type(ref)
        except ValueError as exc:
            raise WritebackError(
                f"{task.id} の識別子 {ref!r} は {name} では使えません "
                f"/ {ref!r} is not a valid {name} identifier", "target") from exc

        result = adapter.invoke("update_issue", {spec.id_param: ident, "assignee": account})
        if isinstance(result, dict) and result.get("unresolved_assignee"):
            raise WritebackError(
                f"{name} が担当者 {account!r} を受け付けませんでした（存在しない・"
                f"割り当てできない）。{name} 側は変わっていません "
                f"/ {name} did not accept assignee {account!r}", "remote")
        return {"tracker": name, "account": account, "result": result}

    return write
