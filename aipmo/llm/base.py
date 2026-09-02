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


