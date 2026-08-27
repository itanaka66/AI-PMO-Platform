"""外部ツール連携アダプタの共通インターフェース。

Jira / Slack / Teams など、どのツールも
「アダプタ名 + アクション名 + 引数」という同じ形で呼ばれる。
テンプレート側から見て全部同じ形になるので、
新しいツールを足してもエンジンと DSL に変更が要らない。

冪等性についての約束:
  同じ会議を 2 回処理しても Issue が重複しないようにするため、
  書き込み系アクションは idempotency_key を受け取れること。
  各アダプタは、そのキーで既存レコードを検索してから作成する。
"""
from __future__ import annotations

import inspect
from abc import ABC
from typing import Any, Callable


class AdapterError(Exception):
    pass


def action(name: str | None = None, *, writes: bool = False) -> Callable:
    """アダプタのメソッドを「テンプレートから呼べるアクション」として公開する。

    writes=True は「外の世界を変える」印。課題を作る、通知を送る、行を書く。
    エージェントに道具として渡すとき、この区別が要る。読むだけの誤りは
    やり直せるが、書いた誤りはやり直せない。

    writes=True marks an action that changes the outside world — creating an
    issue, sending a notification, writing a row. The distinction matters when
    handing tools to an agent: a mistaken read can be retried, a mistaken write
    cannot be taken back.
    """

    def decorator(func: Callable) -> Callable:
        func._aipmo_action = name or func.__name__  # type: ignore[attr-defined]
        func._aipmo_writes = writes                 # type: ignore[attr-defined]
        return func

    return decorator


# Python の型注釈から JSON Schema への対応 / annotation to JSON Schema
_JSON_TYPES = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}

# `from __future__ import annotations` があると注釈は文字列で届く。
# 型そのものを引き当てられないので、先頭の綴りで判定する。
# get_type_hints は前方参照や遅延 import で落ちることがあり、
# 道具の定義づくりで例外を出すのは割に合わない。
#
# With `from __future__ import annotations` the annotation arrives as text, so
# it is matched by its leading token. get_type_hints can raise on forward
# references and lazily imported names, and blowing up while describing a tool
# is not a trade worth making.
_TEXT_TYPES = (
    ("list", "array"), ("dict", "object"), ("bool", "boolean"),
    ("int", "integer"), ("float", "number"), ("str", "string"),
)


def _json_type(annotation: Any) -> str:
    if isinstance(annotation, str):
        text = annotation.strip().lstrip("'\"").lower()
        for prefix, json_type in _TEXT_TYPES:
            if text.startswith(prefix):
                return json_type
        return "string"
    origin = getattr(annotation, "__origin__", annotation)
    return _JSON_TYPES.get(origin, "string")


def describe_action(func: Callable) -> dict[str, Any]:
    """アクションの署名から、LLM に渡す道具の定義を作る。

    定義を手書きさせると、実装と食い違ったまま気づけない。
    署名から起こせば、引数を変えたときに定義も一緒に変わる。

    Deriving the tool definition from the signature keeps it from drifting out
    of step with the implementation, which a hand-written schema always does.
    """
    signature = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, parameter in signature.parameters.items():
        if name in ("self", "idempotency_key"):
            continue

        properties[name] = {"type": _json_type(parameter.annotation)}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)

    doc = (inspect.getdoc(func) or "").strip().split("\n")[0]
    return {
        "description": doc,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


class Adapter(ABC):
    name: str = "base"

    def __init__(self, **config: Any) -> None:
        self.config = config

    def actions(self) -> dict[str, Callable]:
        """公開アクションを収集する。

        マーカーは MRO を遡って探す。サブクラスが @action を付けずに
        メソッドをオーバーライドしただけでアクションが消える、という
        気づきにくい壊れ方を防ぐため。
        """
        found: dict[str, Callable] = {}
        for attr_name, member in inspect.getmembers(self, inspect.ismethod):
            action_name = getattr(member, "_aipmo_action", None)
            if action_name is None:
                for klass in type(self).__mro__:
                    inherited = klass.__dict__.get(attr_name)
                    action_name = getattr(inherited, "_aipmo_action", None)
                    if action_name:
                        break
            if action_name:
                found[action_name] = member
        return found

    def writes(self, action_name: str) -> bool:
        """そのアクションが外の世界を変えるか / whether it changes the world."""
        func = self.actions().get(action_name)
        if func is None:
            return False
        for klass in type(self).__mro__:
            inherited = klass.__dict__.get(func.__name__)
            if inherited is not None and hasattr(inherited, "_aipmo_writes"):
                return bool(inherited._aipmo_writes)
        return bool(getattr(func, "_aipmo_writes", False))

    def describe(self) -> dict[str, dict[str, Any]]:
        """公開アクションの道具定義 / tool definitions for the public actions."""
        return {
            name: {**describe_action(func), "writes": self.writes(name)}
            for name, func in self.actions().items()
        }

    def invoke(self, action_name: str, payload: dict[str, Any]) -> Any:
        actions = self.actions()
        if action_name not in actions:
            raise AdapterError(
                f"アダプタ '{self.name}' にアクション '{action_name}' はありません "
                f"(利用可能: {', '.join(sorted(actions)) or 'なし'})"
            )
        return actions[action_name](**payload)

    def health_check(self) -> bool:
        """設定と認証が有効かを確認する。UI の接続テストで使う。"""
        return True


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> Adapter:
        if name not in self._adapters:
            raise AdapterError(
                f"アダプタ '{name}' が登録されていません "
                f"(登録済み: {', '.join(sorted(self._adapters)) or 'なし'})"
            )
        return self._adapters[name]

    def names(self) -> list[str]:
        return sorted(self._adapters)
