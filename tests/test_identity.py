"""名前から Plane / OpenProject のユーザー ID を引き当てる、のテスト / resolving user ids by name.

確かめること:
  (1) 照合は完全一致だけ（正規化は吸収するが、前方一致・部分一致・近さは使わない）
  (2) 強い手がかり（email）から順に、最初に一致が出た段で決め、複数なら曖昧で止める
  (3) 実アダプタの `list_assignees`（応答の形の違い・ページ・人以外の除外）
  (4) 書き戻し：引き当てた ID を書く／書かれたアカウントが常に優先／定まらなければ書かない
  (5) 起票：作成が担当を受け取らないトラッカーは、作った後に付けて確かめる
  (6) CLI `aipmo members` は読むだけ。設定で引き当てを切れる

What matters: exact match only; tiers strongest first, first match decides, several is ambiguous
and writes nothing; the real adapters' member lists; write-back uses the resolved id, a configured
account always wins, an unsettled name writes nothing; filing sets the assignee after create for
trackers whose create cannot take one; the CLI preview only reads.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aipmo import cli
from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.openproject import OpenProjectAdapter
from aipmo.adapters.plane import PlaneAdapter
from aipmo.engine.runner import Engine
from aipmo.filing import FilingConfig, make_filer
from aipmo.identity import AssigneeResolver, Person, normalize, person_of, resolve
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import Member, PmoCore, load_members
from aipmo.task_engine import TaskEngine, extract_candidates
from aipmo.writeback import WritebackError, make_writer

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


# ===== (1)(2) 照合 / matching ====================================================================

def people(*specs: dict[str, str]) -> list[Person]:
    return [Person(**s) for s in specs]


TANAKA = {"id": "u1", "name": "Taro Tanaka", "first_name": "Taro", "last_name": "Tanaka",
          "display_name": "taro.tanaka", "email": "taro@example.com"}
SATO = {"id": "u2", "name": "Hanako Sato", "first_name": "Hanako", "last_name": "Sato",
        "display_name": "hsato", "email": "hanako@example.com"}


def test_normalization_absorbs_case_width_and_spacing_only():
    assert normalize("Ｔａｒｏ　Tanaka") == normalize("taro tanaka") == "tarotanaka"
    assert normalize(None) == "" and normalize("  ") == ""
    assert normalize("田中 太郎") == normalize("田中　太郎") == normalize("田中太郎")


@pytest.mark.parametrize("wanted", ["Taro Tanaka", "taro tanaka", "ＴＡＲＯ　ＴＡＮＡＫＡ",
                                    "TaroTanaka", "Tanaka Taro", "taro.tanaka", "TARO.TANAKA"])
def test_an_exact_name_login_or_either_name_order_resolves(wanted):
    found = resolve(people(TANAKA, SATO), wanted)
    assert found.ok and found.id == "u1"


@pytest.mark.parametrize("wanted", ["Tan", "Taro", "Tanaka", "tanaka t", "Tarou Tanaka",
                                    "T. Tanaka", "taro.tanak", "taro", "Tanaka Taro Jr", ""])
def test_a_prefix_a_substring_or_a_near_miss_never_matches(wanted):
    found = resolve(people(TANAKA, SATO), wanted)
    assert found.status == "none" and found.id is None


def test_japanese_names_resolve_with_or_without_the_space():
    jp = people({"id": "7", "name": "田中 太郎", "first_name": "太郎", "last_name": "田中"})
    assert resolve(jp, "田中太郎").id == "7" and resolve(jp, "田中　太郎").id == "7"
    assert resolve(jp, "田中").status == "none"


def test_email_is_the_strongest_clue_and_settles_a_name_clash():
    twin = {**SATO, "id": "u3", "email": "other.sato@example.com"}
    clash = people(SATO, twin)
    assert resolve(clash, "Hanako Sato").status == "ambiguous"
    assert resolve(clash, "Hanako Sato", "other.sato@example.com").id == "u3"
    assert resolve(clash, "Hanako Sato", "HANAKO@example.com").id == "u2"


def test_the_first_tier_with_any_match_decides_and_never_falls_through_to_guess():
    twin = {**SATO, "id": "u3", "email": "other@example.com"}
    clash = people(SATO, twin)
    ambiguous = resolve(clash, "Hanako Sato")
    assert ambiguous.status == "ambiguous" and {p.id for p in ambiguous.candidates} == {"u2", "u3"}
    # email が一致する人がいないとき、名前の一致に落ちて 1 人を選んだりしない
    # (email が与えられて一致なしなら、次の段=名前で決める。ここでは 2 人なので曖昧のまま)
    assert resolve(clash, "Hanako Sato", "nobody@example.com").status == "ambiguous"
    # 強い段で 1 人に定まれば、弱い段の重なりは問わない
    mixed = people({"id": "a", "name": "Zed", "login": "kai"},
                   {"id": "b", "name": "Kai", "login": "other"})
    assert resolve(mixed, "kai").id == "a"                      # login 段が先に 1 人に定まる


def test_no_match_and_unusable_entries():
    assert resolve(people(TANAKA), "Nobody").status == "none"
    assert resolve([], "Taro Tanaka").status == "none"
    assert person_of({"name": "no id"}) is None and person_of({"id": ""}) is None
    assert person_of({"id": 12, "name": "A"}).id == "12"


# ===== (3) 実アダプタ / the real adapters ========================================================

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
            return response(method, url, payload) if callable(response) else response
        return 404, {}, b'{"message":"no route"}'


def ok(payload, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def plane_adapter(routes):
    transport = FakeTransport(routes)
    return PlaneAdapter(api_key="k", workspace_slug="acme", project_id="proj-1",
                        transport=transport, max_retries=1), transport


def op_adapter(routes):
    transport = FakeTransport(routes)
    return OpenProjectAdapter(base_url="https://op.example.com", api_key="k",
                              project_id="widgets", transport=transport, max_retries=1), transport


PLANE_MEMBERS = [
    {"member": {"id": "u-ann", "first_name": "Ann", "last_name": "Lee",
                "display_name": "ann.lee", "email": "ann@example.com"}, "role": 15},   # 入れ子の形
    {"id": "u-bob", "first_name": "Bob", "last_name": "Kim", "display_name": "bob", "email": ""},  # 平らな形
    {"member": "u-ghost"},                                                              # 氏名なし → 使えない
    {"role": 5},                                                                         # ID なし
]


def test_planes_member_list_reads_both_shapes_and_skips_what_it_cannot_use():
    adapter, transport = plane_adapter({"/members/": ok(PLANE_MEMBERS)})
    result = adapter.list_assignees()
    assert [i["id"] for i in result["items"]] == ["u-ann", "u-bob"]
    assert result["items"][0]["name"] == "Ann Lee" and result["items"][0]["email"] == "ann@example.com"
    assert transport.requests[0][0] == "GET"
    assert "/workspaces/acme/projects/proj-1/members/" in transport.requests[0][1]
    assert not adapter.writes("list_assignees")                     # 読み取り専用
    paged, _ = plane_adapter({"/members/": ok({"results": PLANE_MEMBERS[:2]})})
    assert paged.list_assignees()["count"] == 2


def op_page(users, total):
    return {"total": total, "_embedded": {"elements": users}}


def test_openprojects_assignee_list_follows_pages_and_leaves_out_groups():
    user = lambda i, name, login, email=None: {  # noqa: E731
        "_type": "User", "id": i, "name": name, "login": login, "email": email,
        "firstName": name.split()[0], "lastName": name.split()[-1]}
    pages = {1: [user(1, "Ann Lee", "ann", "ann@example.com"),
                 {"_type": "Group", "id": 50, "name": "Developers"}],
             2: [user(2, "Bob Kim", "bob"), {"_type": "PlaceholderUser", "id": 60, "name": "TBD"}]}

    def respond(method, url, payload):
        page = int(url.split("offset=")[1].split("&")[0])
        return ok(op_page(pages.get(page, []), 250))

    adapter, transport = op_adapter({"/available_assignees": respond})
    result = adapter.list_assignees()
    assert [i["id"] for i in result["items"]] == ["1", "2"]               # グループ・プレースホルダーを除く
    assert result["items"][0]["email"] == "ann@example.com"
    assert len(transport.requests) == 3                                     # total 250 → 3 ページ目まで
    assert not adapter.writes("list_assignees")


def test_an_empty_or_failing_list_is_handled():
    adapter, _ = op_adapter({"/available_assignees": ok(op_page([], 0))})
    assert adapter.list_assignees()["items"] == []
    broken, _ = plane_adapter({"/members/": (403, {}, b"{}")})
    with pytest.raises(Exception, match="403"):
        broken.list_assignees()


def test_the_resolver_caches_the_list_for_a_short_time():
    adapter, transport = plane_adapter({"/members/": ok(PLANE_MEMBERS)})
    clock = {"t": 0.0}
    reg = AdapterRegistry()
    reg.register(adapter)
    resolver = AssigneeResolver(reg, clock=lambda: clock["t"])
    assert resolver.can_resolve("plane") and not resolver.can_resolve("github_projects")
    assert resolver.resolve("plane", "Ann Lee").id == "u-ann"
    assert resolver.resolve("plane", "bob").id == "u-bob"
    assert len(transport.requests) == 1
    clock["t"] = 1000.0
    resolver.resolve("plane", "bob")
    assert len(transport.requests) == 2


# ===== (4) 書き戻し / write-back ===================================================================

PLANE_ISSUE = {"id": "i1", "name": "Plane の課題", "state": "x", "target_date": None}


def seeded(tmp_path: Path, tracker: str, output: Any, who: str = "Ann Lee") -> TaskEngine:
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    te.ingest("t", "r", extract_candidates({"items": output}, tracker))
    task = next(iter(te.tasks.values()))
    with te.transaction():
        te.tasks[task.id].suggested_assignee = who
    return te


def plane_world(patched: list, members=PLANE_MEMBERS, assignees_back=None):
    def issue(method, url, payload):
        patched.append((method, payload))
        got = assignees_back if assignees_back is not None else payload.get("assignees", [])
        return ok({"id": "i1", "assignees": got})

    return plane_adapter({"/members/": ok(members), "/issues/i1/": issue})


def accept(tmp_path, adapter, members, **kw):
    reg = AdapterRegistry()
    reg.register(adapter)
    te = seeded(tmp_path, "plane", [PLANE_ISSUE])
    core = PmoCore(task_engine=te, members=members)
    task = core.accept_assignment("PLANE:i1", write=make_writer(reg, members, **kw))
    return te, core, task


def test_a_member_without_an_account_is_resolved_by_name_and_the_id_is_what_is_written(tmp_path):
    patched: list = []
    adapter, _ = plane_world(patched)
    te, core, task = accept(tmp_path, adapter, [Member("Ann Lee")])
    assert patched == [("PATCH", {"assignees": ["u-ann"]})]               # 名前ではなく ID
    assert task.assignee == "Ann Lee"                                      # 台帳は人の名前のまま
    info = core.last_writeback
    assert info["account"] == "u-ann" and info["account_source"] == "lookup"
    assert info["resolved_as"] == "Ann Lee"


def test_a_configured_account_always_wins_and_no_lookup_is_made(tmp_path):
    patched: list = []
    adapter, transport = plane_world(patched)
    te, core, _ = accept(tmp_path, adapter, [Member("Ann Lee", accounts=(("plane", "u-bob"),))])
    assert patched == [("PATCH", {"assignees": ["u-bob"]})]
    assert not any("/members/" in url for _, url, _ in transport.requests)
    assert core.last_writeback["account_source"] == "config"


def test_an_ambiguous_or_unknown_name_writes_nothing_and_leaves_the_ledger(tmp_path):
    twins = PLANE_MEMBERS + [{"id": "u-ann2", "first_name": "Ann", "last_name": "Lee",
                              "display_name": "ann2", "email": "ann2@example.com"}]
    patched: list = []
    adapter, _ = plane_world(patched, members=twins)
    reg = AdapterRegistry()
    reg.register(adapter)
    te = seeded(tmp_path, "plane", [PLANE_ISSUE])
    core = PmoCore(task_engine=te, members=[Member("Ann Lee")])
    with pytest.raises(WritebackError, match="複数") as caught:
        core.accept_assignment("PLANE:i1", write=make_writer(reg, [Member("Ann Lee")]))
    assert caught.value.kind == "account" and "email" in str(caught.value)
    assert patched == [] and te.find("PLANE:i1").assignee is None

    # email を書けば絞れる
    core.accept_assignment("PLANE:i1", write=make_writer(
        reg, [Member("Ann Lee", email="ann2@example.com")]))
    assert patched == [("PATCH", {"assignees": ["u-ann2"]})]

    (tmp_path / "x").mkdir()
    te2 = seeded(tmp_path / "x", "plane", [PLANE_ISSUE], who="Stranger")
    with pytest.raises(WritebackError, match="一致する"):
        PmoCore(task_engine=te2).accept_assignment(
            "PLANE:i1", write=make_writer(reg, [Member("Stranger")]))
    assert te2.find("PLANE:i1").assignee is None
    assert len(patched) == 1                                            # 書いたのは email で絞った 1 回だけ


def test_a_failing_lookup_writes_nothing(tmp_path):
    adapter, transport = plane_adapter({"/members/": (500, {}, b"{}")})
    reg = AdapterRegistry()
    reg.register(adapter)
    te = seeded(tmp_path, "plane", [PLANE_ISSUE])
    with pytest.raises(WritebackError, match="取得できず") as caught:
        PmoCore(task_engine=te).accept_assignment(
            "PLANE:i1", write=make_writer(reg, [Member("Ann Lee")]))
    assert caught.value.kind == "account"
    assert all(m != "PATCH" for m, _, _ in transport.requests)


def test_the_lookup_can_be_turned_off_and_then_an_account_is_required_as_before(tmp_path):
    patched: list = []
    adapter, transport = plane_world(patched)
    reg = AdapterRegistry()
    reg.register(adapter)
    te = seeded(tmp_path, "plane", [PLANE_ISSUE])
    with pytest.raises(WritebackError, match="アカウントが未設定"):
        PmoCore(task_engine=te).accept_assignment(
            "PLANE:i1", write=make_writer(reg, [Member("Ann Lee")], lookup=False))
    assert transport.requests == [] and patched == []


def test_a_resolved_id_the_tracker_does_not_take_is_still_reported(tmp_path):
    patched: list = []
    adapter, _ = plane_world(patched, assignees_back=["someone-else"])
    reg = AdapterRegistry()
    reg.register(adapter)
    te = seeded(tmp_path, "plane", [PLANE_ISSUE])
    with pytest.raises(WritebackError) as caught:
        PmoCore(task_engine=te).accept_assignment(
            "PLANE:i1", write=make_writer(reg, [Member("Ann Lee")]))
    assert caught.value.kind == "remote" and te.find("PLANE:i1").assignee is None


def test_openproject_resolves_to_its_numeric_id_by_login_or_email(tmp_path):
    def wp(method, url, payload):
        if method == "GET":
            return ok({"id": 7, "lockVersion": 3})
        return ok({"id": 7, "_links": {"assignee": payload["_links"]["assignee"]}})

    users = [{"_type": "User", "id": 12, "name": "Ann Lee", "login": "ann", "email": "ann@example.com"},
             {"_type": "User", "id": 13, "name": "Bob Kim", "login": "bob", "email": None}]
    adapter, transport = op_adapter({"/available_assignees": ok(op_page(users, 2)),
                                     "work_packages/7": wp})
    reg = AdapterRegistry()
    reg.register(adapter)
    te = seeded(tmp_path, "openproject", [{"id": 7, "subject": "OP の課題", "status": "New"}])
    members = [Member("ann", email="ann@example.com")]
    PmoCore(task_engine=te, members=members).accept_assignment(
        "OP:7", write=make_writer(reg, members))
    patch_call = next(p for m, _, p in transport.requests if m == "PATCH")
    assert patch_call["_links"] == {"assignee": {"href": "/api/v3/users/12"}}


def test_member_email_is_read_from_the_config():
    (ann,) = load_members([{"name": "ann", "email": " ann@example.com "}])
    assert ann.email == "ann@example.com" and load_members(["bob"])[0].email == ""


# ===== (5) 起票 / filing =================================================================================

def test_filing_sets_the_assignee_after_create_for_a_tracker_whose_create_cannot_take_one(tmp_path):
    calls: list = []

    def create(method, url, payload):
        calls.append(("POST", payload))
        return ok({"id": "i9"}, 201)

    def update(method, url, payload):
        calls.append((method, payload))
        return ok({"id": "i9", "assignees": payload.get("assignees", [])})

    adapter, _ = plane_adapter({"/members/": ok(PLANE_MEMBERS), "/issues/i9/": update,
                                "/issues/": create})
    reg = AdapterRegistry()
    reg.register(adapter)
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW)
    member = Member("ann", email="ann@example.com")          # 名前は違うが email で当たる
    te.create_task("PMO:rec:x:1", "週次レビュー", origin="recurring", proposed=False,
                   assignee="ann")
    cfg = FilingConfig(tracker="plane")
    core = PmoCore(task_engine=te, members=[member], filing=cfg,
                   filer=make_filer(reg, [member], cfg))
    core.file_task("PMO:rec:x:1")
    assert [c[0] for c in calls] == ["POST", "PATCH"]
    assert "assignees" not in calls[0][1] and calls[1][1] == {"assignees": ["u-ann"]}
    filed = te.tasks["PMO:rec:x:1"].payload["filing"]
    assert filed["account"] == "u-ann" and filed["account_label"] == "Ann Lee"
    assert core.last_filing["unassigned"] is None

    # 追跡: トラッカーが担当を表示名で返しても、台帳のメンバー名を上書きしない
    te.ingest("collector:plane", "r", extract_candidates(
        {"items": [{"id": "i9", "name": "週次レビュー", "assignee": "Ann Lee"}]}, "plane"))
    assert te.tasks["PMO:rec:x:1"].assignee == "ann" and len(te.tasks) == 1


def test_filing_stays_filed_but_unassigned_when_the_name_is_unsettled_or_not_taken(tmp_path):
    def build(members_json, assignees_back):
        def update(method, url, payload):
            return ok({"id": "i9", "assignees": assignees_back})

        adapter, _ = plane_adapter({"/members/": ok(members_json), "/issues/i9/": update,
                                    "/issues/": ok({"id": "i9"}, 201)})
        reg = AdapterRegistry()
        reg.register(adapter)
        return reg

    for i, (members_json, back) in enumerate([
            ([], []),                                         # 該当なし
            (PLANE_MEMBERS, []),                              # 付かなかった
            (PLANE_MEMBERS + [{"id": "z", "first_name": "Ann", "last_name": "Lee"}], ["z"])]):  # 曖昧
        d = tmp_path / str(i)
        d.mkdir()
        te = TaskEngine(d / "task-ledger.db", now=lambda: NOW)
        te.create_task("PMO:rec:x:1", "週次レビュー", origin="recurring", proposed=False,
                       assignee="Ann Lee")
        cfg = FilingConfig(tracker="plane")
        core = PmoCore(task_engine=te, members=[Member("Ann Lee")], filing=cfg,
                       filer=make_filer(build(members_json, back), [Member("Ann Lee")], cfg))
        task = core.file_task("PMO:rec:x:1")
        assert task.payload["filing"]["state"] == "filed"            # 課題は作られた
        assert core.last_filing["unassigned"] == "Ann Lee"           # 担当だけ付かなかった


# ===== (6) CLI ==============================================================================================

def cli_world(tmp_path, monkeypatch, extra_config=""):
    patched: list = []
    adapter, transport = plane_world(patched)
    reg = AdapterRegistry()
    reg.register(adapter)
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(reg, llms))
    config = tmp_path / "config.yaml"
    config.write_text(
        "pmo_core:\n" + extra_config + "  members:\n    - name: Ann Lee\n    - name: Bob Kim\n"
        "      accounts: {plane: u-bob}\n    - name: Stranger\n", encoding="utf-8")
    return config, patched, transport


def test_cli_members_previews_resolution_and_only_reads(tmp_path, monkeypatch, capsys):
    config, patched, transport = cli_world(tmp_path, monkeypatch)
    assert cli.main(["--config", str(config), "members"]) == 1             # Stranger が当たらない
    out = capsys.readouterr().out
    assert "Ann Lee" in out and "u-ann" in out and "設定済み" in out and "該当なし" in out
    assert patched == [] and all(m == "GET" for m, _, _ in transport.requests)
    assert cli.main(["--config", str(config), "members", "--tracker", "openproject"]) == 0
    assert "設定されていません" in capsys.readouterr().out


def test_cli_assign_says_when_it_resolved_by_name_and_the_flag_turns_it_off(tmp_path, monkeypatch, capsys):
    config, patched, _ = cli_world(tmp_path, monkeypatch)
    te = seeded(tmp_path, "plane", [PLANE_ISSUE])
    te.close()
    assert cli.main(["--config", str(config), "assign", "PLANE:i1", "--apply", "--writeback"]) == 0
    out = capsys.readouterr().out
    assert "名前から" in out and "u-ann" in out and patched == [("PATCH", {"assignees": ["u-ann"]})]

    patched.clear()
    d = tmp_path / "off"
    d.mkdir()
    config2, patched2, _ = cli_world(d, monkeypatch, "  lookup_assignees: false\n")
    seeded(d, "plane", [PLANE_ISSUE]).close()
    assert cli.main(["--config", str(config2), "assign", "PLANE:i1", "--apply", "--writeback"]) == 1
    assert patched2 == []
