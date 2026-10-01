"""WBS ファイルへの変更の反映（aipmo/wbs_edit.py）のテスト。

確かめること:
  (1) 変更の形の検証（決まった操作だけ。誤りは全部まとめて返す。改行などは入れられない）
  (2) 行単位の書き換え：コメント・並び・引用符・改行コードが残り、差分が最小になる
  (3) 結果を読み直して確かめる：新しい誤り、証拠の無い完了、頼んでいないノードの変化は、書かない
  (4) 冪等（同じ変更を何度反映しても同じ）。読んだ後に書き換えられていたら書かない

What matters: only the fixed operations; all problems reported at once; comments, order, quoting
and line endings survive and the diff is minimal; the result is re-read and must introduce no new
error, no done-without-evidence and no change to a node that was not named; idempotent; refuses to
write over a file that changed since it was read.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from aipmo.wbs import load_wbs
from aipmo.wbs_edit import (
    WbsEditError,
    plan_changes,
    validate_changes,
    write_plan,
)

TODAY = date(2026, 10, 2)

BASE = """\
# このファイルは人が PR で直す
wbs:
  id: demo
  name: デモ
  deadline: null
  nodes:
    - id: "1"
      name: "基盤"
      children:
        - id: "1.1"
          name: "作業 A"        # 行末コメント
          status: todo
          effort: 3
          priority: Low
          depends_on: []
          notes: "メモ"
        # 次の作業は進行中
        - id: "1.2"
          name: "作業 B"
          status: in_progress
          effort: 2
          evidence:
            - "a.py"
    - id: "2"
      name: "運用"
      children:
        - id: "2.1"
          name: "作業 C"
          status: todo
          depends_on: ["1.1"]
"""


def make(tmp_path: Path, text: str = BASE, eol: str = "\n") -> Path:
    (tmp_path / "a.py").write_text("def feature(): ...\n", encoding="utf-8")
    path = tmp_path / "wbs.yaml"
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))
    return path


def plan(path: Path, changes, **kw):
    return plan_changes(path, path.parent, changes, as_of=TODAY, **kw)


def set_(node, field, value):
    return {"op": "set", "node": node, "field": field, "value": value}


def changed_lines(p) -> list[str]:
    return [line for line in p.diff.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


# ===== (1) 形の検証 / shape ===========================================================================

def test_a_well_formed_list_is_normalised():
    out = validate_changes({"changes": [
        set_("1.1", "due", "2026-11-01"), set_("1.1", "effort", 2.5),
        {"op": "add_evidence", "node": "1.2", "values": ["a.py::def feature"]},
        {"op": "add", "parent": "1", "node": {"id": "1.3", "name": "新規", "effort": 1}}]})
    assert [c["op"] for c in out] == ["set", "set", "add_evidence", "add"]


@pytest.mark.parametrize("bad", [
    None, [], {"changes": []}, {"changes": "x"}, [1], [{"op": "delete", "node": "1.1"}],
    [set_("1.1", "id", "9")], [set_("1.1", "nope", 1)], [set_("1.1", "status", "finished")],
    [set_("1.1", "status", None)], [set_("1.1", "name", None)], [set_("1.1", "effort", -1)],
    [set_("1.1", "effort", True)], [set_("1.1", "due", "2026-13-40")], [set_("1.1", "due", 20261101)],
    [set_("1.1", "name", "二行\n目")], [set_("1.1", "notes", "x" * 501)], [set_("", "due", "2026-11-01")],
    [set_("1.1", "depends_on", ["ok", "bad id"])], [set_("1.1", "depends_on", "1.2")],
    [{"op": "add_evidence", "node": "1.2", "values": []}],
    [{"op": "add_evidence", "node": "1.2", "values": ["../secret.txt"]}],
    [{"op": "add_evidence", "node": "1.2", "values": ["/etc/passwd"]}],
    [{"op": "add", "parent": "1", "node": {"id": "x y", "name": "n"}}],
    [{"op": "add", "parent": "1", "node": {"id": "9", "name": "n", "status": "done"}}],
    [{"op": "add", "parent": "1", "node": {"id": "9", "name": "n", "evidence": ["a"]}}],
    [{"op": "add", "parent": "1", "node": {"id": "9"}}],
])
def test_malformed_changes_are_refused(bad):
    with pytest.raises(WbsEditError):
        validate_changes(bad)


def test_every_problem_is_reported_at_once_and_the_size_is_bounded():
    with pytest.raises(WbsEditError) as caught:
        validate_changes([set_("1.1", "status", "x"), set_("1.1", "effort", -1),
                          {"op": "zap"}])
    assert len(caught.value.problems) == 3
    with pytest.raises(WbsEditError, match="20"):
        validate_changes([set_("1.1", "due", "2026-11-01")] * 21)


# ===== (2) 行単位の書き換え / line-level edits ==========================================================

def test_set_changes_only_the_target_line_and_keeps_comments_and_quoting(tmp_path):
    path = make(tmp_path)
    p = plan(path, [set_("1.1", "priority", "High"), set_("1.1", "name", "作業 A 改"),
                    set_("1.1", "effort", 5)])
    assert sorted(changed_lines(p)) == sorted([
        '-          name: "作業 A"        # 行末コメント',
        '+          name: "作業 A 改"        # 行末コメント',          # 行末コメントは残る
        "-          effort: 3", "+          effort: 5",
        "-          priority: Low", "+          priority: High"])
    assert "# このファイルは人が PR で直す" in p.new_text and "# 次の作業は進行中" in p.new_text


def test_a_new_key_is_added_inside_the_node_and_before_its_children(tmp_path):
    path = make(tmp_path)
    p = plan(path, [set_("1.1", "due", "2026-11-01"), set_("1", "priority", "High")])
    assert "+          due: 2026-11-01" in changed_lines(p)
    assert "+      priority: High" in changed_lines(p)
    new, _ = load_wbs_text(tmp_path, p.new_text)
    assert str(new["1.1"].due) == "2026-11-01" and new["1"].priority == "High"
    assert p.new_text.index("priority: High") < p.new_text.index("      children:")   # 子の前に入る


def test_a_key_can_be_removed_and_depends_on_is_written_as_a_flow_list(tmp_path):
    path = make(tmp_path)
    p = plan(path, [set_("1.1", "notes", None), set_("2.1", "depends_on", ["1.1", "1.2"])])
    assert '-          notes: "メモ"' in changed_lines(p)
    assert '+          depends_on: ["1.1", "1.2"]' in changed_lines(p)
    assert plan(path, [set_("2.1", "notes", None)]).changed is False         # 無いものを消しても変わらない


def test_evidence_is_appended_created_and_deduplicated(tmp_path):
    path = make(tmp_path)
    (tmp_path / "b.py").write_text("x", encoding="utf-8")
    p = plan(path, [{"op": "add_evidence", "node": "1.2", "values": ["a.py", "b.py::x"]},
                    {"op": "add_evidence", "node": "2.1", "values": ["a.py"]}])
    assert '+            - "b.py::x"' in changed_lines(p)                    # 既存の a.py は足さない
    assert changed_lines(p).count('+            - "b.py::x"') == 1
    assert "+          evidence:" in changed_lines(p) and '+            - "a.py"' in changed_lines(p)


def test_a_node_is_added_under_a_parent_that_already_has_children(tmp_path):
    path = make(tmp_path)
    p = plan(path, [{"op": "add", "parent": "1", "node": {
        "id": "1.3", "name": "新しい作業", "effort": 2, "priority": "Low",
        "depends_on": ["1.1"], "notes": "分けた"}}])
    assert changed_lines(p) == [
        '+        - id: "1.3"', '+          name: "新しい作業"', "+          status: todo",
        "+          effort: 2", "+          priority: Low", '+          depends_on: ["1.1"]',
        '+          notes: "分けた"']
    new, _ = load_wbs_text(tmp_path, p.new_text)
    assert new["1.3"].parent.id == "1" and new["1.3"].effort == 2
    assert p.new_text.index('id: "1.3"') < p.new_text.index('id: "2"')         # 親の子の末尾に入る


def test_line_endings_are_preserved(tmp_path):
    path = make(tmp_path, eol="\r\n")
    p = plan(path, [set_("1.1", "effort", 9)])
    write_plan(p)
    raw = path.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert b"effort: 9" in raw


def load_wbs_text(tmp_path: Path, text: str):
    probe = tmp_path / "probe.yaml"
    probe.write_text(text, encoding="utf-8")
    wbs, problems = load_wbs(probe)
    return {n.id: n for n in wbs.all_nodes()}, problems


# ===== (3) 読み直して確かめる / verification =============================================================

def test_marking_done_needs_a_done_date_and_evidence_that_exists(tmp_path):
    path = make(tmp_path)
    with pytest.raises(WbsEditError, match="done_on"):
        plan(path, [set_("1.1", "status", "done")])
    with pytest.raises(WbsEditError) as caught:
        plan(path, [set_("1.1", "status", "done"), set_("1.1", "done_on", "2026-10-01"),
                    {"op": "add_evidence", "node": "1.1", "values": ["missing.py"]}])
    assert "evidence_missing" in str(caught.value)
    ok = plan(path, [set_("1.1", "status", "done"), set_("1.1", "done_on", "2026-10-01"),
                     {"op": "add_evidence", "node": "1.1", "values": ["a.py::def feature"]}])
    assert ok.changed and any("status" in line for line in ok.report)


def test_a_change_that_would_introduce_a_new_error_writes_nothing(tmp_path):
    path = make(tmp_path)
    before = path.read_bytes()
    with pytest.raises(WbsEditError, match="新しい問題"):
        plan(path, [set_("2.1", "depends_on", ["9.9"])])                        # 存在しない依存先
    with pytest.raises(WbsEditError, match="新しい問題"):
        plan(path, [set_("1.1", "depends_on", ["2.1"])])                        # 循環
    assert path.read_bytes() == before


def test_a_node_that_is_missing_or_ambiguous_or_in_an_unsupported_layout_is_refused(tmp_path):
    path = make(tmp_path)
    with pytest.raises(WbsEditError, match="見つかりません"):
        plan(path, [set_("9.9", "effort", 1)])
    dup = make(tmp_path, BASE.replace('id: "2.1"', 'id: "1.1"'))
    with pytest.raises(WbsEditError, match="複数"):
        plan(dup, [set_("1.1", "effort", 1)])
    block = make(tmp_path, BASE.replace('notes: "メモ"', "notes: |\n            複数行\n"))
    with pytest.raises(WbsEditError, match="書き換えられません"):
        plan(block, [set_("1.1", "notes", "x")])
    flow = make(tmp_path, BASE.replace("    - id: \"1.2\"", "    - id: \"1.2\"")
                .replace('          evidence:\n            - "a.py"\n', '          evidence: ["a.py"]\n'))
    with pytest.raises(WbsEditError, match="evidence"):
        plan(flow, [{"op": "add_evidence", "node": "1.2", "values": ["b.py"]}])


def test_add_refuses_a_leaf_parent_a_clashing_id_and_an_unknown_dependency(tmp_path):
    path = make(tmp_path)
    with pytest.raises(WbsEditError, match="子がありません"):
        plan(path, [{"op": "add", "parent": "1.1", "node": {"id": "1.1.1", "name": "x"}}])
    with pytest.raises(WbsEditError, match="すでに別の作業"):
        plan(path, [{"op": "add", "parent": "1", "node": {"id": "1.2", "name": "別の名前"}}])
    with pytest.raises(WbsEditError, match="新しい問題"):
        plan(path, [{"op": "add", "parent": "1", "node": {"id": "1.3", "name": "n",
                                                           "depends_on": ["7.7"]}}])


def test_a_proposal_for_another_wbs_is_not_applied_to_this_file(tmp_path):
    path = make(tmp_path)
    with pytest.raises(WbsEditError, match="別の WBS"):
        plan(path, [set_("1.1", "effort", 1)], expect_wbs_id="other")
    assert plan(path, [set_("1.1", "effort", 1)], expect_wbs_id="demo").changed


# ===== (4) 冪等・競合 / idempotent, no clobbering =========================================================

CHANGES = [set_("1.1", "effort", 8), set_("1.1", "due", "2026-11-01"),
           {"op": "add_evidence", "node": "1.2", "values": ["a.py::def feature"]},
           {"op": "add", "parent": "2", "node": {"id": "2.2", "name": "追加", "effort": 1}}]


def test_applying_the_same_changes_twice_gives_the_same_file(tmp_path):
    path = make(tmp_path)
    first = plan(path, CHANGES)
    write_plan(first)
    once = path.read_bytes()
    second = plan(path, CHANGES)
    assert second.changed is False and second.report == []
    write_plan(second)
    assert path.read_bytes() == once


def test_a_file_edited_after_planning_is_not_overwritten(tmp_path):
    path = make(tmp_path)
    p = plan(path, [set_("1.1", "effort", 8)])
    path.write_text(path.read_text(encoding="utf-8") + "# 人が追記\n", encoding="utf-8")
    with pytest.raises(WbsEditError, match="書き換えられました"):
        write_plan(p)
    assert "# 人が追記" in path.read_text(encoding="utf-8") and "effort: 8" not in path.read_text(
        encoding="utf-8")


def test_no_stray_temp_files_are_left_behind(tmp_path):
    path = make(tmp_path)
    write_plan(plan(path, [set_("1.1", "effort", 8)]))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.py", "wbs.yaml"]


def test_the_real_projects_wbs_accepts_a_typical_edit(tmp_path):
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / "aipmo.yaml"
    copy.write_text((root / "wbs" / "aipmo.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    p = plan_changes(copy, root, [
        set_("6.15", "priority", "High"), set_("6.15", "due", "2026-12-01"),
        {"op": "add", "parent": "6", "node": {"id": "6.99", "name": "試し", "effort": 1}}],
        as_of=TODAY)
    assert len(changed_lines(p)) == 7 and "6.99" in p.new_text


def test_a_bug_that_edits_an_unnamed_node_is_caught_before_anything_is_written(tmp_path, monkeypatch):
    from aipmo import wbs_edit

    path = make(tmp_path)
    real = wbs_edit._set_field

    def buggy(lines, node_id, field_name, value):
        said = real(lines, node_id, field_name, value)
        for i, line in enumerate(lines):                       # 無関係な作業 C の見積りも書き換える
            if line.strip() == "depends_on: [\"1.1\"]":
                lines[i] = line + "\n" + line.replace("depends_on", "effort").replace('["1.1"]', "99")
        return said

    monkeypatch.setattr(wbs_edit, "_set_field", buggy)
    before = path.read_bytes()
    with pytest.raises(WbsEditError, match="依頼していないノード"):
        plan(path, [set_("1.1", "effort", 8)])
    assert path.read_bytes() == before
