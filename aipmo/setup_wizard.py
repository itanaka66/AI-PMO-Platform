"""初回セットアップウィザード / First-run setup wizard.

対話部分と設定生成を分けてある。生成側は純関数なのでテストできる。
Prompting and config generation are separated; the generator is a pure
function and therefore testable.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .i18n import LANGUAGES, translator

TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

# クラウド LLM の既定 / defaults for the cloud profile
CLOUD_CHAT_MODEL = "gpt-4o-mini"
CLOUD_EMBED_MODEL = "text-embedding-3-small"
CLOUD_EMBED_DIM = 1536

# ローカル LLM の既定 / defaults for the local profile
LOCAL_CHAT_MODEL = "qwen2.5:14b"
LOCAL_EMBED_MODEL = "bge-m3"
LOCAL_EMBED_DIM = 1024


@dataclass
class SetupAnswers:
    """ウィザードの回答 / the answers a user gives."""

    mode: str = "cloud"          # cloud | local
    provider: str = "openai"     # cloud のとき: openai | gemini | groq | openrouter | claude
    chat_model: str | None = None
    tenant: str = "my_company"
    use_data_layer: bool = False
    api_key: str | None = None
    ollama_host: str = "http://localhost:11434"
    postgres_dsn: str = "postgresql://aipmo:aipmo@localhost:5432/aipmo"
    qdrant_url: str = "http://localhost:6333"
    warnings: list[str] = field(default_factory=list)


class SetupError(Exception):
    pass


def validate(answers: SetupAnswers, lang: str | None = None) -> SetupAnswers:
    t = translator(lang)
    if answers.mode not in ("cloud", "local"):
        raise SetupError(f"{t('err_mode')}: {answers.mode!r}")
    if not TENANT_RE.match(answers.tenant):
        raise SetupError(t("err_tenant"))
    if answers.mode == "cloud" and not answers.api_key:
        raise SetupError(t("err_key"))
    if answers.mode == "cloud":
        from .llm.presets import ProviderError, resolve

        try:
            resolve(answers.provider)
        except ProviderError as exc:
            raise SetupError(str(exc)) from None
    return answers


def build_config(answers: SetupAnswers) -> dict[str, Any]:
    """設定辞書を組み立てる。API キーは含めない。

    Builds the config mapping. The API key is deliberately absent — it goes to
    .env, so that config.yaml stays safe to share, commit, or paste into a
    support thread.
    """
    if answers.mode == "cloud":
        from .llm.presets import resolve

        preset = resolve(answers.provider)
        model = answers.chat_model or preset.default_chat_model
        llm = {
            "default": {"provider": preset.name, "model": model},
            "fast": {"provider": preset.name, "model": model},
        }

        if preset.supports_embeddings:
            embedding = {
                "provider": preset.name,
                "model": preset.default_embed_model,
                "dimension": preset.default_embed_dimension,
            }
        else:
            # Groq と OpenRouter に埋め込み API は無い。
            # チャットの提供元をそのまま流用すると、ベクトル検索が壊れる。
            # OpenAI 側に逃がし、鍵が別に要ることを警告として残す。
            # Groq and OpenRouter have no embeddings API. Reusing the chat
            # provider here would break vector search, so this falls back to
            # OpenAI and records that a second key is needed.
            embedding = {
                "provider": "openai",
                "model": CLOUD_EMBED_MODEL,
                "dimension": CLOUD_EMBED_DIM,
            }
            answers.warnings.append(
                f"{preset.name} に埋め込み API が無いため、埋め込みは OpenAI を使います。"
                f" OPENAI_API_KEY も設定してください "
                f"/ {preset.name} has no embeddings API, so embeddings use OpenAI;"
                f" set OPENAI_API_KEY as well."
            )
    else:
        base_url = answers.ollama_host
        if not base_url.endswith("/v1") and not base_url.endswith("/v1/"):
            base_url = base_url.rstrip("/") + "/v1"
        llm = {
            "default": {"provider": "ollama", "model": LOCAL_CHAT_MODEL,
                        "base_url": base_url},
            "fast": {"provider": "ollama", "model": LOCAL_CHAT_MODEL,
                     "base_url": base_url},
        }
        embedding = {
            "provider": "ollama",
            "model": LOCAL_EMBED_MODEL,
            "dimension": LOCAL_EMBED_DIM,
            "base_url": base_url,
        }

    config: dict[str, Any] = {
        "tenant": answers.tenant,
        "llm": llm,
        "prompts_dir": "prompts",
        "adapters": {"mode": "mock"},
    }

    if answers.use_data_layer:
        config["adapters"]["postgres"] = {
            "dsn": answers.postgres_dsn,
            "queries_file": "queries.yaml",
        }
        config["adapters"]["qdrant"] = {
            "url": answers.qdrant_url,
            "public_collection": "public_pmo_knowledge",
            "embedding": embedding,
        }

    return config


def write_files(answers: SetupAnswers, target: Path) -> dict[str, Path]:
    """config.yaml と .env を書き出す / write config.yaml and .env."""
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    config_path = target / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(build_config(answers), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    written["config"] = config_path

    if answers.api_key:
        from .llm.presets import PRESETS

        preset = PRESETS.get(answers.provider)
        # 鍵の変数名は提供元ごとに違う。OPENAI_API_KEY に Gemini の鍵を
        # 入れても動かないので、正しい名前で書き出す。
        # Each provider reads a different variable; writing a Gemini key into
        # OPENAI_API_KEY would simply not work.
        var = (preset.api_key_env if preset and preset.api_key_env
               else "OPENAI_API_KEY")
        env_path = target / ".env"
        env_path.write_text(f"{var}={answers.api_key}\n", encoding="utf-8")
        _restrict_permissions(env_path)
        written["env"] = env_path

    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".env\n__pycache__/\n*.egg-info/\n", encoding="utf-8")
        written["gitignore"] = gitignore

    return written


def _restrict_permissions(path: Path, lang: str | None = None) -> None:
    """.env を本人のみ読み書き可にする。Windows では NTFS ACL を設定する。

    Restrict .env to the current user. On POSIX this is chmod 600; on Windows
    it is an icacls ACL reset, because chmod does not restrict anything there.
    """
    if os.name != "nt":
        path.chmod(0o600)
        return

    import getpass
    import subprocess

    user = os.environ.get("USERNAME") or getpass.getuser()
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r"],
                       check=True, capture_output=True)
        subprocess.run(["icacls", str(path), "/grant:r", f"{user}:F"],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        # 失敗しても続行するが、黙って通さない
        # Continue on failure, but never silently.
        raise SetupError(translator(lang)("err_perms"))


def load_env(target: Path) -> None:
    """.env を環境変数に読み込む / load .env into the process environment."""
    env_path = target / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# -- 対話 / interaction ----------------------------------------------------

def run_interactive(
    target: Path,
    ask: Callable[[str], str] | None = None,
    ask_secret: Callable[[str], str] | None = None,
    out: Callable[[str], None] = print,
    lang: str | None = None,
) -> dict[str, Path]:
    import getpass

    ask = ask or input
    ask_secret = ask_secret or getpass.getpass
    code = lang or detect_language()
    t = translator(code)

    out("")
    out(f"{t('title')}  [{LANGUAGES.get(code, code)}]")
    out("=" * 46)
    out(t("intro"))
    out("")

    if (target / "config.yaml").exists():
        if ask(t("overwrite")).strip().lower() not in ("y", "yes"):
            out(t("cancelled"))
            return {}

    out(t("q_mode"))
    out("   " + t("mode_cloud"))
    out("   " + t("mode_local"))
    mode = "local" if ask(t("choose")).strip() == "2" else "cloud"

    api_key = None
    provider = "openai"
    if mode == "cloud":
        from .llm.presets import PRESETS

        out("")
        out(t("q_provider"))
        choices = ["openai", "gemini", "groq", "openrouter", "claude"]
        for index, name in enumerate(choices, start=1):
            preset = PRESETS[name]
            suffix = "" if preset.supports_embeddings else t("no_embeddings")
            out(f"   [{index}] {name}{suffix}")
        picked = ask(t("choose")).strip()
        provider = choices[int(picked) - 1] if picked.isdigit() and \
            1 <= int(picked) <= len(choices) else "openai"

        preset = PRESETS[provider]
        out("")
        out(t("q_key").replace("OpenAI", provider))
        out(t("key_hidden"))
        out(f"   {preset.api_key_env}")
        api_key = ask_secret(t("key_prompt")).strip() or None

    out("")
    out(t("q_tenant"))
    out(t("tenant_rule"))
    tenant = ask(t("tenant_prompt")).strip() or "my_company"

    out("")
    out(t("q_data"))
    out(t("data_note"))
    use_data = ask(t("data_prompt")).strip().lower() in ("y", "yes")

    answers = validate(SetupAnswers(
        mode=mode, provider=provider, tenant=tenant,
        use_data_layer=use_data, api_key=api_key,
    ), lang=code)
    written = write_files(answers, target)

    out("")
    out(t("done"))
    for label, path in written.items():
        out(f"  {label}: {path}")
    if api_key:
        out("")
        out(t("key_stored"))
    for warning in answers.warnings:
        out("")
        out(f"! {warning}")

    out("")
    out(t("next_try"))
    out("  aipmo validate templates/examples/meeting_minutes.yaml")
    out("")
    if mode == "local":
        out(t("pull_models"))
        out(f"  ollama pull {LOCAL_CHAT_MODEL}")
        out(f"  ollama pull {LOCAL_EMBED_MODEL}")
        out("")
    return written


def detect_language() -> str:
    """言語を推定する / infer the display language."""
    from .i18n import detect

    return detect()
