"""LLM 提供元のプリセット / provider presets.

Gemini・Groq・OpenRouter・vLLM・LM Studio・llama.cpp は、いずれも
OpenAI 互換のチャット API を出している。個別のクラスを4つ書くのではなく、
「base_url と API キーの在り処と、対応している機能」の違いとして扱う。

Gemini, Groq, OpenRouter, vLLM, LM Studio and llama.cpp all expose an
OpenAI-compatible chat API. Rather than four near-identical classes, the
differences are captured as data: base URL, where the key lives, and which
features the endpoint actually supports.

Ollama だけは独自 API のまま別に持っている（llm/base.py）。
ローカルで最も使われる経路で、互換層を挟まない方が素直なため。
Ollama keeps its native client in llm/base.py: it is the most common local
path and going through a compatibility shim buys nothing there.

モデル名について / About model names
------------------------------------
モデル名は寿命が短い。Groq は 2026年8月16日に Llama 系を停止した。
ここの既定値は執筆時点のもので、動かなくなったら設定で上書きする前提。
既定に固執させないため、エラーメッセージで「モデル名を確認せよ」と言う。

Model identifiers are short-lived — Groq shut down its Llama models on
16 August 2026. The defaults here are a snapshot; when one stops working it is
meant to be overridden in config, and the error message says so.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    """提供元ごとの差分 / what differs between providers."""

    name: str
    base_url: str | None
    api_key_env: str | None
    default_chat_model: str
    default_embed_model: str | None = None
    default_embed_dimension: int | None = None

    # response_format={"type":"json_object"} を受け付けるか。
    # 受け付けない相手に送ると 400 で落ちる提供元があるので、
    # 対応可否を明示して、非対応ならプロンプト側で JSON を要求する。
    # Whether the endpoint accepts response_format. Some providers return 400
    # for unsupported fields rather than ignoring them, so this is explicit;
    # where it is false, JSON is requested in the prompt instead.
    supports_json_mode: bool = True

    # 埋め込み API を持っているか。
    # Groq と OpenRouter は持っていない。ここを黙って通すと、
    # ベクトル検索を有効にした瞬間に実行時エラーになる。
    # Whether an embeddings API exists. Groq and OpenRouter have none; letting
    # that through silently turns into a runtime failure the moment vector
    # search is switched on.
    supports_embeddings: bool = True

    # ツール呼び出し (function calling) に対応しているか。
    # エージェントはこれが無いと成立しない。非対応の相手に tools を送ると、
    # 無視されるか 400 になる。どちらも「動いていないのに気づかない」形なので、
    # 設定を読む段階で分かるようにする。
    # Whether the endpoint supports tool calling. An agent cannot work without
    # it. Sending tools to an endpoint that lacks it is either ignored or
    # answered with a 400 — both failure modes look like nothing happening, so
    # this is surfaced while the config is read.
    supports_tools: bool = True

    # ローカル実行か。鍵が要らない相手に鍵を要求しないための区別。
    # Local endpoints need no credential; this avoids demanding one.
    local: bool = False

    notes: str = ""


PRESETS: dict[str, Preset] = {
    "openai": Preset(
        name="openai",
        base_url=None,                     # SDK の既定 / the SDK default
        api_key_env="OPENAI_API_KEY",
        default_chat_model="gpt-4o-mini",
        default_embed_model="text-embedding-3-small",
        default_embed_dimension=1536,
    ),
    "gemini": Preset(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        default_chat_model="gemini-3.5-flash",
        default_embed_model="gemini-embedding-001",
        default_embed_dimension=1536,
        notes="Google AI Studio で鍵を取得 / get a key at aistudio.google.com",
    ),
    "groq": Preset(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_chat_model="openai/gpt-oss-120b",
        # Groq は埋め込み API を提供していない。
        # 埋め込みだけ別の提供元に分ける必要がある。
        # Groq has no embeddings API; embeddings must come from elsewhere.
        supports_embeddings=False,
        notes="非対応フィールドを送ると 400 を返す / rejects unsupported fields with 400",
    ),
    "openrouter": Preset(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_chat_model="openai/gpt-4o-mini",
        supports_embeddings=False,
        # JSON モードの可否はモデル依存。既定では送らず、
        # プロンプトで JSON を要求して寛容に解析する方が失敗が少ない。
        # JSON-mode support depends on the routed model, so it is not sent by
        # default; asking for JSON in the prompt and parsing leniently fails
        # less often than a 400 from an unlucky route.
        supports_json_mode=False,
        notes="モデルは provider/model 形式 / models are named provider/model",
    ),
    "vllm": Preset(
        name="vllm",
        base_url="http://localhost:8000/v1",
        api_key_env=None,
        default_chat_model="",             # 起動時に載せたモデル名 / whatever you served
        default_embed_model="",
        local=True,
        notes="--served-model-name の値をそのまま指定 / use your --served-model-name",
    ),
    "lmstudio": Preset(
        name="lmstudio",
        base_url="http://localhost:1234/v1",
        api_key_env=None,
        default_chat_model="",
        default_embed_model="",
        local=True,
        notes="LM Studio の Local Server を有効にする / enable the local server first",
    ),
    "llamacpp": Preset(
        name="llamacpp",
        base_url="http://localhost:8080/v1",
        api_key_env=None,
        default_chat_model="",
        default_embed_model="",
        supports_json_mode=False,
        # ツール対応はビルドとモデル次第。既定では当てにしない。
        # Tool support depends on the build and the model; not assumed here.
        supports_tools=False,
        local=True,
        notes="llama-server で起動 / start with llama-server",
    ),
}


class ProviderError(Exception):
    pass


def resolve(name: str) -> Preset:
    if name not in PRESETS:
        raise ProviderError(
            f"未知の提供元 / unknown provider: {name!r}\n"
            f"  使えるもの / available: {', '.join(sorted(PRESETS))}, ollama"
        )
    return PRESETS[name]


def require_tools(name: str) -> Preset:
    """エージェントに使えるかを設定読み込み時に確かめる。

    Verified while the config is read, so an agent step never silently
    degrades into a model that ignores its tools.
    """
    preset = resolve(name)
    if not preset.supports_tools:
        raise ProviderError(
            f"{preset.name} はツール呼び出しに対応していません "
            f"/ {preset.name} does not support tool calling.\n"
            f"  エージェントには別のプロファイルを割り当ててください "
            f"/ point the agent at a different profile:\n"
            f"    llm: {{ profile: agent }}   # openai / gemini / groq / ollama"
        )
    return preset


def require_embeddings(name: str) -> Preset:
    """埋め込みに使えるかを設定読み込み時に確かめる。

    実行中のテンプレートの奥で落ちるより、起動時に落ちた方がよい。
    Fails at configuration time rather than deep inside a running template.
    """
    preset = resolve(name)
    if not preset.supports_embeddings:
        raise ProviderError(
            f"{preset.name} は埋め込み API を持っていません "
            f"/ {preset.name} has no embeddings API.\n"
            f"  埋め込みだけ別の提供元にしてください "
            f"/ point the embedding at a different provider:\n"
            f"    embedding: {{ provider: openai }}   # または gemini / ollama"
        )
    return preset
