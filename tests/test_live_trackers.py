"""実サービス（Jira・Plane・OpenProject など）に対する確認。**手元で本物につなぐときだけ**動く。

既定では飛ばす（CI・通常の `pytest` では動かない）。使い方は docs/LIVE-TRACKERS.md。

  AIPMO_LIVE_CONFIG=/path/to/config.yaml        … 実サービスのアダプタを書いた設定ファイル（秘密は環境変数で）
  pytest tests/test_live_trackers.py             … 読み取りだけの診断（疎通・1 件の読み取り・担当候補の一覧）

担当の書き戻しまで試すとき（**実際の課題の担当を変えます**。専用のテスト課題を指定してください）:

  AIPMO_LIVE_WRITE=1
  AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<課題のid>;openproject=12"   … トラッカー=識別子 を ; で区切る
  AIPMO_LIVE_ASSIGNEE="佐藤"                                          … 担当にするメンバー名（設定のメンバー）

書き戻しは、担当を変えたあと読み直して確かめ、元の担当に戻す（元が未割り当てなら、そのまま残るので
専用の課題にすること）。

Opt-in checks against real trackers; see docs/LIVE-TRACKERS.md. Writing back mutates the designated test issues.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

CONFIG = os.environ.get("AIPMO_LIVE_CONFIG")
pytestmark = pytest.mark.skipif(not CONFIG, reason="AIPMO_LIVE_CONFIG が未設定（実サービスに対する確認）")

from aipmo import cli  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.probe import probe_all  # noqa: E402
from aipmo.task_engine import Task  # noqa: E402
from aipmo.trackers import TRACKERS  # noqa: E402
from aipmo.writeback import make_writer  # noqa: E402


@pytest.fixture(scope="module")
def world():
    path = Path(CONFIG)
    config = cli.load_config(path)
    engine = cli.build_engine(config, base_dir=path.resolve().parent)
    members = load_members((config.get("pmo_core") or {}).get("members"))
    return engine.adapters, members


def configured_trackers(adapters) -> list[str]:
    return [n for n in sorted(adapters.names()) if n in TRACKERS and n != "wbs_file"]


def test_every_configured_tracker_connects_reads_and_lists_people(world):
    adapters, _ = world
    names = configured_trackers(adapters)
    assert names, "設定に課題管理ツールのアダプタがありません"
    bad = []
    for report in probe_all(adapters):
        if report["name"] in names and not report["ok"]:
            bad.append((report["name"], [(s["id"], s["hint"], s["detail"]) for s in report["steps"] if not s["ok"]]))
    assert not bad, bad


def test_assignee_names_resolve_to_exactly_one_user_in_plane_and_openproject(world):
    """書き戻す前の確認: メンバーが、トラッカーのユーザーに一意に当たるか（`aipmo members` と同じ）。"""
    from aipmo.identity import AssigneeResolver

    adapters, members = world
    resolver = AssigneeResolver(adapters)
    humans = [m for m in members if not m.is_agent]
    checked = 0
    for tracker in ("plane", "openproject"):
        if not resolver.can_resolve(tracker):
            continue
        for member in humans:
            if member.account(tracker):                 # 書いたアカウントがあれば引き当てない
                continue
            outcome = resolver.resolve(tracker, member.name)
            assert outcome.status != "ambiguous", f"{tracker}: {member.name} が複数のユーザーに当たります"
            checked += 1
    assert checked >= 0


WRITE = os.environ.get("AIPMO_LIVE_WRITE") == "1"


@pytest.mark.skipif(not WRITE, reason="AIPMO_LIVE_WRITE=1（実際の課題の担当を変えます）")
def test_assignee_write_back_round_trips_on_the_designated_issues(world):
    adapters, members = world
    issues = dict(pair.split("=", 1) for pair in os.environ.get("AIPMO_LIVE_ISSUES", "").split(";") if "=" in pair)
    assignee = os.environ.get("AIPMO_LIVE_ASSIGNEE", "")
    assert issues and assignee, "AIPMO_LIVE_ISSUES と AIPMO_LIVE_ASSIGNEE が要ります"
    write = make_writer(adapters, members)
    for name, ref in issues.items():
        spec = TRACKERS[name]
        before = who_is_assigned(adapters.get(name), name, ref)
        task = Task(id=f"{spec.prefix}:{ref}", title="live check", tracker=name, external_id=ref,
                    key=ref if name == "jira" else None)
        outcome = write(task, assignee)
        assert outcome["tracker"] == name, outcome
        after = who_is_assigned(adapters.get(name), name, ref)
        assert after["label"], (name, after)                      # 書いたあと、読み直して担当が付いている
        if before["restore"] and before["restore"] != after["restore"]:
            adapters.get(name).invoke("update_issue", {spec.id_param: spec.id_type(ref), "assignee": before["restore"]})
        elif not before["restore"]:
            warnings.warn(f"{name} {ref}: 元の担当が分からない（未割り当て、または id が読めない）ため、"
                          f"{after['label']} のまま残しました")


def who_is_assigned(adapter, name: str, ref: str) -> dict:
    """読み直した担当。`label` は表示用、`restore` は元に戻すときに渡せる値（分かる場合だけ）。"""
    spec = TRACKERS[name]
    if name == "jira":                                            # Jira に get_issue は無い。キーで検索する
        items = adapter.invoke("search", {"jql": f"key = {ref}", "fields": ["assignee"], "limit": 1})["items"]
        item = items[0] if items else {}
        return {"label": item.get("assignee"), "restore": item.get("assignee_id")}
    issue = adapter.invoke("get_issue", {spec.id_param: spec.id_type(ref)})
    if name == "plane":
        ids = issue.get("assignees") or []
        return {"label": ", ".join(ids), "restore": ids[0] if ids else None}
    return {"label": issue.get("assignee"), "restore": None}     # OpenProject は名前だけ。元に戻せない
