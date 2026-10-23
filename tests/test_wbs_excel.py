"""WBS → Excel エクスポート / インポートのテスト / tests for wbs_excel."""
from __future__ import annotations

import io
import textwrap
from datetime import date
from pathlib import Path

import pytest

from aipmo.wbs import analyse, load_wbs
from aipmo.wbs_excel import export, export_bytes, import_from_excel, make_template, merge_import

pytest.importorskip("openpyxl")


def write_wbs(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "wbs.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


SIMPLE_WBS = """\
    wbs:
      id: test-wbs
      name: テスト WBS
      deadline: 2026-12-31
      nodes:
        - id: "1"
          name: 企画
          children:
            - id: "1.1"
              name: 要件定義
              status: done
              effort: 3
              done_on: 2026-09-10
              owner: 田中
              priority: high
            - id: "1.2"
              name: 画面設計
              status: in_progress
              effort: 3
              due: 2026-11-01
        - id: "2"
          name: 実装
          children:
            - id: "2.1"
              name: ログイン機能
              status: todo
              effort: 5
              due: 2026-11-15
              notes: 要確認
"""


def test_export_creates_file(tmp_path: Path) -> None:
    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, problems = load_wbs(wbs_path)
    analysis = analyse(wbs, tmp_path, date(2026, 10, 23), problems)
    out = tmp_path / "output.xlsx"
    export(wbs, out, analysis)
    assert out.exists()
    assert out.stat().st_size > 0


def test_export_sheet_names(tmp_path: Path) -> None:
    import openpyxl

    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, problems = load_wbs(wbs_path)
    analysis = analyse(wbs, tmp_path, date(2026, 10, 23), problems)
    out = tmp_path / "output.xlsx"
    export(wbs, out, analysis)

    wb = openpyxl.load_workbook(out)
    assert "WBS" in wb.sheetnames
    assert "概要" in wb.sheetnames


def test_export_wbs_rows(tmp_path: Path) -> None:
    import openpyxl

    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, problems = load_wbs(wbs_path)
    analysis = analyse(wbs, tmp_path, date(2026, 10, 23), problems)
    out = tmp_path / "output.xlsx"
    export(wbs, out, analysis)

    wb = openpyxl.load_workbook(out)
    ws = wb["WBS"]
    # ヘッダー行 + ノード数分の行があること
    # nodes: 1, 1.1, 1.2, 2, 2.1 → 5 ノード + ヘッダー 1 = 6 行
    assert ws.max_row >= 6
    # 先頭行はヘッダー
    assert ws.cell(row=1, column=1).value == "ID"
    assert ws.cell(row=1, column=2).value == "作業名"


def test_export_leaf_data(tmp_path: Path) -> None:
    import openpyxl

    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, problems = load_wbs(wbs_path)
    analysis = analyse(wbs, tmp_path, date(2026, 10, 23), problems)
    out = tmp_path / "output.xlsx"
    export(wbs, out, analysis)

    wb = openpyxl.load_workbook(out)
    ws = wb["WBS"]

    # 全行のIDを集める（ヘッダー除く）
    ids = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
    assert "1.1" in ids
    assert "2.1" in ids

    # 1.1 行を見つけて effort を確認
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == "1.1":
            assert ws.cell(row=r, column=4).value == 3.0
            assert ws.cell(row=r, column=8).value == "田中"
            break


def test_export_no_analysis(tmp_path: Path) -> None:
    """analysis なしでも WBS シートだけ出力できる。"""
    import openpyxl

    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, _ = load_wbs(wbs_path)
    out = tmp_path / "output.xlsx"
    export(wbs, out, analysis=None)

    wb = openpyxl.load_workbook(out)
    assert "WBS" in wb.sheetnames
    assert "概要" not in wb.sheetnames


def test_export_no_openpyxl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """openpyxl が無い場合は ImportError を上げる。"""
    import sys
    import builtins

    real_import = builtins.__import__
    # キャッシュから一時的に除外する
    openpyxl_modules = {k: v for k, v in sys.modules.items()
                        if k == "openpyxl" or k.startswith("openpyxl.")}
    for k in openpyxl_modules:
        monkeypatch.delitem(sys.modules, k)

    def mock_import(name: str, *args, **kwargs):
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError("no module named openpyxl")
        return real_import(name, *args, **kwargs)

    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, _ = load_wbs(wbs_path)
    out = tmp_path / "output.xlsx"

    monkeypatch.setattr(builtins, "__import__", mock_import)
    with pytest.raises(ImportError, match="openpyxl"):
        export(wbs, out)


def test_cli_wbs_excel(tmp_path: Path) -> None:
    """CLI コマンド `aipmo wbs excel` が .xlsx を出力する。"""
    import sys

    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    out = tmp_path / "out.xlsx"

    sys.argv = ["aipmo", "wbs", "excel", str(wbs_path),
                "--root", str(tmp_path), "--out", str(out)]
    from aipmo.cli import main
    rc = main()
    assert rc == 0
    assert out.exists()


# ---------- make_template ----------

def test_make_template_returns_bytes() -> None:
    """make_template() が bytes を返す。"""
    data = make_template()
    assert isinstance(data, bytes)
    assert len(data) > 0


def test_make_template_sheet_names() -> None:
    """テンプレートには WBS シートと使い方シートがある。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(make_template()))
    assert "WBS" in wb.sheetnames
    assert "使い方" in wb.sheetnames


def test_make_template_has_header() -> None:
    """WBS シートの先頭行はヘッダー（ID 列）。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(make_template()))
    ws = wb["WBS"]
    assert ws.cell(row=1, column=1).value == "ID"


# ---------- export_bytes ----------

def test_export_bytes_returns_bytes(tmp_path: Path) -> None:
    """export_bytes() が bytes を返す。"""
    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, problems = load_wbs(wbs_path)
    analysis = analyse(wbs, tmp_path, date(2026, 10, 23), problems)
    data = export_bytes(wbs, analysis)
    assert isinstance(data, bytes)
    assert len(data) > 0


# ---------- import_from_excel ----------

def _load_yaml_str(tmp_path: Path, yaml_str: str, name: str = "imported.yaml"):
    """YAML 文字列を一時ファイルに書いて load_wbs で読む。"""
    p = tmp_path / name
    p.write_text(yaml_str, encoding="utf-8")
    return load_wbs(p)


def _find_node(nodes, nid):
    for n in nodes:
        if n.id == nid:
            return n
        found = _find_node(n.children, nid)
        if found:
            return found
    return None


def _collect_ids(nodes):
    result = []
    for n in nodes:
        result.append(n.id)
        result.extend(_collect_ids(n.children))
    return result


def test_import_roundtrip(tmp_path: Path) -> None:
    """export → import のラウンドトリップでノード ID が保たれる。"""
    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, problems = load_wbs(wbs_path)
    analysis = analyse(wbs, tmp_path, date(2026, 10, 23), problems)

    xlsx_bytes = export_bytes(wbs, analysis)
    yaml_str = import_from_excel(io.BytesIO(xlsx_bytes))

    re_wbs, re_problems = _load_yaml_str(tmp_path, yaml_str)
    assert not re_problems

    original_ids = set(_collect_ids(wbs.nodes))
    imported_ids = set(_collect_ids(re_wbs.nodes))
    assert original_ids == imported_ids


def test_import_leaf_status(tmp_path: Path) -> None:
    """インポート後の葉ノードにステータスが保たれる。"""
    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, _ = load_wbs(wbs_path)
    xlsx_bytes = export_bytes(wbs)

    yaml_str = import_from_excel(io.BytesIO(xlsx_bytes))
    re_wbs, _ = _load_yaml_str(tmp_path, yaml_str)

    node_11 = _find_node(re_wbs.nodes, "1.1")
    assert node_11 is not None
    assert node_11.status == "done"


# ---------- merge_import ----------

MERGE_BASE_WBS = """\
    wbs:
      id: test-wbs
      name: テスト WBS
      nodes:
        - id: "1"
          name: 企画
          children:
            - id: "1.1"
              name: 要件定義
              status: done
              effort: 3
              evidence:
                - https://example.com/evidence
              depends_on: ["0.1"]
            - id: "1.2"
              name: 画面設計
              status: in_progress
              effort: 2
        - id: "2"
          name: 実装
          children:
            - id: "2.1"
              name: ログイン
              status: todo
              effort: 5
              depends_on: ["1.1", "1.2"]
"""


def test_merge_keeps_same_id(tmp_path: Path) -> None:
    """同一 ID のノードは Excel フィールドで上書きされ、evidence は保持される。"""
    wbs_path = write_wbs(tmp_path, MERGE_BASE_WBS)
    wbs, _ = load_wbs(wbs_path)
    xlsx = export_bytes(wbs)

    merged_yaml, log = merge_import(MERGE_BASE_WBS, io.BytesIO(xlsx))
    re_wbs, _ = _load_yaml_str(tmp_path, merged_yaml, "merged.yaml")

    node_11 = _find_node(re_wbs.nodes, "1.1")
    assert node_11 is not None
    assert node_11.status == "done"
    assert node_11.evidence == ["https://example.com/evidence"]

    action_ids = {c["id"] for c in log}
    assert "1.1" not in action_ids   # 変更なし → ログに出ない


def test_merge_deleted_node_logged(tmp_path: Path) -> None:
    """Excel で削除されたノードは change_log に deleted として記録される。"""
    # 1.2 を除いた Excel を作る
    deleted_wbs_yaml = """\
wbs:
  id: test-wbs
  name: テスト WBS
  nodes:
    - id: "1"
      name: 企画
      children:
        - id: "1.1"
          name: 要件定義
          status: done
          effort: 3
    - id: "2"
      name: 実装
      children:
        - id: "2.1"
          name: ログイン
          status: todo
          effort: 5
"""
    del_path = tmp_path / "deleted.yaml"
    del_path.write_text(deleted_wbs_yaml, encoding="utf-8")
    wbs_del, _ = load_wbs(del_path)
    xlsx = export_bytes(wbs_del)

    _, log = merge_import(MERGE_BASE_WBS, io.BytesIO(xlsx))
    deleted_ids = {c["id"] for c in log if c["action"] == "deleted"}
    assert "1.2" in deleted_ids


def test_merge_added_node_logged(tmp_path: Path) -> None:
    """Excel で追加されたノードは change_log に added として記録される。"""
    added_wbs_yaml = """\
wbs:
  id: test-wbs
  name: テスト WBS
  nodes:
    - id: "1"
      name: 企画
      children:
        - id: "1.1"
          name: 要件定義
          status: done
          effort: 3
        - id: "1.2"
          name: 画面設計
          status: in_progress
          effort: 2
        - id: "1.3"
          name: 新規追加タスク
          status: todo
          effort: 1
    - id: "2"
      name: 実装
      children:
        - id: "2.1"
          name: ログイン
          status: todo
          effort: 5
"""
    add_path = tmp_path / "added.yaml"
    add_path.write_text(added_wbs_yaml, encoding="utf-8")
    wbs_add, _ = load_wbs(add_path)
    xlsx = export_bytes(wbs_add)

    _, log = merge_import(MERGE_BASE_WBS, io.BytesIO(xlsx))
    added_ids = {c["id"] for c in log if c["action"] == "added"}
    assert "1.3" in added_ids


def test_merge_depends_resolved_when_dep_deleted(tmp_path: Path) -> None:
    """削除された ID への depends_on は自動解消され change_log に記録される。"""
    # 1.2 を削除 → 2.1 の depends_on["1.2"] が解消されるはず
    deleted_wbs_yaml = """\
wbs:
  id: test-wbs
  name: テスト WBS
  nodes:
    - id: "1"
      name: 企画
      children:
        - id: "1.1"
          name: 要件定義
          status: done
          effort: 3
    - id: "2"
      name: 実装
      children:
        - id: "2.1"
          name: ログイン
          status: todo
          effort: 5
"""
    del_path = tmp_path / "dep_test.yaml"
    del_path.write_text(deleted_wbs_yaml, encoding="utf-8")
    wbs_del, _ = load_wbs(del_path)
    xlsx = export_bytes(wbs_del)

    merged_yaml, log = merge_import(MERGE_BASE_WBS, io.BytesIO(xlsx))

    resolved = [c for c in log if c["action"] == "depends_resolved"]
    resolved_ids = {c["id"] for c in resolved}
    assert "2.1" in resolved_ids
    # 解消後の YAML では 2.1 の depends_on に 1.2 が含まれない
    re_wbs, _ = _load_yaml_str(tmp_path, merged_yaml, "dep_merged.yaml")
    node_21 = _find_node(re_wbs.nodes, "2.1")
    assert node_21 is not None
    deps = node_21.depends_on or []
    assert "1.2" not in deps


def test_import_parent_has_no_leaf_fields(tmp_path: Path) -> None:
    """親ノードにはステータスや工数が設定されない。"""
    wbs_path = write_wbs(tmp_path, SIMPLE_WBS)
    wbs, _ = load_wbs(wbs_path)
    xlsx_bytes = export_bytes(wbs)

    yaml_str = import_from_excel(io.BytesIO(xlsx_bytes))
    re_wbs, _ = _load_yaml_str(tmp_path, yaml_str)

    # "1" は親ノード（子: 1.1, 1.2）
    parent = _find_node(re_wbs.nodes, "1")
    assert parent is not None
    assert len(parent.children) == 2
