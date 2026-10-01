"""WBS ファイルへの変更を、決まった形の「変更の一覧」から安全に反映する。

WBS の変更提案（`wbs_replan`）を人が承認したとき、その差分を `wbs/aipmo.yaml` のような
WBS ファイルへ反映するための部品。LLM が書いた自由な文章や JSON をそのままファイルに
流し込むことはしない — 次の**決まった形の変更だけ**を受け付け、反映の前に全部検証する。

```json
{"changes": [
  {"op": "set", "node": "3.5", "field": "due", "value": "2026-11-01"},
  {"op": "set", "node": "3.5", "field": "status", "value": "done"},
  {"op": "set", "node": "3.5", "field": "done_on", "value": "2026-10-02"},
  {"op": "add_evidence", "node": "3.5", "values": ["aipmo/foo.py::def bar"]},
  {"op": "add", "parent": "3", "node": {"id": "3.13", "name": "新しい作業", "effort": 2}}
]}
```

- `set` … 1 つの項目を書き換える（`name` `status` `effort` `priority` `due` `owner` `notes`
  `done_on` `depends_on`）。`null` で項目を消せる（`name` `status` は除く）。
- `add_evidence` … 証拠を足す（既にあるものは足さない）。
- `add` … **すでに子を持つ**親の下に、新しい作業を足す（status は todo / in_progress / blocked）。
- 消す・id を変える・動かす、は**できない**。

守ること / Guarantees:

- **書式を壊さない。** YAML を作り直さず、対象の行だけを書き換える（コメント・並び・引用符は
  そのまま）。変更はレビューしやすい最小の差分になる。
- **反映の前に確かめる。** 結果を WBS として読み直し、(1) 新しい誤り（error）が増えない、
  (2) 完了にしたものには `done_on` と証拠があり、証拠は実在する、(3) 依頼していないノードが
  変わっていない、を満たさなければ**何も書かない**。
- **同じ変更を何度反映しても同じ結果。** すでにその値なら変えない。
- **読んだ後に書き換えられていたら書かない**（内容の指紋を比べる）。

Applies a fixed shape of WBS changes to the WBS file once a human approved a replan proposal.
Free-form model output is never poured into the file: only the operations above are accepted and
all are validated first. Text is edited line by line (comments, order and quoting survive), the
result is re-read as a WBS and must introduce no new error, must have evidence for anything newly
done, and must touch no node that was not named. Idempotent; refuses to write if the file changed
since it was read.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .wbs import (STATUSES, MAX_WBS_BYTES, Wbs, WbsError, _safe_relative, load_wbs,
                  validate_evidence)

MAX_CHANGES = 20
SET_FIELDS = ("name", "status", "effort", "priority", "due", "owner", "notes", "done_on",
              "depends_on")
NOT_REMOVABLE = ("name", "status")
ADD_STATUSES = ("todo", "in_progress", "blocked")
NEW_NODE_KEYS = ("id", "name", "status", "effort", "priority", "due", "owner", "notes",
                 "depends_on")
_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,30}$")
# 反映してはいけない新しい警告（現実との食い違いを作るもの）。
# New warnings that would put the WBS out of step with reality.
BLOCKING_WARNINGS = ("done_without_evidence", "done_before_dependency")


class WbsEditError(ValueError):
    """変更を反映できない。`problems` に理由を全部入れる。"""

    def __init__(self, problems: list[str] | str) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__(" / ".join(self.problems))


@dataclass(frozen=True)
class Plan:
    """検証済みの反映計画。`write_plan` が書く。"""
    path: str
    old_text: str
    new_text: str
    base_hash: str
    eol: str
    report: list[str] = field(default_factory=list)
    diff: str = ""
    changed: bool = True


# =============================================================================
# 変更の形の検証 / validating the shape of the changes
# =============================================================================

def _text_ok(value: Any, limit: int, what: str, problems: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{what}: 空でない文字列にしてください")
        return None
    if len(value) > limit or any(c in value for c in "\r\n\x00") or not value.isprintable():
        problems.append(f"{what}: {limit} 文字以内の 1 行にしてください（改行・制御文字は不可）")
        return None
    return value.strip()


def _date_ok(value: Any, what: str, problems: list[str]) -> str | None:
    if not isinstance(value, str):
        problems.append(f"{what}: YYYY-MM-DD の文字列にしてください")
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        problems.append(f"{what}: 日付として読めません: {value!r}")
        return None


def validate_changes(raw: Any) -> list[dict[str, Any]]:
    """`changes` の形を検証して、正規化した一覧を返す。誤りは全部まとめて WbsEditError。"""
    items = raw.get("changes") if isinstance(raw, dict) else raw
    problems: list[str] = []
    if not isinstance(items, list) or not items:
        raise WbsEditError("changes は 1 件以上の一覧にしてください / `changes` must be a "
                           "non-empty list")
    if len(items) > MAX_CHANGES:
        raise WbsEditError(f"一度に反映できるのは {MAX_CHANGES} 件までです")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        where = f"変更 {index}"
        if not isinstance(item, dict):
            problems.append(f"{where}: マッピングにしてください")
            continue
        op = item.get("op")
        if op == "set":
            node = _text_ok(item.get("node"), 40, f"{where} node", problems)
            fld = item.get("field")
            if fld not in SET_FIELDS:
                problems.append(f"{where}: field は {', '.join(SET_FIELDS)} のどれか: {fld!r}")
                continue
            value = item.get("value")
            if value is None:
                if fld in NOT_REMOVABLE:
                    problems.append(f"{where}: {fld} は消せません")
                    continue
            elif fld == "name":
                value = _text_ok(value, 200, f"{where} name", problems)
            elif fld == "status":
                if value not in STATUSES:
                    problems.append(f"{where}: status は {', '.join(STATUSES)} のどれか: {value!r}")
                    continue
            elif fld == "effort":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    problems.append(f"{where}: effort は 0 以上の数値: {value!r}")
                    continue
            elif fld in ("priority", "owner"):
                value = _text_ok(value, 60, f"{where} {fld}", problems)
            elif fld == "notes":
                value = _text_ok(value, 500, f"{where} notes", problems)
            elif fld in ("due", "done_on"):
                value = _date_ok(value, f"{where} {fld}", problems)
            elif fld == "depends_on":
                if (not isinstance(value, list) or len(value) > 20
                        or any(not isinstance(v, str) or not _ID.match(v) for v in value)):
                    problems.append(f"{where}: depends_on は id の一覧（20 件まで）")
                    continue
            if node is not None and (value is not None or fld not in NOT_REMOVABLE):
                out.append({"op": "set", "node": node, "field": fld, "value": value})
        elif op == "add_evidence":
            node = _text_ok(item.get("node"), 40, f"{where} node", problems)
            values = item.get("values")
            if not isinstance(values, list) or not values or len(values) > 10:
                problems.append(f"{where}: values は 1〜10 件の一覧")
                continue
            clean = []
            for value in values:
                text = _text_ok(value, 300, f"{where} 証拠", problems)
                if text is not None and not _safe_relative(text.partition("::")[0].strip()):
                    problems.append(f"{where}: 証拠のパスはリポジトリ内の相対パスだけ: {text!r}")
                    text = None
                if text is not None:
                    clean.append(text)
            if node is not None and len(clean) == len(values):
                out.append({"op": "add_evidence", "node": node, "values": clean})
        elif op == "add":
            parent = _text_ok(item.get("parent"), 40, f"{where} parent", problems)
            spec = item.get("node")
            if not isinstance(spec, dict) or set(spec) - set(NEW_NODE_KEYS):
                problems.append(f"{where}: node は {', '.join(NEW_NODE_KEYS)} だけのマッピング")
                continue
            node_id = spec.get("id")
            if not isinstance(node_id, str) or not _ID.match(node_id):
                problems.append(f"{where}: 新しい id は英数字と . _ - の 31 文字以内: {node_id!r}")
                continue
            status = spec.get("status", "todo")
            if status not in ADD_STATUSES:
                problems.append(f"{where}: 新しい作業の status は {', '.join(ADD_STATUSES)}")
                continue
            sub: list[str] = []
            new_node: dict[str, Any] = {"id": node_id, "name": _text_ok(
                spec.get("name"), 200, f"{where} name", sub), "status": status}
            if spec.get("effort") is not None:
                eff = spec["effort"]
                if isinstance(eff, bool) or not isinstance(eff, (int, float)) or eff < 0:
                    sub.append(f"{where}: effort は 0 以上の数値")
                else:
                    new_node["effort"] = eff
            for key, limit in (("priority", 60), ("owner", 60), ("notes", 500)):
                if spec.get(key) is not None:
                    new_node[key] = _text_ok(spec[key], limit, f"{where} {key}", sub)
            if spec.get("due") is not None:
                new_node["due"] = _date_ok(spec["due"], f"{where} due", sub)
            if spec.get("depends_on") is not None:
                deps = spec["depends_on"]
                if (not isinstance(deps, list) or any(not isinstance(d, str) or not _ID.match(d)
                                                      for d in deps)):
                    sub.append(f"{where}: depends_on は id の一覧")
                else:
                    new_node["depends_on"] = deps
            problems.extend(sub)
            if parent is not None and not sub:
                out.append({"op": "add", "parent": parent, "node": new_node})
        else:
            problems.append(f"{where}: op は set / add_evidence / add のどれか: {op!r}")
    if problems:
        raise WbsEditError(problems)
    return out


# =============================================================================
# 行単位の書き換え / line-level editing
# =============================================================================

_ID_LINE = re.compile(
    r"""^(?P<ind>[ ]*)-[ ]+id:[ ]*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<pl>[^\s#]+))[ ]*(?:\#.*)?$""")
_QUOTED = re.compile(r'^"(?:[^"\\]|\\.)*"')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _find_node(lines: list[str], node_id: str) -> int:
    found = [i for i, line in enumerate(lines)
             if (m := _ID_LINE.match(line)) and (m["dq"] or m["sq"] or m["pl"]) == node_id]
    if not found:
        raise WbsEditError(f"ノード {node_id} が WBS に見つかりません、または "
                           f"`- id:` の形で書かれていません")
    if len(found) > 1:
        raise WbsEditError(f"ノード {node_id} が複数あります")
    return found[0]


def _block(lines: list[str], start: int) -> tuple[int, int, int]:
    """(開始, 終了（含まない）, キーの桁)。末尾の空行・コメントは含めない。"""
    match = _ID_LINE.match(lines[start])
    assert match is not None
    key_indent = _indent(lines[start]) + 2
    last = start
    for i in range(start + 1, len(lines)):
        if _blank_or_comment(lines[i]):
            continue
        if _indent(lines[i]) < key_indent:
            break
        last = i
    return start, last + 1, key_indent


def _key_line(lines: list[str], start: int, end: int, key_indent: int, key: str) -> int | None:
    for i in range(start, end):
        text = lines[i][key_indent:] if i == start else (
            lines[i][key_indent:] if _indent(lines[i]) == key_indent else "")
        if re.match(rf"{re.escape(key)}:(\s|$)", text):
            return i
    return None


def _children_line(lines: list[str], start: int, end: int, key_indent: int) -> int | None:
    return _key_line(lines, start, end, key_indent, "children")


def _insert_at(lines: list[str], start: int, end: int, key_indent: int) -> int:
    """このノード自身の項目を足す位置（子の一覧があればその直前）。"""
    children = _children_line(lines, start, end, key_indent)
    return children if children is not None else end


def _fmt(field_name: str, value: Any) -> str:
    if field_name in ("status", "priority", "due", "done_on", "effort"):
        if field_name in ("priority",) and not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]*", str(value)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    if field_name == "depends_on":
        return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _split_value(rest: str) -> tuple[str, str]:
    """`値  # コメント` を (値, コメント付きの末尾) に分ける。"""
    rest = rest.rstrip()
    quoted = _QUOTED.match(rest)
    if quoted:
        return quoted.group(0), rest[quoted.end():]
    if rest.startswith("["):
        close = rest.find("]")
        return (rest[:close + 1], rest[close + 1:]) if close >= 0 else (rest, "")
    cut = rest.find(" #")
    return (rest[:cut].rstrip(), rest[cut:]) if cut >= 0 else (rest, "")


def _set_field(lines: list[str], node_id: str, field_name: str, value: Any) -> str | None:
    """1 項目を書き換える。変更の説明（変わらなければ None）。"""
    start = _find_node(lines, node_id)
    start, end, key_indent = _block(lines, start)
    at = _key_line(lines, start, end, key_indent, field_name)
    if at is not None:
        text = lines[at][key_indent:]
        rest = text[len(field_name) + 1:].strip()
        if not rest or rest[0] in "|>&*!{" or rest.startswith(("- ", "|", ">")):
            raise WbsEditError(f"ノード {node_id} の {field_name} は、この形式（複数行・参照など）では"
                               f"書き換えられません。人が直してください")
        nxt = lines[at + 1] if at + 1 < end else ""
        if nxt.strip() and not _blank_or_comment(nxt) and _indent(nxt) > key_indent \
                and not nxt.lstrip().startswith("- "):
            raise WbsEditError(f"ノード {node_id} の {field_name} は複数行です。人が直してください")
        old_value, tail = _split_value(rest)
        try:
            current = yaml.safe_load(old_value)
        except yaml.YAMLError:
            current = old_value
        if value is None:
            del lines[at]
            return f"{node_id} {field_name}: {old_value} → （削除）"
        if _same(field_name, current, value):
            return None
        new_value = _fmt(field_name, value)
        prefix = lines[at][:key_indent] + f"{field_name}: "
        lines[at] = prefix + new_value + tail
        return f"{node_id} {field_name}: {old_value} → {new_value}"
    if value is None:
        return None
    where = _insert_at(lines, start, end, key_indent)
    lines.insert(where, " " * key_indent + f"{field_name}: {_fmt(field_name, value)}")
    return f"{node_id} {field_name}: （なし） → {_fmt(field_name, value)}"


def _same(field_name: str, current: Any, value: Any) -> bool:
    if field_name == "effort":
        return isinstance(current, (int, float)) and float(current) == float(value)
    if field_name in ("due", "done_on"):
        return str(current) == str(value)
    if field_name == "depends_on":
        return list(current or []) == list(value)
    return str(current).strip() == str(value).strip()


def _add_evidence(lines: list[str], node_id: str, values: list[str]) -> str | None:
    start = _find_node(lines, node_id)
    start, end, key_indent = _block(lines, start)
    at = _key_line(lines, start, end, key_indent, "evidence")
    existing: list[str] = []
    if at is not None:
        rest = lines[at][key_indent + len("evidence:"):].strip()
        if rest:
            raise WbsEditError(f"ノード {node_id} の evidence は一覧の形（`- 項目`）で書かれて"
                               f"いません。人が直してください")
        item_indent, last = None, at
        for i in range(at + 1, end):
            if _blank_or_comment(lines[i]):
                continue
            stripped = lines[i].lstrip()
            if _indent(lines[i]) >= key_indent and stripped.startswith("- "):
                if _indent(lines[i]) == key_indent and _ID_LINE.match(lines[i]):
                    break                                   # 兄弟のノード
                item_indent = _indent(lines[i]) if item_indent is None else item_indent
                if _indent(lines[i]) != item_indent:
                    break
                try:
                    existing.append(str(yaml.safe_load(stripped[2:])))
                except yaml.YAMLError:
                    existing.append(stripped[2:].strip())
                last = i
            else:
                break
        fresh = [v for v in values if v not in existing]
        if not fresh:
            return None
        pad = " " * (item_indent if item_indent is not None else key_indent + 2)
        for offset, value in enumerate(fresh, 1):
            lines.insert(last + offset, pad + "- " + json.dumps(value, ensure_ascii=False))
        return f"{node_id} 証拠を追加: " + ", ".join(fresh)
    where = _insert_at(lines, start, end, key_indent)
    pad = " " * key_indent
    block = [pad + "evidence:"] + [pad + "  - " + json.dumps(v, ensure_ascii=False) for v in values]
    lines[where:where] = block
    return f"{node_id} 証拠を追加: " + ", ".join(values)


def _add_node(lines: list[str], parent_id: str, node: dict[str, Any]) -> str | None:
    if any((m := _ID_LINE.match(line)) and (m["dq"] or m["sq"] or m["pl"]) == node["id"]
           for line in lines):
        # 同じ id が既にある: 同じ名前なら反映済み（冪等）、違えば衝突
        existing = _find_node(lines, node["id"])
        text = lines[existing + 1: existing + 4]
        if any(json.dumps(node["name"], ensure_ascii=False) in t or node["name"] in t for t in text):
            return None
        raise WbsEditError(f"id {node['id']} はすでに別の作業に使われています")
    start = _find_node(lines, parent_id)
    start, end, key_indent = _block(lines, start)
    children = _children_line(lines, start, end, key_indent)
    if children is None:
        raise WbsEditError(f"ノード {parent_id} には子がありません。この操作は、すでに子を持つ"
                           f"親の下にだけ作業を足せます")
    dash = None
    for i in range(children + 1, end):
        if lines[i].lstrip().startswith("- ") and _ID_LINE.match(lines[i]):
            dash = _indent(lines[i])
            break
    if dash is None:
        raise WbsEditError(f"ノード {parent_id} の children の形を読めません（空の一覧など）")
    pad = " " * dash
    order = ("name", "status", "effort", "priority", "owner", "due", "depends_on", "notes")
    new = [pad + f"- id: {json.dumps(node['id'])}"]
    for key in order:
        if node.get(key) is not None:
            new.append(pad + "  " + f"{key}: {_fmt(key, node[key])}")
    lines[end:end] = new
    return f"{parent_id} の下に {node['id']} を追加: {node['name']}"


# =============================================================================
# 計画と反映 / planning and writing
# =============================================================================

def _load_text(text: str) -> tuple[Wbs, list[Any]]:
    fd, name = tempfile.mkstemp(suffix=".yaml", prefix="wbs-edit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return load_wbs(Path(name))
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _snapshot(wbs: Wbs) -> dict[str, dict[str, Any]]:
    return {n.id: {"name": n.name, "status": n.status, "effort": n.effort,
                   "priority": n.priority, "due": n.due, "owner": n.owner, "notes": n.notes,
                   "done_on": n.done_on, "depends_on": list(n.depends_on),
                   "evidence": list(n.evidence), "parent": n.parent.id if n.parent else None}
            for n in wbs.all_nodes()}


def plan_changes(path: Path, root: Path, raw_changes: Any, *, as_of: date | None = None,
                 expect_wbs_id: str | None = None) -> Plan:
    """変更を検証して、反映後の内容を作る（ファイルは書かない）。

    `expect_wbs_id` を渡すと、提案の対象の WBS とファイルの WBS が同じ id のときだけ進む
    （別の WBS の提案を、取り違えてこのファイルへ反映しない）。
    誤りがあれば WbsEditError（理由は全部まとめて）。
    """
    changes = validate_changes(raw_changes)
    try:
        if path.stat().st_size > MAX_WBS_BYTES:
            raise WbsEditError(f"WBS ファイルが大きすぎます: {path}")
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise WbsEditError(f"WBS ファイルを読めません: {exc}") from exc
    text = raw_bytes.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    old_text = text.replace("\r\n", "\n")
    as_of = as_of or date.today()

    try:
        old_wbs, old_problems = _load_text(old_text)
    except WbsError as exc:
        raise WbsEditError(f"今の WBS ファイルを読めません: {exc}") from exc
    if expect_wbs_id is not None and old_wbs.id != expect_wbs_id:
        raise WbsEditError(
            f"この提案の対象の WBS は '{expect_wbs_id}' ですが、ファイルの WBS は "
            f"'{old_wbs.id}' です。別の WBS への提案は反映しません")
    old_ids = {n.id for n in old_wbs.all_nodes()}

    lines = old_text.split("\n")
    report: list[str] = []
    named: set[str] = set()
    added: set[str] = set()
    for change in changes:
        if change["op"] == "set":
            named.add(change["node"])
            said = _set_field(lines, change["node"], change["field"], change["value"])
        elif change["op"] == "add_evidence":
            named.add(change["node"])
            said = _add_evidence(lines, change["node"], change["values"])
        else:
            named.add(change["parent"])
            added.add(change["node"]["id"])
            said = _add_node(lines, change["parent"], change["node"])
        if said:
            report.append(said)
    new_text = "\n".join(lines)

    problems: list[str] = []
    try:
        new_wbs, new_problems = _load_text(new_text)
    except WbsError as exc:
        raise WbsEditError(f"反映後の WBS を読めません（反映しません）: {exc}") from exc
    new_ids = {n.id for n in new_wbs.all_nodes()}

    def key(p: Any) -> tuple[str, str | None, str]:
        return (p.code, p.node, p.message)

    before = {key(p) for p in old_problems + validate_evidence(old_wbs, root, as_of)}
    after_all = new_problems + validate_evidence(new_wbs, root, as_of)
    for p in after_all:
        if key(p) in before:
            continue
        if p.level == "error" or p.code in BLOCKING_WARNINGS:
            problems.append(f"反映すると新しい問題が出ます: [{p.code}] {p.node or ''} {p.message}")

    # 完了にしたものは、done_on と証拠が要る。
    old_snap, new_snap = _snapshot(old_wbs), _snapshot(new_wbs)
    for node_id, now in new_snap.items():
        was = old_snap.get(node_id)
        if now["status"] == "done" and (was is None or was["status"] != "done"):
            if not now["done_on"] or not now["evidence"]:
                problems.append(f"{node_id} を完了にするには、done_on と証拠（evidence）が要ります")

    # 依頼していないノードが変わっていないこと（行の書き換えの取りこぼし検出）。
    for node_id in old_ids & new_ids:
        if old_snap[node_id] != new_snap[node_id] and node_id not in named:
            problems.append(f"依頼していないノード {node_id} が変わります（反映しません）")
    for node_id in (new_ids - old_ids) - added:
        problems.append(f"依頼していないノード {node_id} が増えます（反映しません）")
    for node_id in old_ids - new_ids:
        problems.append(f"ノード {node_id} が消えます（反映しません）")
    if problems:
        raise WbsEditError(problems)

    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=str(path), tofile=f"{path} (反映後)"))
    return Plan(path=str(path), old_text=old_text, new_text=new_text,
                base_hash=hashlib.sha256(raw_bytes).hexdigest(), eol=eol, report=report,
                diff=diff, changed=new_text != old_text)


def write_plan(plan: Plan) -> None:
    """計画をファイルに書く。読んだ後に書き換えられていたら書かない。原子的に置き換える。"""
    path = Path(plan.path)
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise WbsEditError(f"WBS ファイルを読めません: {exc}") from exc
    if hashlib.sha256(current).hexdigest() != plan.base_hash:
        raise WbsEditError("計画を作った後に WBS ファイルが書き換えられました。"
                           "もう一度確かめてから反映してください")
    if not plan.changed:
        return
    data = plan.new_text.replace("\n", plan.eol).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wbs-edit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise WbsEditError(f"WBS ファイルを書けません: {exc}") from exc
