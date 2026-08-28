"""セットアップウィザードのテスト / setup wizard tests."""
from __future__ import annotations

import os
import stat

import pytest
import yaml

from aipmo.cli import ConfigError, build_engine
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


def test_local_mode_needs_no_api_key():
    validate(SetupAnswers(mode="local", api_key=None))


@pytest.mark.parametrize("tenant", ["A", "1company", "my company", "x", "my-company", ""])
def test_bad_tenant_names_rejected(tenant):
    with pytest.raises(SetupError):
        validate(SetupAnswers(mode="local", tenant=tenant))


def test_good_tenant_name_accepted():
    validate(SetupAnswers(mode="local", tenant="acme_corp"))


# --- config generation ----------------------------------------------------

def test_api_key_never_lands_in_config():
    """config.yaml は共有されうるので、鍵を書かない。"""
    config = build_config(SetupAnswers(mode="cloud", api_key="sk-secret-value"))
    assert "sk-secret-value" not in yaml.safe_dump(config)


def test_cloud_config_uses_openai():
    config = build_config(SetupAnswers(mode="cloud", api_key="sk-x"))
    assert config["llm"]["default"]["provider"] == "openai"


def test_local_config_uses_ollama_with_host():
    config = build_config(SetupAnswers(
        mode="local", ollama_host="http://ollama:11434", use_data_layer=True,
    ))
    assert config["llm"]["default"]["provider"] == "ollama"
    assert config["llm"]["default"]["host"] == "http://ollama:11434"
    assert config["adapters"]["qdrant"]["embedding"]["provider"] == "ollama"


def test_data_layer_omitted_when_not_requested():
    config = build_config(SetupAnswers(mode="local", use_data_layer=False))
    assert "postgres" not in config["adapters"]
    assert "qdrant" not in config["adapters"]


def test_generated_config_actually_builds_an_engine(tmp_path):
    """ウィザードの出力がエンジンに読み込めることまで確認する。

    設定を生成できても engine が組めなければ意味がない。
    """
    write_files(
        SetupAnswers(mode="local", tenant="acme_corp", use_data_layer=True), tmp_path
    )
    (tmp_path / "queries.yaml").write_text("noop: SELECT 1\n", encoding="utf-8")

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    engine = build_engine(config, tmp_path)

    assert "postgres" in engine.adapters.names()
    assert "qdrant" in engine.adapters.names()
    assert engine.adapters.get("qdrant").tenant == "acme_corp"


def test_missing_query_file_gives_readable_error(tmp_path):
    """初心者に生の traceback を見せない / no raw traceback for beginners."""
    write_files(
        SetupAnswers(mode="local", tenant="acme_corp", use_data_layer=True), tmp_path
    )
    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))

    with pytest.raises(ConfigError, match="queries_file"):
        build_engine(config, tmp_path)


def test_relative_paths_resolve_against_config_not_cwd(tmp_path, monkeypatch):
    """ショートカット起動で作業ディレクトリが変わっても壊れない。"""
    write_files(
        SetupAnswers(mode="local", tenant="acme_corp", use_data_layer=True), tmp_path
    )
    (tmp_path / "queries.yaml").write_text("noop: SELECT 1\n", encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    engine = build_engine(config, tmp_path)
    assert "postgres" in engine.adapters.names()


# --- file writing ---------------------------------------------------------

def test_env_file_is_not_world_readable(tmp_path):
    written = write_files(
        SetupAnswers(mode="cloud", api_key="sk-x", tenant="acme_corp"), tmp_path
    )
    mode = written["env"].stat().st_mode
    if os.name != "nt":
        assert not mode & stat.S_IRGRP
        assert not mode & stat.S_IROTH


def test_gitignore_covers_env(tmp_path):
    write_files(SetupAnswers(mode="cloud", api_key="sk-x"), tmp_path)
    assert ".env" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_existing_gitignore_not_clobbered(tmp_path):
    (tmp_path / ".gitignore").write_text("custom-entry\n", encoding="utf-8")
    write_files(SetupAnswers(mode="cloud", api_key="sk-x"), tmp_path)
    assert "custom-entry" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "already-set")
    write_files(SetupAnswers(mode="cloud", api_key="sk-from-file"), tmp_path)
    load_env(tmp_path)
    assert os.environ["OPENAI_API_KEY"] == "already-set"


# --- interaction ----------------------------------------------------------

def scripted(answers: list[str]):
    queue = list(answers)
    return lambda _prompt: queue.pop(0)


def test_interactive_cloud_path(tmp_path):
    written = run_interactive(
        tmp_path,
        ask=scripted(["1", "1", "acme_corp", "n"]),
        ask_secret=scripted(["sk-test-key"]),
        out=lambda _: None,
    )
    config = yaml.safe_load(written["config"].read_text(encoding="utf-8"))
    assert config["tenant"] == "acme_corp"
    assert config["llm"]["default"]["provider"] == "openai"
    assert "sk-test-key" in written["env"].read_text(encoding="utf-8")


def test_interactive_local_path_skips_key_prompt(tmp_path):
    def no_secret(_prompt):
        raise AssertionError("ローカルモードで API キーを尋ねてはいけない")

    written = run_interactive(
        tmp_path,
        ask=scripted(["2", "acme_corp", "y"]),
        ask_secret=no_secret,
        out=lambda _: None,
    )
    config = yaml.safe_load(written["config"].read_text(encoding="utf-8"))
    assert config["llm"]["default"]["provider"] == "ollama"
    assert "postgres" in config["adapters"]
    assert "env" not in written


def test_interactive_declines_overwrite(tmp_path):
    (tmp_path / "config.yaml").write_text("tenant: original\n", encoding="utf-8")
    written = run_interactive(
        tmp_path, ask=scripted(["n"]), ask_secret=scripted([]), out=lambda _: None,
    )
    assert written == {}
    assert "original" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_interactive_defaults_on_empty_input(tmp_path):
    """初心者が Enter を連打しても壊れない / mashing Enter must not break."""
    written = run_interactive(
        tmp_path,
        ask=scripted(["", "", "", ""]),
        ask_secret=scripted(["sk-x"]),
        out=lambda _: None,
    )
    config = yaml.safe_load(written["config"].read_text(encoding="utf-8"))
    assert config["tenant"] == "my_company"
    assert config["llm"]["default"]["provider"] == "openai"


# --- 設定内の環境変数展開 / environment expansion in config ----------------

def test_env_reference_is_expanded(monkeypatch):
    """資格情報を config.yaml に書かせないための仕組み。"""
    from aipmo.cli import expand_env

    monkeypatch.setenv("AIPMO_PG_DSN", "postgresql://real/db")
    config = expand_env({"adapters": {"postgres": {"dsn": "${AIPMO_PG_DSN}"}}})
    assert config["adapters"]["postgres"]["dsn"] == "postgresql://real/db"


def test_default_value_is_used_when_unset(monkeypatch):
    from aipmo.cli import expand_env

    monkeypatch.delenv("AIPMO_PORT", raising=False)
    assert expand_env("${AIPMO_PORT:-8765}") == "8765"


def test_undefined_variable_is_left_intact(monkeypatch):
    """空文字に潰すと、壊れた DSN で接続を試みて原因が追いにくくなる。

    Blanking it would produce a malformed DSN and an error that points
    anywhere but at the missing variable.
    """
    from aipmo.cli import expand_env

    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    assert expand_env("${NOT_SET_ANYWHERE}") == "${NOT_SET_ANYWHERE}"


def test_expansion_reaches_nested_lists(monkeypatch):
    from aipmo.cli import expand_env

    monkeypatch.setenv("HOST_A", "10.0.0.1")
    assert expand_env({"hosts": ["${HOST_A}", "static"]})["hosts"] == ["10.0.0.1", "static"]
