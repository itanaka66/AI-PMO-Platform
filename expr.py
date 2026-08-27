"""{{ ... }} プレースホルダの解決。

外部テンプレートエンジン (Jinja2 等) に依存させず、
意図的に機能を絞った小さな評価器にしている。理由:
  - テンプレートは第三者が書いて配布する想定 (教材販売)
  - 任意コード実行を許すと、配布テンプレートが攻撃面になる
参照できるのは値の取り出しと、限定的な比較のみ。
"""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

# 条件式: <path> <op> <literal>
CONDITION = re.compile(
    r"^\s*([A-Za-z_][\w.\[\]]*)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$"
)


class ResolutionError(Exception):
    pass


def lookup(path: str, scope: dict[str, Any]) -> Any:
    """'steps.fetch.output.items[0].title' のようなパスを辿る。"""
    current: Any = scope
    token_re = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
    for m in token_re.finditer(path):
        key, index = m.group(1), m.group(2)
        try:
            if index is not None:
                current = current[int(index)]
            elif isinstance(current, dict):
                current = current[key]
            else:
                current = getattr(current, key)
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise ResolutionError(f"参照を解決できません: {path}") from exc
    return current


def render(value: Any, scope: dict[str, Any]) -> Any:
    """文字列 / dict / list を再帰的に走査してプレースホルダを埋める。

    文字列全体がちょうど 1 つのプレースホルダの場合は、
    文字列化せず元の型 (dict, list, int ...) のまま返す。
    LLM の JSON 出力を次のステップにそのまま渡すために必要。
    """
    if isinstance(value, str):
        full = PLACEHOLDER.fullmatch(value.strip())
        if full:
            return lookup(full.group(1), scope)
        return PLACEHOLDER.sub(lambda m: _stringify(lookup(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    return value


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def evaluate_condition(expr: str, scope: dict[str, Any]) -> bool:
    """`when:` の評価。真偽値そのものか、単純な二項比較のみを許す。"""
    expr = expr.strip()

    full = PLACEHOLDER.fullmatch(expr)
    if full:
        return bool(lookup(full.group(1), scope))

    m = CONDITION.match(PLACEHOLDER.sub(lambda x: x.group(1), expr))
    if not m:
        raise ResolutionError(f"条件式を解釈できません: {expr}")

    left = lookup(m.group(1), scope)
    op = m.group(2)
    right = _parse_literal(m.group(3), scope)

    ops = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        ">": lambda a, b: a > b,
        "<": lambda a, b: a < b,
        ">=": lambda a, b: a >= b,
        "<=": lambda a, b: a <= b,
    }
    return bool(ops[op](left, right))


def _parse_literal(token: str, scope: dict[str, Any]) -> Any:
    token = token.strip()
    if token.startswith(("'", '"')) and token.endswith(("'", '"')):
        return token[1:-1]
    lowered = token.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return lookup(token, scope)
