"""役割AIと Task Engine の接続に使う、役割ごとの既定と判定。

役割AI（`templates/roles/` の開発・テスト・調査・文書・営業）は、担当候補
（`pmo_core.members` の `kind: agent`）として台帳に並ぶ。タスクがその役割AIに
割り当てられると、PMO Core がそのタスクの情報を引数にして役割のテンプレートを
起動し、結果を台帳に記録する（[aipmo/pmo_core.py](aipmo/pmo_core.py)）。

ここが受け持つのは次の純粋な判定だけ。

  - タスクの項目（`{key}` `{title}` など）を、テンプレートの引数に写す
  - そのタスクが、その役割AIに**向いている**か（トラッカー、必須の項目）
  - 実行結果から、台帳に残す要約を取り出す

テンプレートの引数名は役割ごとに違う（開発AIは `issue_key`、調査AIは
`question`）。内蔵の 5 役割には既定の対応を持たせ、`params` で上書きできる。

The pure decisions behind connecting role AIs to the Task Engine: map a
task's fields (`{key}`, `{title}`…) onto a template's parameters, decide
whether the task *suits* the role (tracker, required fields), and pull a
short result out of a finished run. The five built-in roles have default
mappings; `params` overrides them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .trackers import tracker_of

# 台帳のタスクから引数に写せる項目 / task fields that can fill a parameter
PLACEHOLDERS = ("key", "external_id", "project", "title", "id", "tracker", "due_date")

# 実行の結果として台帳に残す文字数の上限。履歴を太らせないため。
# Characters of a result kept in the ledger, so history stays small.
EXCERPT_CHARS = 600

# 台帳に残す実行記録の件数 / dispatch records kept per task
KEEP_DISPATCHES = 5

SETTLED_BAD = ("failed", "skipped", "abandoned")

# 人が役割AIの成果を確かめた結果。accepted = 認めた、rejected = 差し戻した。
# What a human decided about a role AI's result.
REVIEW_DECISIONS = ("accepted", "rejected")


def review_of(entry: dict[str, Any] | None) -> dict[str, Any]:
    """実行記録のレビュー（無ければ空）/ the review on a dispatch record, or empty."""
    value = (entry or {}).get("review")
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class RolePreset:
    params: Mapping[str, str]
    trackers: tuple[str, ...] | None      # None = どのトラッカーのタスクでもよい


ROLE_PRESETS: dict[str, RolePreset] = {
    # 開発AI・テストAIは Jira の課題を読んでコメントする道具を持つので、Jira の
    # タスクにだけ向く。
    # The developer and tester roles read and comment on Jira issues, so they
    # suit Jira tasks only.
    "role_developer": RolePreset({"issue_key": "{key}", "jira_project": "{project}"},
                                 ("jira",)),
    "role_tester": RolePreset({"issue_key": "{key}", "jira_project": "{project}"},
                              ("jira",)),
    "role_researcher": RolePreset({"question": "{title}"}, None),
    "role_writer": RolePreset({"jira_project": "{project}"}, None),
    "role_sales": RolePreset({"jira_project": "{project}"}, None),
}


def field_value(task: Any, name: str) -> str:
    if name == "tracker":
        return tracker_of(task)
    value = getattr(task, name, None)
    return "" if value is None else str(value)


def render(template: str, task: Any) -> str:
    """`{key}` などを台帳のタスクの値に置き換える。

    `str.format` は使わない — 題名に波括弧が入っていても壊れないように、
    既知の名前だけを単純に置き換える。
    Plain replacement of the known names; not `str.format`, so braces inside a
    title cannot break it.
    """
    out = template
    for name in PLACEHOLDERS:
        out = out.replace("{" + name + "}", field_value(task, name))
    return out


def params_for(member: Any, task: Any) -> dict[str, str]:
    """この役割AIがこのタスクで使うテンプレート引数 / the template parameters."""
    preset = ROLE_PRESETS.get(member.template or "")
    mapping: dict[str, str] = dict(preset.params) if preset else {}
    mapping.update(dict(member.params))
    return {name: render(value, task) for name, value in mapping.items()}


def fit(member: Any, task: Any) -> tuple[bool, str]:
    """このタスクは、この役割AIに向いているか。向かなければ理由を返す。

    向かないタスクを無理に走らせない（Jira の課題を読む役割に GitHub の
    課題を渡しても、空振りか誤った調査になる）。理由は台帳に残り、警告になって
    人に回る。

    Whether the task suits the role, with the reason when it does not. A task
    is never forced through a role it does not fit — a Jira-reading role handed
    a GitHub issue would only misfire. The reason stays in the ledger and
    becomes an alert for a human.
    """
    preset = ROLE_PRESETS.get(member.template or "")
    trackers = tuple(member.trackers) or (preset.trackers if preset else None)
    tracker = tracker_of(task)
    if trackers is not None and tracker not in trackers:
        return False, (f"{member.template} は {', '.join(trackers)} のタスクだけが対象です"
                       f"（このタスクは {tracker or '宛先不明'}）")

    mapping: dict[str, str] = dict(preset.params) if preset else {}
    mapping.update(dict(member.params))
    for name, value in mapping.items():
        wanted = value.strip()
        if wanted in {"{" + p + "}" for p in PLACEHOLDERS} and not render(wanted, task):
            return False, f"引数 {name} に必要な {wanted} がこのタスクにはありません"
    return True, ""


def latest_dispatch(task: Any, agent: str | None = None) -> dict[str, Any] | None:
    """そのタスクの直近の実行記録（役割AIを指定すれば、その役割AIのもの）。"""
    for entry in reversed(task.dispatches or []):
        if agent is None or str(entry.get("agent", "")).lower() == agent.lower():
            return dict(entry)
    return None


def excerpt_of(result: Any) -> str:
    """実行結果から、台帳に残す要約（エージェント工程の回答）を取り出す。"""
    results = getattr(result, "results", None)
    outputs = [r.output for r in results.values()] if isinstance(results, dict) else [result]
    for output in outputs:
        if isinstance(output, dict) and isinstance(output.get("answer"), str):
            text = output["answer"].strip()
            if text:
                return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "…"
    return ""
