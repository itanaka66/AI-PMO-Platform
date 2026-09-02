"""セットアップウィザードのテスト / setup wizard tests.

主眼は、初心者が踏みやすい落とし穴を塞げているかの検証。
Focus: the traps a beginner would actually fall into.
"""
from __future__ import annotations

import os
import stat

import pytest
import yaml

from aipmo.setup_wizard import (
    SetupAnswers,
    SetupError,
    build_config,
    load_env,
    run_interactive,
    validate,
    write_files,
)


# --- validation -----------------------------------------------------------

def test_cloud_mode_requires_api_key():
    with pytest.raises(SetupError):
        validate(SetupAnswers(mode="cloud", api_key=None))


def test_local_mode_does_not_require_api_key():
    answers = validate(SetupAnswers(mode="local", tenant="acme"))
    assert answers.mode == "local"


@pytest.mark.parametrize("tenant", ["A", "a", "Acme Corp", "acme-corp", "株式会社"])
def test_invalid_tenant_rejected(tenant):
    with pytest.raises(SetupError):
        validate(SetupAnswers(mode="local", tenant=tenant))


def test_valid_tenant_accepted():
    assert validate(SetupAnswers(mode="local", tenant="acme_corp")).tenant == "acme_corp"


# --- config generation ----------------------------------------------------

def test_api_key_never_lands_in_config():
    """config.yaml は共有・コミットされる前提。キーが混ざってはいけない。"""
    answers = SetupAnswers(mode="cloud", tenant="acme", api_key="sk-secret-value")
    serialized = yaml.safe_dump(build_config(answers))
    assert "sk-secret-value" not in serialized


def test_cloud_config_uses_openai():
    config = build_config(SetupAnswers(mode="cloud", tenant="acme", api_key="sk-x"))
    assert config["llm"]["default"]["provider"] == "openai"
    assert config["tenant"] == "acme"


def test_local_config_uses_ollama_with_host():
    config = build_config(SetupAnswers(mode="local", tenant="acme",
                                       ollama_host="http://ollama:11434"))
    assert config["llm"]["default"]["provider"] == "ollama"
    assert config["llm"]["default"]["base_url"] == "http://ollama:11434/v1"


def test_data_layer_omitted_by_default():
    """初心者に Postgres/Qdrant を要求しない。既定では mock だけ。"""
    config = build_config(SetupAnswers(mode="cloud", tenant="acme", api_key="sk-x"))
    assert "postgres" not in config["adapters"]
    assert "qdrant" not in config["adapters"]


def test_data_layer_included_when_requested():
    config = build_config(SetupAnswers(mode="cloud", tenant="acme", api_key="sk-x",
                                       use_data_layer=True))
    assert config["adapters"]["postgres"]["queries_file"] == "queries.yaml"
    assert config["adapters"]["qdrant"]["embedding"]["provider"] == "openai"


def test_embedding_provider_follows_llm_mode():
    """埋め込みだけクラウドに残ると、ローカル構成の意味が消える。"""
    config = build_config(SetupAnswers(mode="local", tenant="acme", use_data_layer=True))
    assert config["adapters"]["qdrant"]["embedding"]["provider"] == "ollama"


# --- file writing ---------------------------------------------------------

def test_writes_config_and_env(tmp_path):
    written = write_files(
        SetupAnswers(mode="cloud", tenant="acme", api_key="sk-x"), tmp_path
    )
    assert written["config"].exists()
    assert "OPENAI_API_KEY=sk-x" in written["env"].read_text()


def test_gitignore_covers_env(tmp_path):
    """キーを書いた .env が誤ってコミットされないようにする。"""
    written = write_files(
        SetupAnswers(mode="cloud", tenant="acme", api_key="sk-x"), tmp_path
    )
    assert ".env" in written["gitignore"].read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_env_is_not_world_readable(tmp_path):
    written = write_files(
        SetupAnswers(mode="cloud", tenant="acme", api_key="sk-x"), tmp_path
    )
    mode = stat.S_IMODE(written["env"].stat().st_mode)
    assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0


def test_no_env_written_without_key(tmp_path):
    written = write_files(SetupAnswers(mode="local", tenant="acme"), tmp_path)
    assert "env" not in written
    assert not (tmp_path / ".env").exists()


def test_load_env_does_not_clobber_existing(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from_file\n")
    monkeypatch.setenv("OPENAI_API_KEY", "from_environment")
    load_env(tmp_path)
    assert os.environ["OPENAI_API_KEY"] == "from_environment"


def test_load_env_missing_file_is_harmless(tmp_path):
    load_env(tmp_path)  # 例外を出さない / must not raise


# --- interactive flow -----------------------------------------------------

class FakePrompt:
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else ""


def test_interactive_cloud_flow(tmp_path):
    ask = FakePrompt(["1", "1", "acme_corp", "N"])
    secret = FakePrompt(["sk-test-key"])
    written = run_interactive(tmp_path, ask=ask, ask_secret=secret, out=lambda _: None)

    config = yaml.safe_load(written["config"].read_text())
    assert config["tenant"] == "acme_corp"
    assert config["llm"]["default"]["provider"] == "openai"
    assert "sk-test-key" in written["env"].read_text()


def test_interactive_local_flow_skips_key_prompt(tmp_path):
    ask = FakePrompt(["2", "acme_corp", "N"])
    secret = FakePrompt([])
    run_interactive(tmp_path, ask=ask, ask_secret=secret, out=lambda _: None)

    assert secret.prompts == []  # ローカル構成でキーを聞かない


def test_interactive_declines_overwrite(tmp_path):
    (tmp_path / "config.yaml").write_text("tenant: existing\n")
    ask = FakePrompt(["N"])
    written = run_interactive(tmp_path, ask=ask, ask_secret=FakePrompt([]),
                              out=lambda _: None)

    assert written == {}
    assert "existing" in (tmp_path / "config.yaml").read_text()


def test_interactive_defaults_when_user_just_presses_enter(tmp_path):
    """既定値のまま Enter を連打しても壊れないこと。"""
    ask = FakePrompt(["", "", "", ""])
    secret = FakePrompt(["sk-x"])
    written = run_interactive(tmp_path, ask=ask, ask_secret=secret, out=lambda _: None)

    config = yaml.safe_load(written["config"].read_text())
    assert config["tenant"] == "my_company"
    assert config["llm"]["default"]["provider"] == "openai"
