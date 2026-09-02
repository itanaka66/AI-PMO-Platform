"""profile 名 → 具体的な LLM プロバイダの割り当て。

テンプレートは profile 名しか書かない。どの提供元・どのモデルに割り当てるかは
設定で決める。提供元を乗り換えてもテンプレートは変わらない。

Templates only ever name a profile; which provider and model it resolves to is
configuration. Switching providers leaves every template untouched.
"""
from __future__ import annotations

from typing import Any

from .base import EchoProvider, LLMProvider, OpenAICompatibleProvider
from .presets import PRESETS, ProviderError


class LLMRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, profile: str, provider: LLMProvider) -> None:
        self._providers[profile] = provider

    def get(self, profile: str) -> LLMProvider:
        if profile in self._providers:
            return self._providers[profile]
        if "default" in self._providers:
            return self._providers["default"]
        raise KeyError(
            f"LLM プロファイル '{profile}' が未設定で、default も定義されていません"
        )

    def profiles(self) -> list[str]:
        return sorted(self._providers)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> LLMRegistry:
        """設定の llm セクションから構築する。

        llm:
          default:
            provider: groq          # openai / gemini / groq / openrouter
            model: openai/gpt-oss-120b
          fast:
            provider: ollama
            model: qwen2.5:7b
        """
        registry = cls()
        for profile, spec in (config or {}).items():
            registry.register(profile, build_provider(spec))
        return registry


def build_provider(spec: dict[str, Any] | str | None) -> LLMProvider:
    """設定の一節から提供元を作る / build one provider from its config block."""
    if isinstance(spec, str):
        spec = {"provider": spec}
    spec = dict(spec or {})
    provider = spec.pop("provider", "openai")

    if provider == "ollama" and "host" in spec:
        host = spec.pop("host")
        if not host.endswith("/v1") and not host.endswith("/v1/"):
            host = host.rstrip("/") + "/v1"
        spec["base_url"] = host

    if provider == "echo":
        return EchoProvider(**spec)
    if provider in PRESETS:
        return OpenAICompatibleProvider(provider=provider, **spec)

    raise ProviderError(
        f"未知の提供元 / unknown provider: {provider!r}\n"
        f"  使えるもの / available: {', '.join(sorted(PRESETS))}"
    )
