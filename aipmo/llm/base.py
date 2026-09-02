"""LLM プロバイダ抽象。

テンプレートは「profile 名」しか書かない。
どのプロバイダ・どのモデルに割り当てるかは実行環境の設定で決める。

  Docker 版      : default -> ollama (ローカル)
  非 Docker 版   : default -> openai (クラウド)

同じテンプレートが両方の環境でそのまま動くことが要件なので、
この分離は必須。
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class LLMRequest:
    prompt: str
    system: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    json_mode: bool = False

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: object | None = None   # 会話履歴に戻すため / to replay into history

    def as_json(self) -> object:
        """LLM 出力から JSON を取り出す。

        json_mode を指定しても ```json フェンスや前置きが混ざることは
        実運用では普通に起きるので、素の json.loads に頼らない。
        """
        text = self.text.strip()
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fenced:
            text = fenced.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 最初の { または [ から末尾の対応する括弧までを試す
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        raise ValueError("LLM 出力を JSON として解釈できませんでした")

class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        ...

    def converse(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.2, max_tokens: int = 4096) -> LLMResponse:
        """道具を渡した多ターンの対話 / a multi-turn exchange with tools.

        エージェントはこちらを使う。complete は1往復しかしないので、
        道具を呼んだ結果を返して続けることができない。

        Agents use this. `complete` is a single exchange and cannot carry a
        tool result back into the conversation.
        """
        raise NotImplementedError(
            f"{self.name} は道具を使う対話に対応していません "
            f"/ {self.name} does not implement tool-using conversation"
        )

class EchoProvider(LLMProvider):
    """テスト用。API キー無しでエンジン全体を通せるようにするためのもの。"""

    name = "echo"

    def __init__(self, canned: str | None = None,
                 script: list[LLMResponse] | None = None) -> None:
        self.canned = canned
        self.script = list(script or [])
        self.calls: list[LLMRequest] = []
        self.conversations: list[list[dict]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.canned is not None:
            return LLMResponse(text=self.canned, model="echo")
        if request.json_mode:
            return LLMResponse(text="{}", model="echo")
        return LLMResponse(text=request.prompt, model="echo")

    def converse(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.2, max_tokens: int = 4096) -> LLMResponse:
        self.conversations.append(list(messages))
        if self.script:
            return self.script.pop(0)
        return LLMResponse(text="", model="echo")

class OpenAICompatibleProvider(LLMProvider):
    """OpenAI 互換エンドポイント全般 / any OpenAI-compatible endpoint.

    OpenAI 本体、Gemini（互換層）、Groq、OpenRouter、vLLM、LM Studio、
    llama.cpp をこの一つで扱う。違いは presets.py にデータとして置いてある。

    Covers OpenAI itself, Gemini via its compatibility layer, Groq, OpenRouter,
    vLLM, LM Studio and llama.cpp. The differences live as data in presets.py.
    """

    def __init__(
        self,
        provider: str = "openai",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 180.0,
        **extra: Any,
    ) -> None:
        from .presets import ProviderError, resolve

        self.preset = resolve(provider)
        self.name = self.preset.name
        self.model = model or self.preset.default_chat_model
        self.timeout = timeout
        self.extra = extra

        if not self.model:
            raise ProviderError(
                f"{self.name}: model を指定してください / a model must be named.\n"
                f"  {self.preset.notes}"
            )

        self.base_url = base_url or self.preset.base_url
        self._api_key = api_key
        if self._api_key is None and self.preset.api_key_env:
            self._api_key = os.environ.get(self.preset.api_key_env)
        # ローカルは鍵不要だが、OpenAI SDK は空を嫌うので置き石を渡す。
        # Local servers need no key, but the SDK refuses an empty one.
        if self._api_key is None and self.preset.local:
            self._api_key = "not-needed"

    def complete(self, request: LLMRequest) -> LLMResponse:
        from openai import OpenAI  # 遅延 import / lazy import

        client = OpenAI(api_key=self._api_key, base_url=self.base_url,
                        timeout=self.timeout)

        messages = []
        system = request.system
        if request.json_mode and not self.preset.supports_json_mode:
            # response_format を受け付けない相手には、プロンプトで JSON を求める。
            # 送りつけると 400 を返す提供元があるため、黙って落とさない。
            # Where response_format is unsupported, ask for JSON in the prompt:
            # some providers answer an unsupported field with a 400 rather than
            # ignoring it.
            instruction = ("Respond with a single valid JSON object and nothing "
                           "else. No markdown fences, no commentary.")
            system = f"{system}\n\n{instruction}" if system else instruction
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": request.prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            **self.extra,
        }
        if request.json_mode and self.preset.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        result = client.chat.completions.create(**kwargs)
        usage = getattr(result, "usage", None)
        return LLMResponse(
            text=result.choices[0].message.content or "",
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

    def converse(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.2, max_tokens: int = 4096) -> LLMResponse:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, base_url=self.base_url,
                        timeout=self.timeout)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self.extra,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        result = client.chat.completions.create(**kwargs)
        message = result.choices[0].message
        usage = getattr(result, "usage", None)

        calls: list[ToolCall] = []
        for call in (getattr(message, "tool_calls", None) or []):
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # 引数が壊れていても落とさない。エージェントに差し戻して
                # 直させる方が、実行全体を失うより良い。
                # Malformed arguments are handed back to the agent to correct
                # rather than losing the whole run.
                arguments = {"__malformed__": call.function.arguments}
            calls.append(ToolCall(id=call.id, name=call.function.name,
                                  arguments=arguments))

        return LLMResponse(
            text=message.content or "",
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            tool_calls=calls,
            raw_message=message,
        )

class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI 本体。既存の設定との互換のため名前を残している。

    Kept as a named class so existing configs keep working.
    """

    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, **extra: Any) -> None:
        super().__init__(provider="openai", model=model, api_key=api_key,
                         base_url=base_url, **extra)

class OllamaProvider(LLMProvider):
    """ローカル LLM。Docker 版で使う。"""

    name = "ollama"

    def __init__(self, model: str = "qwen2.5:14b",
                 host: str | None = None) -> None:
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def complete(self, request: LLMRequest) -> LLMResponse:
        import urllib.request

        payload = {
            "model": self.model,
            "prompt": request.prompt,
            "system": request.system or "",
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.json_mode:
            payload["format"] = "json"

        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        return LLMResponse(
            text=body.get("response", ""),
            model=self.model,
            input_tokens=body.get("prompt_eval_count"),
            output_tokens=body.get("eval_count"),
        )

def _claude_tool_schema(openai_tool: dict) -> dict:
    """OpenAI の function-calling 形式を Claude の道具定義形式に変換する。

    ToolBox（engine/agent.py）は道具の定義を1つの共通形式
    ({"type": "function", "function": {"name","description","parameters"}})
    で作っている。Claude はこの形をそのまま受け付けないので、ここで変換する
    -- ToolBox 側は提供元がどちらかを知る必要が無いままでいられる。

    Converts ToolBox's one shared tool-definition shape (OpenAI's
    function-calling format) into Claude's, so ToolBox itself never needs to
    know which provider it is talking to.
    """
    function = openai_tool.get("function", openai_tool)
    return {
        "name": function["name"],
        "description": function.get("description", ""),
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }

def _is_all_tool_results(content: Any) -> bool:
    return isinstance(content, list) and bool(content) and all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )

def _to_claude_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """OpenAI 形式の会話履歴を Claude の Messages API 形式に変換する。

    3つの違いをここで吸収する:
      - system はメッセージではなく、別枠のトップレベル引数
      - 道具呼び出し／結果はメッセージそのものの role ではなく、
        content の中の tool_use / tool_result ブロック
      - 連続する tool ロールのメッセージは、Claude では1つの user
        メッセージにまとめた複数の tool_result ブロックとして送る

    engine/agent.py の会話履歴組み立て（_recognize_request / _assistant_turn
    / _observe）は provider 非依存で OpenAI 形式のまま書かれているため、
    この変換は Claude 用の provider の内側だけで完結させる。

    Absorbs three differences here: system is a separate top-level
    parameter rather than a message; tool calls/results are content blocks
    inside a message rather than a message role of their own; and
    consecutive role="tool" messages become tool_result blocks inside a
    single user message, which is the shape Claude expects for multiple
    results answering one assistant turn. engine/agent.py's history
    builders stay provider-agnostic (OpenAI-shaped); this translation is
    contained entirely inside the Claude provider.
    """
    system: str | None = None
    out: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")

        if role == "system":
            content = message.get("content") or ""
            system = content if system is None else f"{system}\n\n{content}"
            continue

        if role == "user":
            out.append({"role": "user", "content": message.get("content") or ""})
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        arguments = json.loads(raw_arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                else:
                    arguments = raw_arguments or {}
                blocks.append({
                    "type": "tool_use", "id": call.get("id"),
                    "name": function.get("name"), "input": arguments,
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": message.get("content") or "",
            }
            if out and out[-1]["role"] == "user" and _is_all_tool_results(out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        # 未知の role はそのまま user として渡す。黙って消すより、
        # 相手に判断させたほうが安全。
        # An unrecognised role is passed through as a user message rather
        # than silently dropped - letting the API judge it is safer.
        out.append({"role": "user", "content": message.get("content") or ""})

    return system, out

class AnthropicProvider(LLMProvider):
    """Claude（Anthropic Messages API）。

    OpenAI 互換ではない（別の API 形状）ため、OpenAICompatibleProvider には
    乗せず、Ollama と同じく専用クラスとして持つ。会話履歴と道具定義の変換は
    _to_claude_messages / _claude_tool_schema が行う。

    Claude speaks its own Messages API, not an OpenAI-compatible one, so --
    like Ollama -- it gets its own class rather than riding
    OpenAICompatibleProvider. Conversation-history and tool-definition
    translation live in _to_claude_messages / _claude_tool_schema above.
    """

    name = "claude"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 180.0,
        **extra: Any,
    ) -> None:
        from .presets import PRESETS

        preset = PRESETS["claude"]
        self.model = model or preset.default_chat_model
        self.timeout = timeout
        self.extra = extra
        self._api_key = api_key or os.environ.get(preset.api_key_env or "ANTHROPIC_API_KEY")
        self.base_url = base_url

        if not self._api_key:
            from .presets import ProviderError

            raise ProviderError(
                f"claude: {preset.api_key_env} が設定されていません "
                f"/ {preset.api_key_env} is not set.\n"
                f"  {preset.notes}"
            )

    def _client(self):
        from anthropic import Anthropic  # 遅延 import / lazy import

        kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self.timeout}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return Anthropic(**kwargs)

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._client()

        system = request.system
        if request.json_mode:
            # response_format に相当するものが無いので、プロンプト側で要求する
            # （OpenAICompatibleProvider が非対応の相手にしているのと同じ処理）。
            # No response_format equivalent exists; ask in the prompt instead
            # (the same treatment OpenAICompatibleProvider gives an
            # unsupported endpoint).
            instruction = ("Respond with a single valid JSON object and nothing "
                           "else. No markdown fences, no commentary.")
            system = f"{system}\n\n{instruction}" if system else instruction

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
            **self.extra,
        }
        if system:
            kwargs["system"] = system

        result = client.messages.create(**kwargs)
        text = "".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        usage = getattr(result, "usage", None)
        return LLMResponse(
            text=text,
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )

    def converse(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.2, max_tokens: int = 4096) -> LLMResponse:
        client = self._client()
        system, claude_messages = _to_claude_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": claude_messages,
            **self.extra,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [_claude_tool_schema(tool) for tool in tools]

        result = client.messages.create(**kwargs)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in result.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name,
                                      arguments=dict(block.input or {})))

        usage = getattr(result, "usage", None)
        return LLMResponse(
            text="".join(text_parts),
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            tool_calls=calls,
            # raw_message はそのまま残さない: engine/agent.py の
            # _assistant_turn は raw_message が無ければ OpenAI 形式の
            # 辞書を tool_calls/text から自分で組み立てる（既定の分岐）。
            #
            # raw_message is left unset: engine/agent.py's _assistant_turn
            # falls back to building an OpenAI-shaped dict from
            # tool_calls/text when raw_message is absent.
            raw_message=None,
        )

