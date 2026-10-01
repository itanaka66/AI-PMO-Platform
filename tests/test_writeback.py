"""Jira 以外への担当の書き戻しのテスト / assignee write-back to other trackers.

実際のアダプタ（偽の通信路つき）を通す。出力の形は推測せず、各アダプタ自身の
`_flatten` を使う。確かめるのは、(1) 非 Jira の課題が台帳に宛先つきで入ること、
(2) 宛先のトラッカーへ正しい引数で書かれること、(3) 名前を推測して別人に書かない
こと、(4) 反映されなかったことを成功にしないこと。

Real adapters over fake transports; shapes come from each adapter's own
`_flatten`. What matters: non-Jira issues enter the ledger with a destination;
the right call reaches the right tracker; no name is guessed into the wrong
person; a write the tracker did not take is not a success.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aipmo import cli
from aipmo.adapters.azure_devops import AzureDevOpsAdapter
from aipmo.adapters.azure_devops import _flatten as ado_flatten
from aipmo.adapters.base import Adapter, AdapterRegistry, action
from aipmo.adapters.github_projects import GitHubProjectsAdapter
from aipmo.adapters.github_projects import _flatten as gh_flatten
from aipmo.adapters.openproject import OpenProjectAdapter
from aipmo.adapters.openproject import _flatten as op_flatten
from aipmo.adapters.plane import PlaneAdapter
from aipmo.dsl import loader
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import Member, PmoCore, load_members
from aipmo.task_engine import TaskEngine, extract_candidates
from aipmo.trackers import key_id
from aipmo.writeback import WritebackError, make_writer, tracker_of, writable_trackers

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        payload = json.loads(body.decode("utf-8")) if body else {}
        self.requests.append((method, url, payload))
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            response = self.routes[max(matches)[1]]
            return response() if callable(response) else response
        return 404, {}, b'{"message":"no route"}'


def ok(payload, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def github(routes):
    transport = FakeTransport(routes)
    return GitHubProjectsAdapter(token="t", owner="acme", repo="widgets",
                                 transport=transport, max_retries=1), transport


def plane(routes):
    transport = FakeTransport(routes)
    return PlaneAdapter(api_key="k", workspace_slug="acme", project_id="proj-1",
                        transport=transport, max_retries=1), transport


def openproject(routes):
    transport = FakeTransport(routes)
    return OpenProjectAdapter(base_url="https://op.example.com", api_key="k",
                              project_id="widgets", transport=transport,
                              max_retries=1), transport


def azure(routes):
    transport = FakeTransport(routes)
    return AzureDevOpsAdapter(organization="acme", project="Widgets", pat="t",
                              transport=transport, max_retries=1), transport


class FakeJira(Adapter):
    name = "jira"

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[dict[str, Any]] = []

    @action(writes=True)
    def update_issue(self, issue_key: str, assignee: str | None = None) -> dict[str, Any]:
        self.updates.append({"issue_key": issue_key, "assignee": assignee})
        return {"issue_key": issue_key}


# 各トラッカーが返す生の課題を、アダプタ自身の変換に通した「出力」
RAW = {
    "github_projects": [gh_flatten({
        "number": 42, "title": "GH の課題", "state": "open", "assignee": None,
        "labels": [{"name": "bug"}], "html_url": "https://github.com/acme/widgets/issues/42"})],
    "openproject": [op_flatten({
        "id": 7, "subject": "OP の課題", "dueDate": "2026-09-02", "lockVersion": 1,
        "_links": {"status": {"title": "New"}, "assignee": {}}})],
    "azure_devops": [ado_flatten({
        "id": 78, "fields": {"System.Title": "ADO の課題", "System.State": "Active",
                             "Microsoft.VSTS.Scheduling.DueDate": "2026-09-03"}},
        "Microsoft.VSTS.Scheduling.DueDate")],
}


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": [], "tracker": "", "external_id": ""}
    return {**base, **kw}


def seeded(tmp_path: Path, adapter: str, output: Any) -> tuple[TaskEngine, Any]:
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates({"items": output}, adapter))
    task = next(iter(te.tasks.values()))
    with te.transaction():
        te.tasks[task.id].suggested_assignee = "ann"
    return te, task


# ===== (1) 非 Jira の課題が宛先つきで台帳に入る / ingest with a destination ===========

def test_each_trackers_own_output_shape_is_read():
    gh = extract_candidates({"items": RAW["github_projects"]}, "github_projects")[0]
    assert (gh["key"], gh["tracker"], gh["external_id"]) == ("GH:42", "github_projects", "42")
    assert gh["title"] == "GH の課題"

    op = extract_candidates({"items": RAW["openproject"]}, "openproject")[0]
    assert (op["key"], op["tracker"], op["external_id"]) == ("OP:7", "openproject", "7")
    assert op["title"] == "OP の課題" and op["due_date"] == "2026-09-02"

    ado = extract_candidates({"items": RAW["azure_devops"]}, "azure_devops")[0]
    assert (ado["key"], ado["external_id"]) == ("ADO:78", "78")
    assert ado["due_date"] == "2026-09-03"

    pl = extract_candidates({"items": [{"id": "9f1c", "name": "Plane の課題",
                                        "target_date": "2026-09-01", "completed": True}]},
                            "plane")[0]
    assert (pl["key"], pl["external_id"], pl["due_date"]) == ("PLANE:9f1c", "9f1c", "2026-09-01")
    assert pl["done"] is True


def test_jira_output_keeps_its_traditional_identity():
    item = {"key": "proj-1", "summary": "Jira の課題", "due_date": "2026-09-01"}
    (jira,) = extract_candidates({"items": [item]}, "jira")
    assert (jira["key"], jira["tracker"], jira["external_id"]) == ("PROJ-1", "jira", "PROJ-1")
    assert key_id("PROJ-1") == "JIRA:PROJ-1" and key_id("GH:42") == "GH:42"
    (unknown,) = extract_candidates({"items": [item]})              # 出どころ不明
    assert unknown["tracker"] == "" and unknown["key"] == "PROJ-1"


def test_same_number_in_two_trackers_never_collides_and_repeats_merge(tmp_path):
    te = TaskEngine(tmp_path / "l.db", now=lambda: NOW)
    gh = extract_candidates({"items": RAW["github_projects"]}, "github_projects")
    te.ingest("t", "r1", gh)
    te.ingest("t", "r2", gh)                                         # 同じ課題は1件
    te.ingest("t", "r3", extract_candidates(
        {"items": [{"key": "ABC-42", "summary": "Jira の 42"}]}, "jira"))
    assert sorted(te.tasks) == ["GH:42", "JIRA:ABC-42"]
    task = te.tasks["GH:42"]
    assert (task.tracker, task.external_id) == ("github_projects", "42")
    assert te.find("gh:42").id == te.find("GH:42").id == task.id    # 大文字小文字を問わず


def test_the_engine_tells_the_ledger_which_adapter_produced_an_output(tmp_path):
    class Gh(Adapter):
        name = "github_projects"

        @action()
        def search(self, query: str = "") -> dict[str, Any]:
            return {"items": RAW["github_projects"], "count": 1}

    adapters = AdapterRegistry()
    adapters.register(Gh())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    engine = Engine(adapters, llms)
    te = TaskEngine(tmp_path / "l.db", now=lambda: NOW)
    te.attach(engine)
    engine.run(loader.load_dict({"name": "t", "steps": [
        {"id": "find", "adapter": "github_projects", "action": "search"}]}))
    assert te.tasks["GH:42"].tracker == "github_projects"


def test_rows_from_before_trackers_existed_still_count_as_jira(tmp_path):
    te = TaskEngine(tmp_path / "l.db", now=lambda: NOW)
    te.ingest("t", "r", [cand(key="A-1", title="old")])
    task = te.tasks["JIRA:A-1"]
    task.tracker = ""                                   # 以前の行
    assert tracker_of(task) == "jira"


# ===== (2) 正しい宛先へ正しい引数で書く / the right call to the right tracker ==========

def test_github_receives_the_login_and_the_ledger_follows(tmp_path):
    adapter, transport = github({"/issues/42": ok({"assignees": [{"login": "ann-gh"}]})})
    te, task = seeded(tmp_path, "github_projects", RAW["github_projects"])
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann", accounts=(("github_projects", "ann-gh"),))]

    core = PmoCore(task_engine=te, members=members)
    done = core.accept_assignment("GH:42", write=make_writer(registry, members))

    method, url, payload = transport.requests[0]
    assert method == "PATCH" and url.endswith("/repos/acme/widgets/issues/42")
    assert payload == {"assignees": ["ann-gh"]}
    assert done.assignee == "ann" and core.last_writeback["tracker"] == "github_projects"
    assert te.find("GH:42").assignee == "ann"


def test_azure_devops_receives_the_display_name(tmp_path):
    adapter, transport = azure({"workitems/78": ok({"id": 78, "fields": {
        "System.AssignedTo": {"displayName": "Ann Sato", "uniqueName": "ann@x.com"}}})})
    te, _ = seeded(tmp_path, "azure_devops", RAW["azure_devops"])
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann", accounts=(("azure_devops", "Ann Sato"),))]
    PmoCore(task_engine=te, members=members).accept_assignment(
        "ADO:78", write=make_writer(registry, members))
    patch = transport.requests[0][2]
    assert {"op": "add", "path": "/fields/System.AssignedTo", "value": "Ann Sato"} in patch


def test_plane_receives_the_user_uuid(tmp_path):
    adapter, transport = plane({"/issues/9f1c/": ok({"assignees": ["user-uuid-1"]})})
    te = TaskEngine(tmp_path / "l.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates(
        {"items": [{"id": "9f1c", "name": "Plane の課題"}]}, "plane"))
    with te.transaction():
        te.tasks["PLANE:9f1c"].suggested_assignee = "ann"
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann", accounts=(("plane", "user-uuid-1"),))]
    PmoCore(task_engine=te, members=members).accept_assignment(
        "PLANE:9f1c", write=make_writer(registry, members))
    assert transport.requests[0][2] == {"assignees": ["user-uuid-1"]}


def test_openproject_receives_a_user_link_with_the_lock_version(tmp_path):
    calls = {"n": 0}

    def wp7():
        calls["n"] += 1
        if calls["n"] == 1:
            return ok({"id": 7, "lockVersion": 3})                                 # GET
        return ok({"id": 7, "_links": {"assignee": {"href": "/api/v3/users/12"}}})  # PATCH

    adapter, transport = openproject({"work_packages/7": wp7})
    te, _ = seeded(tmp_path, "openproject", RAW["openproject"])
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann", accounts=(("openproject", "12"),))]
    PmoCore(task_engine=te, members=members).accept_assignment(
        "OP:7", write=make_writer(registry, members))
    patch = transport.requests[1][2]
    assert patch["lockVersion"] == 3
    assert patch["_links"] == {"assignee": {"href": "/api/v3/users/12"}}


def test_jira_still_resolves_a_display_name_without_an_account(tmp_path):
    jira = FakeJira()
    registry = AdapterRegistry()
    registry.register(jira)
    te = TaskEngine(tmp_path / "l.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates({"items": [{"key": "A-1", "summary": "x"}]}, "jira"))
    with te.transaction():
        te.tasks["JIRA:A-1"].suggested_assignee = "ann"
    PmoCore(task_engine=te, members=[Member("ann")]).accept_assignment(
        "A-1", write=make_writer(registry, [Member("ann")]))
    assert jira.updates == [{"issue_key": "A-1", "assignee": "ann"}]


# ===== (3) 名前を推測しない / no guessing =========================================

def test_a_member_without_an_account_is_never_written_by_name(tmp_path):
    adapter, transport = github({"/issues/42": ok({"assignees": [{"login": "ann"}]})})
    te, _ = seeded(tmp_path, "github_projects", RAW["github_projects"])
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann")]                             # GitHub のアカウント未設定
    with pytest.raises(WritebackError) as caught:
        PmoCore(task_engine=te, members=members).accept_assignment(
            "GH:42", write=make_writer(registry, members))
    assert caught.value.kind == "account" and "accounts.github_projects" in str(caught.value)
    assert transport.requests == []                       # トラッカーへは何も送っていない
    assert te.find("GH:42").assignee is None              # 台帳も変わらない


def test_unknown_members_are_not_written_either(tmp_path):
    adapter, transport = github({})
    te, _ = seeded(tmp_path, "github_projects", RAW["github_projects"])
    registry = AdapterRegistry()
    registry.register(adapter)
    with pytest.raises(WritebackError, match="未設定"):
        PmoCore(task_engine=te).accept_assignment("GH:42", write=make_writer(registry, []))
    assert transport.requests == []


# ===== (4) 反映されなかったことを成功にしない / a refused write is not a success =========

def test_github_silently_dropping_the_login_is_reported_and_the_ledger_stays(tmp_path):
    adapter, transport = github({"/issues/42": ok({"assignees": []})})   # 捨てられた
    te, _ = seeded(tmp_path, "github_projects", RAW["github_projects"])
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann", accounts=(("github_projects", "no-such-user"),))]
    with pytest.raises(WritebackError) as caught:
        PmoCore(task_engine=te, members=members).accept_assignment(
            "GH:42", write=make_writer(registry, members))
    assert caught.value.kind == "remote" and "no-such-user" in str(caught.value)
    assert te.find("GH:42").assignee is None
    assert te.find("GH:42").suggested_assignee == "ann"            # 提案は残る


def test_azure_devops_resolving_a_different_person_is_not_a_success(tmp_path):
    adapter, _ = azure({"workitems/78": ok({"id": 78, "fields": {
        "System.AssignedTo": {"displayName": "Someone Else", "uniqueName": "x@y.com"}}})})
    te, _ = seeded(tmp_path, "azure_devops", RAW["azure_devops"])
    registry = AdapterRegistry()
    registry.register(adapter)
    members = [Member("ann", accounts=(("azure_devops", "Ann Sato"),))]
    with pytest.raises(WritebackError, match="受け付けませんでした"):
        PmoCore(task_engine=te, members=members).accept_assignment(
            "ADO:78", write=make_writer(registry, members))
    assert te.find("ADO:78").assignee is None


def test_plane_and_openproject_report_an_assignee_that_did_not_take():
    p, _ = plane({"/issues/i1/": ok({"assignees": ["someone-else"]})})
    assert p.invoke("update_issue", {"issue_id": "i1", "assignee": "u1"}
                    ).get("unresolved_assignee") == "u1"
    p2, _ = plane({"/issues/i1/": ok({"assignees": ["u1"]})})
    assert "unresolved_assignee" not in p2.invoke(
        "update_issue", {"issue_id": "i1", "assignee": "u1"})

    n = {"n": 0}

    def wp():
        n["n"] += 1
        return ok({"lockVersion": 1}) if n["n"] == 1 else ok(
            {"_links": {"assignee": {"href": "/api/v3/users/99"}}})

    o, _ = openproject({"work_packages/5": wp})
    assert o.invoke("update_issue", {"work_package_id": 5, "assignee": "12"}
                    ).get("unresolved_assignee") == "12"


def test_the_adapters_leave_other_fields_alone_when_only_the_assignee_is_sent():
    a, transport = github({"/issues/1": ok({"assignees": [{"login": "x"}]})})
    result = a.invoke("update_issue", {"issue_number": 1, "assignee": "x"})
    assert transport.requests[0][2] == {"assignees": ["x"]} and result["changed"] == ["assignees"]
    o, ot = openproject({"work_packages/5": lambda: ok(
        {"lockVersion": 2, "_links": {"assignee": {"href": "/api/v3/users/12"}}})})
    result = o.invoke("update_issue", {"work_package_id": 5, "assignee": "12"})
    assert result["changed"] == ["assignee"]
    assert set(ot.requests[1][2]) == {"lockVersion", "_links"}


# ===== 宛先・設定の誤り / target and config errors ==============================================

def test_every_failure_to_find_a_destination_says_so_and_sends_nothing(tmp_path):
    te = TaskEngine(tmp_path / "l.db", now=lambda: NOW)
    te.ingest("t", "r", [cand(title="宛先の無いタスク")])
    keyless = next(iter(te.tasks.values()))
    registry = AdapterRegistry()
    writer = make_writer(registry, [])
    with pytest.raises(WritebackError) as caught:
        writer(keyless, "ann")
    assert caught.value.kind == "target"

    te.ingest("t", "r2", extract_candidates({"items": RAW["github_projects"]}, "github_projects"))
    gh = te.tasks["GH:42"]
    with pytest.raises(WritebackError) as caught:
        writer(gh, "ann")                                  # アダプタ未登録
    assert caught.value.kind == "adapter"

    class ReadOnly(Adapter):
        name = "github_projects"

        @action()
        def search(self) -> dict[str, Any]:
            return {}

    registry.register(ReadOnly())
    with pytest.raises(WritebackError, match="update_issue") as caught:
        writer(gh, "ann")
    assert caught.value.kind == "adapter"

    gh.external_id = "not-a-number"
    full, _ = github({})
    registry2 = AdapterRegistry()
    registry2.register(full)
    with pytest.raises(WritebackError) as caught:
        make_writer(registry2, [Member("ann", accounts=(("github_projects", "a"),))])(gh, "ann")
    assert caught.value.kind == "target"


def test_which_trackers_can_be_written_to():
    registry = AdapterRegistry()
    gh, _ = github({})
    registry.register(gh)
    registry.register(FakeJira())

    class ReadOnly(Adapter):
        name = "plane"

        @action()
        def search(self) -> dict[str, Any]:
            return {}

    registry.register(ReadOnly())
    assert writable_trackers(registry) == {"github_projects", "jira"}


def test_member_accounts_are_read_from_config():
    (ann,) = load_members([{"name": "ann", "accounts": {
        "github_projects": "ann-gh", "plane": "uuid-1", "openproject": ""}}])
    assert ann.account("github_projects") == "ann-gh" and ann.account("plane") == "uuid-1"
    assert ann.account("openproject") is None             # 空は未設定
    assert ann.account("azure_devops") is None


# ===== CLI ====================================================================================

def test_cli_assign_writes_back_to_the_tasks_own_tracker(tmp_path, monkeypatch, capsys):
    adapter, transport = github({"/issues/42": ok({"assignees": [{"login": "ann-gh"}]})})
    registry = AdapterRegistry()
    registry.register(adapter)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry, llms))

    config = tmp_path / "config.yaml"
    config.write_text(
        "pmo_core:\n  members:\n    - name: ann\n      accounts: {github_projects: ann-gh}\n",
        encoding="utf-8")
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates({"items": RAW["github_projects"]}, "github_projects"))
    te.close()

    assert cli.main(["--config", str(config), "assign", "GH:42", "--apply", "--writeback"]) == 0
    assert "github_projects" in capsys.readouterr().out
    assert transport.requests[0][2] == {"assignees": ["ann-gh"]}
    assert TaskEngine(tmp_path / "task-ledger.db").find("GH:42").assignee == "ann"


def test_cli_refuses_when_the_account_is_missing_and_changes_nothing(tmp_path, monkeypatch, capsys):
    adapter, transport = github({"/issues/42": ok({"assignees": [{"login": "ann"}]})})
    registry = AdapterRegistry()
    registry.register(adapter)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry, llms))
    config = tmp_path / "config.yaml"
    config.write_text("pmo_core:\n  members: [ann]\n", encoding="utf-8")
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates({"items": RAW["github_projects"]}, "github_projects"))
    te.close()

    assert cli.main(["--config", str(config), "assign", "GH:42", "--apply", "--jira"]) == 1
    assert "accounts.github_projects" in capsys.readouterr().err        # --jira は旧名で有効
    assert transport.requests == []
    assert TaskEngine(tmp_path / "task-ledger.db").find("GH:42").assignee is None


# ===== Web =====================================================================================

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "operator-token-1", "viewer-token-2"


def web_client(tmp_path: Path, adapters: AdapterRegistry, members: list[Member]):
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates({"items": RAW["github_projects"]}, "github_projects"))
    with te.transaction():
        te.tasks["GH:42"].suggested_assignee = "ann"
    te.close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(adapters, llms), tmp_path / "t", OPERATOR, viewer_token=VIEWER,
                     lang="en", store=RunStore(), pmo_ledger=tmp_path / "task-ledger.db",
                     members=members)
    return TestClient(app)


def op():
    return {"x-aipmo-token": OPERATOR}


def test_web_confirms_and_writes_to_github(tmp_path):
    adapter, transport = github({"/issues/42": ok({"assignees": [{"login": "ann-gh"}]})})
    registry = AdapterRegistry()
    registry.register(adapter)
    client = web_client(tmp_path, registry, [Member("ann", accounts=(
        ("github_projects", "ann-gh"),))])

    session = client.get("/api/session", headers=op()).json()
    assert session["writeback"] == ["github_projects"]
    task = client.get("/api/pmo", headers=op()).json()["tasks"][0]
    assert task["tracker"] == "github_projects" and task["external_id"] == "42"

    done = client.post("/api/pmo/assignments/accept",
                       json={"ref": "GH:42", "writeback": True}, headers=op())
    assert done.status_code == 200
    assert done.json()["written_to"] == "github_projects" and done.json()["jira_updated"] is False
    assert transport.requests[0][2] == {"assignees": ["ann-gh"]}


def test_web_maps_each_failure_to_a_status_and_changes_nothing(tmp_path):
    # 反映されなかった → 502
    refused, _ = github({"/issues/42": ok({"assignees": []})})
    registry = AdapterRegistry()
    registry.register(refused)
    client = web_client(tmp_path / "a", registry, [Member("ann", accounts=(
        ("github_projects", "ghost"),))])
    bad = client.post("/api/pmo/assignments/accept",
                      json={"ref": "GH:42", "writeback": True}, headers=op())
    assert bad.status_code == 502

    # アカウント未設定 → 422
    ok_adapter, transport = github({"/issues/42": ok({"assignees": [{"login": "ann"}]})})
    registry2 = AdapterRegistry()
    registry2.register(ok_adapter)
    client2 = web_client(tmp_path / "b", registry2, [Member("ann")])
    missing = client2.post("/api/pmo/assignments/accept",
                           json={"ref": "GH:42", "writeback": True}, headers=op())
    assert missing.status_code == 422 and "accounts.github_projects" in missing.json()["detail"]
    assert transport.requests == []

    # アダプタ未設定 → 503
    client3 = web_client(tmp_path / "c", AdapterRegistry(), [Member("ann")])
    assert client3.post("/api/pmo/assignments/accept",
                        json={"ref": "GH:42", "writeback": True}, headers=op()).status_code == 503

    # どの場合も、台帳は変わっていない
    for sub in ("a", "b", "c"):
        assert TaskEngine(tmp_path / sub / "task-ledger.db").find("GH:42").assignee is None


def test_web_viewer_cannot_confirm_and_ledger_only_still_works(tmp_path):
    registry = AdapterRegistry()
    client = web_client(tmp_path, registry, [Member("ann")])
    assert client.post("/api/pmo/assignments/accept", json={"ref": "GH:42"},
                       headers={"x-aipmo-token": VIEWER}).status_code == 403
    done = client.post("/api/pmo/assignments/accept", json={"ref": "GH:42"}, headers=op())
    assert done.status_code == 200 and done.json()["written_to"] is None


def test_the_screen_asks_for_write_back_only_where_it_can():
    js = (Path(__file__).resolve().parents[1] / "aipmo" / "web" / "static"
          / "app.js").read_text(encoding="utf-8")
    assert "canWriteBack" in js and "writableTrackers" in js and "session.writeback" in js
    assert "jiraWritable" not in js and "innerHTML" not in js
