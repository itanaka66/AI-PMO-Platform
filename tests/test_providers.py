"""提供元プリセットのテスト / provider preset tests.

主眼は、乗り換えたときに黙って壊れる箇所を先に落とすこと。
Focus: the places where switching provider breaks quietly.
"""
from __future__ import annotations

import pytest

from aipmo.llm.base import LLMRequest, OpenAICompatibleProvider
from aipmo.llm.embeddings import OllamaEmbedder, build_embedder
from aipmo.llm.presets import PRESETS, ProviderError, require_embeddings, resolve
from aipmo.llm.registry import LLMRegistry, build_provider


# --- プリセットの健全性 / preset sanity ------------------------------------

@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_resolves(name):
    assert resolve(name).name == name


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_remote_presets_name_a_key_variable(name):
    """鍵の在り処が決まっていないと、利用者はどこに置くか分からない。"""
    preset = PRESETS[name]
    if not preset.local:
        assert preset.api_key_env, f"{name} に api_key_env がありません"


@pytest.mark.parametrize("name", ["gemini", "groq", "openrouter"])
def test_hosted_presets_carry_a_base_url(name):
    assert PRESETS[name].base_url.startswith("https://")


def test_unknown_provider_lists_the_alternatives():
    """名前を間違えたとき、正解の候補が出ること。"""
    with pytest.raises(ProviderError, match="available"):
        resolve("gemeni")


# --- 埋め込みを持たない提供元 / providers without embeddings ---------------

@pytest.mark.parametrize("name", ["groq", "openrouter"])
def test_embedding_only_providers_are_refused_early(name):
    """Groq と OpenRouter に埋め込みは無い。設定を読む段階で落とす。

    実行時まで通してしまうと、ベクトル検索を有効にした瞬間に、
    原因の分かりにくい失敗になる。
    """
    with pytest.raises(ProviderError, match="embeddings API"):
        require_embeddings(name)


@pytest.mark.parametrize("name", ["groq", "openrouter"])
def test_build_embedder_refuses_them_too(name):
    with pytest.raises(ProviderError):
        build_embedder({"provider": name})


def test_refusal_suggests_a_working_alternative():
    with pytest.raises(ProviderError, match="openai"):
        require_embeddings("groq")


@pytest.mark.parametrize("name", ["openai", "gemini"])
def test_providers_with_embeddings_are_accepted(name):
    assert require_embeddings(name).supports_embeddings


def test_groq_chat_still_works_alongside_another_embedder(monkeypatch):
    """チャットは Groq、埋め込みは別、という組み合わせが成立すること。

    これが実務上いちばん現実的な構成なので、成立させておく必要がある。
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    chat = build_provider({"provider": "groq"})
    embedder = build_embedder({"provider": "ollama", "model": "bge-m3"})

    assert chat.preset.name == "groq"
    assert isinstance(embedder, OllamaEmbedder)


# --- JSON モードの差 / JSON-mode differences --------------------------------

def test_json_mode_is_sent_where_supported(monkeypatch):
    sent = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            sent.update(kwargs)
            return _reply("{}")

    _install(monkeypatch, FakeClient)
    provider = OpenAICompatibleProvider(provider="openai", api_key="x")
    provider.complete(LLMRequest(prompt="p", json_mode=True))

    assert sent["response_format"] == {"type": "json_object"}


def test_json_mode_falls_back_to_the_prompt_where_unsupported(monkeypatch):
    """非対応の相手に response_format を送ると 400 を返す提供元がある。

    黙って落とさず、プロンプト側で JSON を要求する。
    """
    sent = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            sent.update(kwargs)
            return _reply("{}")

    _install(monkeypatch, FakeClient)
    provider = OpenAICompatibleProvider(provider="openrouter", api_key="x")
    provider.complete(LLMRequest(prompt="p", json_mode=True))

    assert "response_format" not in sent
    assert "JSON" in sent["messages"][0]["content"]


def test_existing_system_prompt_is_preserved_when_json_is_appended(monkeypatch):
    sent = {}

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            sent.update(kwargs)
            return _reply("{}")

    _install(monkeypatch, FakeClient)
    provider = OpenAICompatibleProvider(provider="openrouter", api_key="x")
    provider.complete(LLMRequest(prompt="p", system="あなたはPMOです", json_mode=True))

    system = sent["messages"][0]["content"]
    assert "あなたはPMOです" in system and "JSON" in system


# --- モデル名と鍵 / models and keys ----------------------------------------

def test_local_provider_requires_an_explicit_model():
    """ローカルは何を載せたかで名前が変わる。既定を勝手に決めない。"""
    with pytest.raises(ProviderError, match="model"):
        OpenAICompatibleProvider(provider="vllm")


def test_local_provider_needs_no_api_key():
    provider = OpenAICompatibleProvider(provider="lmstudio", model="local-model")
    assert provider._api_key == "not-needed"
    assert provider.base_url.startswith("http://localhost")


def test_api_key_is_read_from_the_provider_specific_variable(monkeypatch):
    """OPENAI_API_KEY が設定されていても、Gemini はそれを使わないこと。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    provider = OpenAICompatibleProvider(provider="gemini")
    assert provider._api_key == "gemini-key"


def test_base_url_can_be_overridden(monkeypatch):
    """社内ゲートウェイ経由に差し替えられること。"""
    monkeypatch.setenv("GROQ_API_KEY", "x")
    provider = OpenAICompatibleProvider(provider="groq",
                                        base_url="http://gateway.internal/v1")
    assert provider.base_url == "http://gateway.internal/v1"


# --- レジストリ / registry --------------------------------------------------

def test_registry_builds_mixed_providers(monkeypatch):
    """profile ごとに別の提供元を割り当てられること。

    テンプレートは profile 名しか書かないので、これが乗り換えの単位になる。
    """
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    registry = LLMRegistry.from_config({
        "default": {"provider": "gemini"},
        "fast": {"provider": "ollama", "model": "qwen2.5:7b"},
    })

    assert registry.get("default").preset.name == "gemini"
    assert registry.get("fast").name == "ollama"


def test_shorthand_provider_string(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert build_provider("groq").preset.name == "groq"


def test_unknown_provider_in_config_is_rejected():
    with pytest.raises(ProviderError):
        LLMRegistry.from_config({"default": {"provider": "nope"}})


# --- helpers ---------------------------------------------------------------

def _reply(text: str):
    class Message:
        content = text

    class Choice:
        message = Message()

    class Result:
        choices = [Choice()]
        usage = None

    return Result()


def _install(monkeypatch, client_class) -> None:
    """openai.OpenAI を差し替える / swap out openai.OpenAI."""
    import sys
    import types

    module = types.ModuleType("openai")
    module.OpenAI = client_class
    monkeypatch.setitem(sys.modules, "openai", module)


# --- ウィザードとの結線 / wizard integration --------------------------------

def test_wizard_writes_the_right_key_variable(tmp_path):
    """Gemini の鍵を OPENAI_API_KEY に書いても動かない。"""
    from aipmo.setup_wizard import SetupAnswers, write_files

    written = write_files(
        SetupAnswers(mode="cloud", provider="gemini", tenant="acme",
                     api_key="gemini-key"),
        tmp_path,
    )
    assert "GEMINI_API_KEY=gemini-key" in written["env"].read_text()
    assert "OPENAI_API_KEY" not in written["env"].read_text()


def test_wizard_config_uses_the_chosen_provider():
    from aipmo.setup_wizard import SetupAnswers, build_config

    config = build_config(SetupAnswers(mode="cloud", provider="groq",
                                       tenant="acme", api_key="x"))
    assert config["llm"]["default"]["provider"] == "groq"


def test_wizard_routes_embeddings_away_from_groq_and_warns():
    """チャットが Groq でも、埋め込みは動く提供元に向ける。

    ここで黙って groq を書くと、ベクトル検索が起動時に落ちる。
    """
    from aipmo.setup_wizard import SetupAnswers, build_config

    answers = SetupAnswers(mode="cloud", provider="groq", tenant="acme",
                           api_key="x", use_data_layer=True)
    config = build_config(answers)

    assert config["adapters"]["qdrant"]["embedding"]["provider"] == "openai"
    assert any("OPENAI_API_KEY" in w for w in answers.warnings)


def test_wizard_rejects_an_unknown_provider():
    from aipmo.setup_wizard import SetupAnswers, SetupError, validate

    with pytest.raises(SetupError):
        validate(SetupAnswers(mode="cloud", provider="gemeni",
                              tenant="acme", api_key="x"))
