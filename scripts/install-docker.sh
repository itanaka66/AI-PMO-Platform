#!/usr/bin/env bash
# Docker 構成のインストーラ / Docker deployment installer
#
# PostgreSQL・Ollama・Qdrant はそれぞれ独立に、このマシンに自前で立てるか
# 外部のものに接続するかを対話的に選べる。自前で立てない、と選んだものは
# コンテナも Docker の volume も一切作られない。
#
# For each of PostgreSQL, Ollama, and Qdrant, independently choose whether
# to run it here or connect to an external one. Whichever you choose not
# to self-host gets neither a container nor a Docker volume.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CHAT_MODEL="qwen2.5:14b"
EMBED_MODEL="bge-m3"

step() { printf '\n==> %s\n' "$1"; }
note() { printf '    %s\n' "$1"; }
warn() { printf '\n[!] %s\n' "$1"; }
fail() { printf '\nエラー / Error: %s\n\n' "$1" >&2; exit 1; }

printf '\n  AI-PMO Platform — Docker\n'
printf '  ---------------------------------------------\n'

step "Docker を確認しています / Checking Docker"
command -v docker >/dev/null 2>&1 \
  || fail "Docker が見つかりません / Docker not found: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose v2 が必要です / Docker Compose v2 is required"
docker info >/dev/null 2>&1 \
  || fail "Docker が起動していません / Docker is not running. Start Docker Desktop and retry."
printf '    OK  %s\n' "$(docker --version)"

# --- 接続先の選択 / choosing each target -------------------------------------
#
# 対話端末が無ければ（CI・スクリプト経由の実行など）、全部自前という
# 従来どおりの既定に倒す。
#
# With no interactive terminal (CI, scripted runs), fall back to the
# previous default of self-hosting everything.

POSTGRES_MODE=local
OLLAMA_MODE=local
QDRANT_MODE=local
PG_DSN_VALUE=""
OLLAMA_HOST_VALUE=""
QDRANT_URL_VALUE=""
QDRANT_API_KEY_VALUE=""

if [ -t 0 ]; then
  step "PostgreSQL の接続先 / PostgreSQL target"
  note "1) このマシンに自前で立てる（既定）/ run it on this machine (default)"
  note "2) 外部の PostgreSQL に接続する / connect to an external PostgreSQL"
  read -r -p "    選択 / choice [1]: " choice || choice=""
  if [ "$choice" = "2" ]; then
    POSTGRES_MODE=external
    read -r -p "    接続文字列 / connection string (postgresql://...): " PG_DSN_VALUE
    [ -n "$PG_DSN_VALUE" ] || fail "接続文字列が空です / connection string cannot be empty"
  fi

  step "Ollama の接続先 / Ollama target"
  note "1) このマシンに自前で立てる（既定）/ run it on this machine (default)"
  note "2) 外部の Ollama に接続する / connect to an external Ollama"
  read -r -p "    選択 / choice [1]: " choice || choice=""
  if [ "$choice" = "2" ]; then
    OLLAMA_MODE=external
    read -r -p "    URL（例 / e.g. http://your-host:11434）: " OLLAMA_HOST_VALUE
    [ -n "$OLLAMA_HOST_VALUE" ] || fail "URL が空です / URL cannot be empty"
  fi

  step "Qdrant の接続先 / Qdrant target（ナレッジ機能・任意 / knowledge features, optional）"
  note "1) このマシンに自前で立てる（既定）/ run it on this machine (default)"
  note "2) 外部の Qdrant（Qdrant Cloud 等）に接続する / connect to an external Qdrant"
  note "3) 使わない / do not use it"
  read -r -p "    選択 / choice [1]: " choice || choice=""
  case "$choice" in
    2)
      QDRANT_MODE=external
      read -r -p "    URL: " QDRANT_URL_VALUE
      [ -n "$QDRANT_URL_VALUE" ] || fail "URL が空です / URL cannot be empty"
      read -r -p "    API key（無ければ空エンター / leave empty if none）: " QDRANT_API_KEY_VALUE || true
      ;;
    3)
      QDRANT_MODE=skip
      ;;
  esac
else
  note "非対話環境のため既定値を使います（PostgreSQL・Ollama・Qdrant を"
  note "すべて自前で立てる）/ non-interactive: using defaults (self-hosting"
  note "PostgreSQL, Ollama, and Qdrant all)"
fi

# --- .env の生成 / writing .env ----------------------------------------------

step ".env を書いています / Writing .env"

if [ "$POSTGRES_MODE" = external ]; then
  PG_DSN="$PG_DSN_VALUE"
else
  PG_DSN="postgresql://aipmo:aipmo@postgres:5432/aipmo"
fi

if [ "$OLLAMA_MODE" = external ]; then
  OLLAMA_HOST_OUT="$OLLAMA_HOST_VALUE"
else
  OLLAMA_HOST_OUT="http://ollama:11434"
fi

case "$QDRANT_MODE" in
  external) QDRANT_URL_OUT="$QDRANT_URL_VALUE"; QDRANT_KEY_OUT="$QDRANT_API_KEY_VALUE" ;;
  local)    QDRANT_URL_OUT="http://qdrant:6333"; QDRANT_KEY_OUT="" ;;
  skip)     QDRANT_URL_OUT=""; QDRANT_KEY_OUT="" ;;
esac

cat > .env <<ENVFILE
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-aipmo}
AIPMO_PG_DSN=${PG_DSN}
OLLAMA_HOST=${OLLAMA_HOST_OUT}
QDRANT_URL=${QDRANT_URL_OUT}
QDRANT_API_KEY=${QDRANT_KEY_OUT}
ENVFILE
note "書き込み先 / written to: $(pwd)/.env"

# --- どのコンテナを自前で立てるか / which containers to self-host -----------

# 空配列を set -u 下で展開すると、古い bash（macOS 既定の 3.2 系など）で
# 「unbound variable」になる。配列でなく文字列で組み立てて回避する。
#
# Expanding an empty array under set -u breaks on older bash (macOS still
# ships 3.2 by default) — building this as a plain string sidesteps it.
PROFILE_ARGS=""
[ "$POSTGRES_MODE" = local ] && PROFILE_ARGS="$PROFILE_ARGS --profile postgres"
[ "$OLLAMA_MODE"   = local ] && PROFILE_ARGS="$PROFILE_ARGS --profile ollama"
[ "$QDRANT_MODE"   = local ] && PROFILE_ARGS="$PROFILE_ARGS --profile qdrant"

step "コンテナを起動しています / Starting the containers"
printf '    初回はイメージ取得に時間がかかります / First run downloads images.\n'
# shellcheck disable=SC2086
docker compose $PROFILE_ARGS up -d

if [ "$OLLAMA_MODE" = local ]; then
  step "モデルを取得しています / Pulling models"
  printf '    数 GB のダウンロードです / This downloads several GB.\n'
  docker compose exec -T ollama ollama pull "$CHAT_MODEL"
  docker compose exec -T ollama ollama pull "$EMBED_MODEL"
else
  note "外部 Ollama を使うため、モデル取得は行いません / using an external"
  note "Ollama: skipping model pulls. そちら側で $CHAT_MODEL / $EMBED_MODEL"
  note "を用意しておいてください / make sure those models are available there."
fi

step "アプリをビルドしています / Building the application"
docker compose build aipmo

step "接続を確認しています / Checking connections"
docker compose run --rm aipmo doctor \
  || warn "一部のアダプタに接続できませんでした。上の出力と .env を確認して
    ください / some adapters could not connect — check the output above
    and .env."

step "動作確認 / Smoke test"
docker compose run --rm aipmo validate templates/examples/meeting_minutes.yaml

printf '\n  完了しました / Done.\n\n'
printf '  使い方 / Usage:\n'
printf '    docker compose run --rm aipmo doctor\n'
printf '    docker compose run --rm aipmo run templates/examples/meeting_minutes.yaml\n\n'
