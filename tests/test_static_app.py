"""app.js の静的な点検。

同じ名前の関数を 2 回宣言すると、あとの方が前の方を黙って上書きする（実際に、タスク画面の関数が
旧い PMO 画面の関数を上書きして、PMO 画面が空になった）。画面を足しても、これは起きないようにする。

A function declared twice silently replaces the earlier one (it once emptied the legacy PMO screen),
so declarations must be unique.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "aipmo" / "web" / "static"


def test_function_names_in_app_js_are_unique():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    names = re.findall(r"^(?:async )?function ([A-Za-z0-9_$]+)", source, flags=re.MULTILINE)
    assert len(names) > 50
    assert [n for n, c in Counter(names).items() if c > 1] == []


def test_top_level_bindings_in_app_js_are_unique():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    names = re.findall(r"^(?:const|let) ([A-Za-z0-9_$]+)", source, flags=re.MULTILINE)
    assert [n for n, c in Counter(names).items() if c > 1] == []


def test_every_tab_has_a_section_in_the_page():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    order = re.search(r"const TAB_ORDER = \[([^\]]*)\]", source).group(1)
    tabs = re.findall(r'"([a-z]+)"', order)
    assert tabs and len(set(tabs)) == len(tabs)
    for tab in tabs:
        assert f'data-tab="{tab}"' in html, tab
