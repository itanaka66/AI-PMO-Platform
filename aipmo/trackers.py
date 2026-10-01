"""課題管理ツール（トラッカー）ごとの違いの対応表。

Jira・GitHub Projects・Plane・OpenProject・Azure DevOps は、課題の「番号」
「題名」「期限」を、それぞれ違う名前で返し、更新アクションの引数名も違う。
台帳が非 Jira の課題を拾い、担当を書き戻すために、その違いをここに集める。
他のモジュールを import しない（台帳と書き戻しの両方から使うため）。

A table of how each tracker differs. The five trackers name an issue's
identifier, title and due date differently, and take different parameter names
in their update action. The ledger uses this to pick up non-Jira issues and to
write assignees back. It imports nothing from the package, since both the
ledger and the write-back use it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tracker:
    adapter: str                          # アダプタの登録名 / registry name
    prefix: str                           # 台帳での id の接頭辞 / id prefix in the ledger
    id_param: str                         # update_issue に渡す識別子の引数名
    id_type: type                         # その型（int か str）
    ref_fields: tuple[str, ...]           # 出力のどの項目が識別子か
    title_fields: tuple[str, ...]
    due_fields: tuple[str, ...]


TRACKERS: dict[str, Tracker] = {
    "jira": Tracker("jira", "JIRA", "issue_key", str,
                    ("key",), ("summary", "title"), ("due_date", "duedate")),
    "github_projects": Tracker("github_projects", "GH", "issue_number", int,
                               ("number",), ("title",), ("due_date",)),
    "plane": Tracker("plane", "PLANE", "issue_id", str,
                     ("id",), ("name", "title"), ("target_date", "due_date")),
    "openproject": Tracker("openproject", "OP", "work_package_id", int,
                           ("id",), ("subject", "title"), ("due_date",)),
    "azure_devops": Tracker("azure_devops", "ADO", "work_item_id", int,
                            ("id",), ("title",), ("due_date",)),
}


def key_id(key: str) -> str:
    """表示用のキーから台帳の id を作る。

    Jira のキー（`PROJ-1`）は従来どおり `JIRA:PROJ-1`。ほかのトラッカーは
    キー自体が `GH:42` のように接頭辞つきなので、そのまま id にする。

    A Jira key (`PROJ-1`) keeps its traditional `JIRA:PROJ-1`. Every other
    tracker's key already carries its prefix (`GH:42`), so it is the id.
    """
    return key if ":" in key else f"JIRA:{key}"
