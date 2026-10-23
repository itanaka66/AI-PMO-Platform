"""WBS を Excel ファイルへ書き出す・読み込む / Export and import a WBS as an Excel workbook.

`openpyxl` がインストールされている場合にだけ使える。
Requirements openpyxl (the `excel` extra or `pip install openpyxl`).

使い方 / Usage::

    aipmo wbs excel wbs-demo.yaml --root demo --out wbs-demo.xlsx
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    from .wbs import Node, Wbs

# ステータスのセル背景色 / background fill per status
_STATUS_FILLS: dict[str, str] = {
    "done":        "C6EFCE",   # 緑
    "in_progress": "FFEB9C",   # 黄
    "blocked":     "FFC7CE",   # 赤
    "todo":        "FFFFFF",   # 白
}

_STATUS_LABEL: dict[str, str] = {
    "done":        "完了",
    "in_progress": "進行中",
    "blocked":     "ブロック",
    "todo":        "未着手",
}
_LABEL_TO_STATUS: dict[str, str] = {v: k for k, v in _STATUS_LABEL.items()}
_LABEL_TO_STATUS.update({k: k for k in _STATUS_LABEL})   # 英語キーもそのまま通す

_PRIORITY_LABEL: dict[str, str] = {
    "highest": "最高",
    "high":    "高",
    "medium":  "中",
    "low":     "低",
    "lowest":  "最低",
}
_LABEL_TO_PRIORITY: dict[str, str] = {v: k for k, v in _PRIORITY_LABEL.items()}
_LABEL_TO_PRIORITY.update({k: k for k in _PRIORITY_LABEL})

_HEADERS = [
    "ID", "作業名", "ステータス", "工数(SP)",
    "進捗(%)", "期日", "完了日", "担当者", "優先度", "備考",
]

_COL_WIDTHS = [12, 40, 12, 10, 8, 14, 14, 14, 10, 30]

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _require_openpyxl() -> Any:
    try:
        import openpyxl
        return openpyxl
    except ImportError as exc:
        raise ImportError(
            "openpyxl が必要です。`pip install openpyxl` か `pip install 'aipmo[excel]'` "
            "を実行してください。\n"
            "openpyxl is required: run `pip install openpyxl` or "
            "`pip install 'aipmo[excel]'`."
        ) from exc


def _rollup_percent(node: "Node") -> int:
    from .wbs import _rollup  # type: ignore[attr-defined]
    return _rollup(node)["percent"]


def _rollup_status(node: "Node") -> str:
    from .wbs import _rollup  # type: ignore[attr-defined]
    return _rollup(node)["status"]


def _write_node(ws: Any, node: "Node", depth: int, row: int) -> int:
    """1 ノードを書き、次の行番号を返す / write one node, return the next row index."""
    from openpyxl.styles import Alignment, Font, PatternFill

    indent = "  " * depth
    if node.is_leaf:
        status = node.status
        effort = node.effort
        percent = 100 if node.done else 0
    else:
        status = _rollup_status(node)
        effort = sum(leaf.effort or 0.0 for leaf in node.leaves()) or None
        percent = _rollup_percent(node)

    fill_hex = _STATUS_FILLS.get(status, "FFFFFF")
    fill = PatternFill(fill_type="solid", fgColor=fill_hex)
    bold = depth == 0
    font = Font(bold=bold)
    center = Alignment(horizontal="center", vertical="center")

    due_val = node.due.isoformat() if node.due else ""
    done_val = node.done_on.isoformat() if node.done_on else ""

    values = [
        node.id,
        indent + node.name,
        _STATUS_LABEL.get(status, status),
        effort if effort is not None else "",
        percent,
        due_val,
        done_val,
        node.owner or "",
        _PRIORITY_LABEL.get((node.priority or "").lower(), node.priority or ""),
        node.notes or "",
    ]
    for col, val in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.font = font
        if col == 3:   # ステータス列だけ塗る
            cell.fill = fill
        if col in (4, 5):
            cell.alignment = center

    if not node.is_leaf:
        ws.row_dimensions[row].outline_level = depth

    row += 1
    for child in node.children:
        row = _write_node(ws, child, depth + 1, row)
    return row


def _add_summary_sheet(wb: Any, wbs: "Wbs", analysis: dict[str, Any]) -> None:
    """概要シートを追加する / add a summary sheet."""
    from openpyxl.styles import Font

    ws = wb.create_sheet(title="概要")
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 20

    s = analysis["summary"]
    rows = [
        ("WBS 名", wbs.name),
        ("ID", wbs.id),
        ("集計日", analysis["as_of"]),
        ("全体期日", wbs.deadline.isoformat() if wbs.deadline else ""),
        ("", ""),
        ("タスク総数", s["leaves"]),
        ("完了", s["done"]),
        ("進行中", s["in_progress"]),
        ("ブロック", s["blocked"]),
        ("", ""),
        ("進捗（件数ベース）", f"{s['percent_by_count']}%"),
        ("進捗（工数ベース）", f"{s['percent_by_effort']}%"),
        ("残工数(SP)", s["remaining_effort"]),
    ]
    if analysis.get("forecast"):
        fc = analysis["forecast"]
        rows += [
            ("", ""),
            ("完了見込み", fc.get("projected_completion", "")),
            ("速度（/日）", analysis["velocity"].get("per_day", "")),
        ]
    critical = analysis.get("critical_path", [])
    if critical:
        rows += [("", ""), ("クリティカルパス", " → ".join(critical))]

    errors = [p for p in analysis["problems"] if p["level"] == "error"]
    warnings = [p for p in analysis["problems"] if p["level"] == "warning"]
    if errors:
        rows += [("", ""), (f"Error ({len(errors)})", "")]
        for p in errors:
            rows.append(("  " + (p["node"] or "-"), p["message"]))
    if warnings:
        rows += [("", ""), (f"Warning ({len(warnings)})", "")]
        for p in warnings:
            rows.append(("  " + (p["node"] or "-"), p["message"]))

    bold = Font(bold=True)
    for r, (label, value) in enumerate(rows, start=1):
        ws.cell(row=r, column=1, value=label).font = (
            bold if label and not label.startswith("  ") else Font())
        ws.cell(row=r, column=2, value=value)


def _build_workbook(wbs: "Wbs", analysis: dict[str, Any] | None = None) -> Any:
    """WBS から Workbook を作る共通処理 / build a Workbook from a Wbs."""
    openpyxl = _require_openpyxl()
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "WBS"

    header_fill = PatternFill(fill_type="solid", fgColor="4472C4")
    header_font = Font(bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="center", vertical="center")
    for col, (header, width) in enumerate(zip(_HEADERS, _COL_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 20

    next_row = 2
    for root_node in wbs.roots:
        next_row = _write_node(ws, root_node, 0, next_row)

    ws.sheet_properties.outlinePr.summaryBelow = False

    if analysis is not None:
        _add_summary_sheet(wb, wbs, analysis)

    return wb


def export(wbs: "Wbs", out: "Union[Path, io.IOBase]",
           analysis: dict[str, Any] | None = None) -> None:
    """WBS を Excel ファイルに書き出す / write the WBS to an Excel file or stream.

    `out` は `Path` またはファイルライクオブジェクト（`io.BytesIO` など）。
    `analysis` は `aipmo.wbs.analyse()` の戻り値。None のとき概要シートは作らない。
    """
    wb = _build_workbook(wbs, analysis)
    wb.save(out)


def export_bytes(wbs: "Wbs", analysis: dict[str, Any] | None = None) -> bytes:
    """WBS を Excel バイト列として返す / return the WBS workbook as bytes."""
    buf = io.BytesIO()
    export(wbs, buf, analysis)
    return buf.getvalue()


# =============================================================================
# テンプレート / template
# =============================================================================

_TEMPLATE_NOTES = [
    ("ID", "例: 1, 1.1, 1.1.1  ─  各階層をドットで区切る。文字列として入力してください。"),
    ("作業名", "親ノードは太字・子は 2 スペースインデント（階層ごとに増える）。インポート時はインデントの深さから階層を判定します。"),
    ("ステータス", "完了 / 進行中 / ブロック / 未着手 のいずれか。"),
    ("工数(SP)", "葉ノードの見積りポイント（数値）。親ノードは入力不要（集計値）。"),
    ("進捗(%)", "自動計算のため入力不要。インポート時は無視されます。"),
    ("期日", "YYYY-MM-DD 形式。"),
    ("完了日", "YYYY-MM-DD 形式。status が完了のときに入力。"),
    ("担当者", "担当者名。"),
    ("優先度", "最高 / 高 / 中 / 低 / 最低 のいずれか。"),
    ("備考", "自由テキスト。"),
]

_TEMPLATE_EXAMPLES = [
    # (id, depth, name, status, effort, due, done_on, owner, priority, notes)
    ("1",     0, "企画",           "todo", "",  "",            "",            "",   "",   ""),
    ("1.1",   1, "要件定義",        "done", "3", "2026-09-10",  "2026-09-10",  "田中", "high", ""),
    ("1.2",   1, "画面設計",        "todo", "3", "2026-11-01",  "",            "高橋", "medium", ""),
    ("2",     0, "実装",           "todo", "",  "",            "",            "",   "",   ""),
    ("2.1",   1, "ログイン機能",    "in_progress", "5", "2026-11-15", "", "鈴木", "high", ""),
    ("2.2",   1, "検索機能",       "todo", "5",  "2026-11-30",  "",            "鈴木", "medium", ""),
]


def make_template() -> bytes:
    """WBS インポート用 Excel テンプレートを返す / return an Excel import template as bytes."""
    openpyxl = _require_openpyxl()
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "WBS"

    # ヘッダー行
    header_fill = PatternFill(fill_type="solid", fgColor="4472C4")
    header_font = Font(bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="center", vertical="center")
    for col, (header, width) in enumerate(zip(_HEADERS, _COL_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 20

    # サンプル行
    example_font_parent = Font(bold=True)
    example_font_child = Font()
    for row_i, (nid, depth, name, status, effort, due, done_on, owner, priority, notes) in \
            enumerate(_TEMPLATE_EXAMPLES, start=2):
        indent = "  " * depth
        fill_hex = _STATUS_FILLS.get(_LABEL_TO_STATUS.get(status, "todo"), "FFFFFF")
        fill = PatternFill(fill_type="solid", fgColor=fill_hex)
        values = [
            nid,
            indent + name,
            _STATUS_LABEL.get(_LABEL_TO_STATUS.get(status, status), status),
            effort,
            "",   # 進捗(%) は空欄
            due,
            done_on,
            owner,
            _PRIORITY_LABEL.get(priority, priority),
            notes,
        ]
        font = example_font_parent if depth == 0 else example_font_child
        for col, val in enumerate(values, start=1):
            cell = ws.cell(row=row_i, column=col, value=val)
            cell.font = font
            if col == 3:
                cell.fill = fill

    # 使い方シート
    ws2 = wb.create_sheet(title="使い方")
    ws2.column_dimensions["A"].width = 16
    ws2.column_dimensions["B"].width = 70
    ws2.cell(row=1, column=1, value="列").font = Font(bold=True)
    ws2.cell(row=1, column=2, value="説明").font = Font(bold=True)
    for r, (col_name, desc) in enumerate(_TEMPLATE_NOTES, start=2):
        ws2.cell(row=r, column=1, value=col_name)
        c = ws2.cell(row=r, column=2, value=desc)
        c.alignment = Alignment(wrap_text=True)
        ws2.row_dimensions[r].height = 30
    ws2.cell(row=len(_TEMPLATE_NOTES) + 3, column=1,
             value="※ インポートは「WBS」シートのみ読み込みます。").font = Font(italic=True, color="666666")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =============================================================================
# インポート / import
# =============================================================================

def _cell_str(ws: Any, row: int, col: int) -> str:
    """セルの値を文字列として取り出す（None と日付に対応）。"""
    val = ws.cell(row=row, column=col).value
    if val is None:
        return ""
    if hasattr(val, "isoformat"):   # date / datetime
        return val.isoformat()[:10]
    return str(val).strip()


def _cell_float(ws: Any, row: int, col: int) -> float | None:
    val = ws.cell(row=row, column=col).value
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def import_from_excel(src: "Union[Path, io.IOBase]") -> str:
    """Excel ファイルを読み、WBS YAML 文字列として返す。

    「WBS」シートのデータを読む。「概要」シートの WBS名・ID・全体期日 も読む（省略可）。
    Reads the "WBS" sheet. Also reads wbs name/id/deadline from the "概要" sheet if present.
    """
    import yaml
    openpyxl = _require_openpyxl()

    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)

    # 概要シートから WBS メタデータを読む（あれば）
    wbs_name = "WBS"
    wbs_id = "imported"
    wbs_deadline = ""
    if "概要" in wb.sheetnames:
        ws_meta = wb["概要"]
        for r in ws_meta.iter_rows(min_row=1, max_row=20, values_only=True):
            label = str(r[0] or "").strip()
            value = str(r[1] or "").strip() if len(r) > 1 else ""
            if label == "WBS 名" and value:
                wbs_name = value
            elif label == "ID" and value:
                wbs_id = value
            elif label == "全体期日" and value:
                wbs_deadline = value

    # WBS シートからノードを読む
    if "WBS" not in wb.sheetnames:
        raise ValueError("「WBS」シートが見つかりません / sheet 'WBS' not found")
    ws = wb["WBS"]

    # フラットなノードリストを作る
    flat: list[dict[str, Any]] = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        nid = _cell_str(ws, row[0].row, 1)
        if not nid:
            continue
        raw_name = _cell_str(ws, row[0].row, 2)
        depth = (len(raw_name) - len(raw_name.lstrip(" "))) // 2
        name = raw_name.strip()
        status_label = _cell_str(ws, row[0].row, 3)
        status = _LABEL_TO_STATUS.get(status_label, "todo")
        effort = _cell_float(ws, row[0].row, 4)
        due = _cell_str(ws, row[0].row, 6)
        done_on = _cell_str(ws, row[0].row, 7)
        owner = _cell_str(ws, row[0].row, 8)
        priority_label = _cell_str(ws, row[0].row, 9)
        priority = _LABEL_TO_PRIORITY.get(priority_label) if priority_label else None
        notes = _cell_str(ws, row[0].row, 10)

        node: dict[str, Any] = {"id": nid, "name": name, "_depth": depth}
        if status != "todo":
            node["status"] = status
        if effort is not None:
            node["effort"] = effort
        if due:
            node["due"] = due
        if done_on:
            node["done_on"] = done_on
        if owner:
            node["owner"] = owner
        if priority:
            node["priority"] = priority
        if notes:
            node["notes"] = notes
        flat.append(node)

    wb.close()

    # 深さスタックでツリーを組み立てる / build tree from depth stack
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []   # (depth, node)
    for node in flat:
        depth = node.pop("_depth")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            parent.setdefault("children", [])
            parent["children"].append(node)
            # 親ノードは status/effort/done_on を持たない（集計で決まる）
            for leaf_field in ("status", "effort", "done_on"):
                parent.pop(leaf_field, None)
        else:
            roots.append(node)
        stack.append((depth, node))

    # YAML 構造を作る
    wbs_doc: dict[str, Any] = {"id": wbs_id, "name": wbs_name, "nodes": roots}
    if wbs_deadline:
        wbs_doc["deadline"] = wbs_deadline

    return yaml.dump({"wbs": wbs_doc}, allow_unicode=True,
                     default_flow_style=False, sort_keys=False)
