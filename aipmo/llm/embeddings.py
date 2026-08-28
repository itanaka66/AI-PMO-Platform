"""埋め込みベクトル生成 / Embedding generation.

LLM プロバイダと同じく、テンプレートからは論理名しか見えない。
Docker 版はローカル、ノート PC 版はクラウドに割り当てる。

Like LLM providers, templates only ever reference a logical name.
The Docker build maps it to a local model, the laptop build to a cloud API.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod


class Embedder(ABC):
    name: str = "base"
    dimension: int = 0

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashEmbedder(Embedder):
    """テスト用の決定的な擬似埋め込み。外部依存なしで検索経路を通せる。

    Deterministic pseudo-embedding for tests. Lets the retrieval path run
    with no external dependency.
    """

    name = "hash"

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [digest[i % len(digest)] / 255.0 for i in range(self.dimension)]
            norm = sum(v * v for v in raw) ** 0.5 or 1.0
            vectors.append([v / norm for v in raw])
        return vectors


class OpenAICompatibleEmbedder(Embedder):
    """OpenAI 互換の埋め込みエンドポイント / OpenAI-compatible embeddings.

    OpenAI、Gemini（互換層）、vLLM、LM Studio が対象。
    Groq と OpenRouter は埋め込み API を持たないので、ここには来ない
    （presets.require_embeddings が設定読み込み時に弾く）。

    Covers OpenAI, Gemini via its compatibility layer, vLLM and LM Studio.
    Groq and OpenRouter never reach here: they have no embeddings API and
    presets.require_embeddings rejects them while the config is being read.
    """

    def __init__(self, provider: str = "openai", model: str | None = None,
                 dimension: int | None = None, api_key: str | None = None,
                 base_url: str | None = None) -> None:
        from .presets import ProviderError, require_embeddings

        self.preset = require_embeddings(provider)
        self.name = self.preset.name
        self.model = model or (self.preset.default_embed_model or "")
        self.dimension = dimension or (self.preset.default_embed_dimension or 0)

        if not self.model:
            raise ProviderError(
                f"{self.name}: 埋め込みモデル名を指定してください "
                f"/ name an embedding model.\n  {self.preset.notes}"
            )

        self.base_url = base_url or self.preset.base_url
        self._api_key = api_key
        if self._api_key is None and self.preset.api_key_env:
            self._api_key = os.environ.get(self.preset.api_key_env)
        if self._api_key is None and self.preset.local:
            self._api_key = "not-needed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, base_url=self.base_url)
        result = client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in result.data]


class OpenAIEmbedder(OpenAICompatibleEmbedder):
    """既存の設定との互換のため名前を残している / kept for existing configs."""

    def __init__(self, model: str | None = None, dimension: int | None = None,
                 api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(provider="openai", model=model, dimension=dimension,
                         api_key=api_key, base_url=base_url)


class OllamaEmbedder(Embedder):
    """ローカルの Ollama / local Ollama.

    Ollama は埋め込みに独自エンドポイントを使うので、互換層には乗せない。
    Ollama serves embeddings from its own endpoint, so it stays native.
    """

    name = "ollama"

    def __init__(self, model: str = "bge-m3", dimension: int = 1024,
                 host: str | None = None) -> None:
        self.model = model
        self.dimension = dimension
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request

        vectors = []
        for text in texts:
            req = urllib.request.Request(
                f"{self.host}/api/embeddings",
                data=json.dumps({"model": self.model, "prompt": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            vectors.append(body["embedding"])
        return vectors


def build_embedder(spec: dict | None) -> Embedder:
    """設定の embedding 節から埋め込み器を作る。

    Groq や OpenRouter を指定した場合は、ここで分かりやすく落ちる。
    ベクトル検索を使う瞬間まで気づけない方が困る。

    Naming Groq or OpenRouter fails here with an explanation, rather than at
    the first moment vector search is actually used.
    """
    spec = dict(spec or {"provider": "hash"})
    provider = spec.pop("provider", "hash")

    if provider == "ollama":
        return OllamaEmbedder(**spec)
    if provider == "hash":
        return HashEmbedder(**spec)

    from .presets import PRESETS

    if provider in PRESETS:
        return OpenAICompatibleEmbedder(provider=provider, **spec)

    raise ValueError(
        f"未知の embedding プロバイダ / unknown embedding provider: {provider}\n"
        f"  使えるもの / available: openai, gemini, vllm, lmstudio, ollama, hash"
    )
