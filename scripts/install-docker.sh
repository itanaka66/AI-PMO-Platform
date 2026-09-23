#!/usr/bin/env bash
# Docker 構成のインストーラ / Docker deployment installer
#
# PostgreSQL・Ollama・Qdrant はそれぞれ独立に、このマシンに自前で立てるか
# 外部のものに接続するかを選べる。自前で立てない、と選んだものはコンテナも
# Docker の volume も一切作られない。CLI 引数で指定すれば非対話で実行でき、
# 引数を渡さなければ対話端末で質問する（従来どおり）。
#
# For each of PostgreSQL, Ollama, and Qdrant, independently choose whether
# to run it here or connect to an external one. Whichever you choose not to
# self-host gets neither a container nor a Docker volume. Pass CLI flags to
# run non-interactively; omit them to be asked interactively (as before).
#
# 使い方 / Usage:
#   ./scripts/install-docker.sh [options]
#
#   --postgres local|external          既定 local / default local
#   --postgres-dsn DSN                 --postgres external のとき必須
#                                       required with --postgres external
#   --ollama local|external            既定 local / default local
#   --ollama-host URL                  --ollama external のとき必須
#                                       required with --ollama external
#   --qdrant local|external|skip       既定 local / default local
#   --qdrant-url URL                   --qdrant external のとき必須
#                                       required with --qdrant external
#   --qdrant-api-key KEY               任意（--qdrant external のみ）
#                                       optional (--qdrant external only)
#   --web / --no-web                   WebUI も入れる/入れない・既定 no-web
#                                       also install the WebUI or not — default no-web
#   -h, --help                         このヘルプを表示 / show this help
#
# 例 / Examples:
#   ./scripts/install-docker.sh
#     # 対話的に質問される（対話端末が無ければ全部自前・WebUIなしが既定）
#     # asked interactively (defaults to self-hosting everything with no
#     # WebUI when there is no TTY)
#
#   ./scripts/install-docker.sh --postgres external \
#     --postgres-dsn postgresql://user:pw@host:5432/db \
#     --ollama local --qdrant skip --web
#     # 質問されない。PostgreSQL だけ外部、Ollama は自前、Qdrant は使わない、
#     # WebUI は入れる
#     # no prompts: external PostgreSQL, self-hosted Ollama, no Qdrant,
#     # WebUI included

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CHAT_MODEL="qwen2.5:14b"
EMBED_MODEL="bge-m3"

step() { printf '\n==> %s\n' "$1"; }
note() { printf '    %s\n' "$1"; }
warn() { printf '\n[!] %s\n' "$1"; }
fail() { printf '\nエラー / Error: %s\n\n' "$1" >&2; exit 1; }

usage() {
  sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- CLI 引数の解析 / parsing CLI arguments -----------------------------------

POSTGRES_MODE=""
OLLAMA_MODE=""
QDRANT_MODE=""
WEB_MODE=""
PG_DSN_VALUE=""
OLLAMA_HOST_VALUE=""
QDRANT_URL_VALUE=""
QDRANT_API_KEY_VALUE=""
CLI_SELECTION_GIVEN=0

require_value() {
  # $1 = フラグ名 / flag name, $2 = 次の引数があるか / whether a next arg exists
  if [ "$2" -eq 0 ]; then
    fail "$1 には値が要ります / $1 requires a value"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --postgres)
      require_value "--postgres" $(( $# > 1 ? 1 : 0 ))
      POSTGRES_MODE="$2"; CLI_SELECTION_GIVEN=1; shift 2 ;;
    --postgres-dsn)
      require_value "--postgres-dsn" $(( $# > 1 ? 1 : 0 ))
      PG_DSN_VALUE="$2"; shift 2 ;;
    --ollama)
      require_value "--ollama" $(( $# > 1 ? 1 : 0 ))
      OLLAMA_MODE="$2"; CLI_SELECTION_GIVEN=1; shift 2 ;;
    --ollama-host)
      require_value "--ollama-host" $(( $# > 1 ? 1 : 0 ))
      OLLAMA_HOST_VALUE="$2"; shift 2 ;;
    --qdrant)
      require_value "--qdrant" $(( $# > 1 ? 1 : 0 ))
      QDRANT_MODE="$2"; CLI_SELECTION_GIVEN=1; shift 2 ;;
    --qdrant-url)
      require_value "--qdrant-url" $(( $# > 1 ? 1 : 0 ))
      QDRANT_URL_VALUE="$2"; shift 2 ;;
    --qdrant-api-key)
      require_value "--qdrant-api-key" $(( $# > 1 ? 1 : 0 ))
      QDRANT_API_KEY_VALUE="$2"; shift 2 ;;
    --web)
      WEB_MODE=1; CLI_SELECTION_GIVEN=1; shift ;;
    --no-web)
      WEB_MODE=0; CLI_SELECTION_GIVEN=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      fail "不明な引数です / unknown argument: $1 (--help を参照 / see --help)" ;;
  esac
done

for name_value in "--postgres:$POSTGRES_MODE" "--ollama:$OLLAMA_MODE"; do
  mode="${name_value#*:}"
  flag="${name_value%%:*}"
  case "$mode" in
    ""|local|external) ;;
    *) fail "${flag} は local か external のどちらかです / must be local or external: $mode" ;;
  esac
done
case "$QDRANT_MODE" in
  ""|local|external|skip) ;;
  *) fail "--qdrant は local・external・skip のどれかです / must be local, external, or skip: $QDRANT_MODE" ;;
esac

if [ "$POSTGRES_MODE" = external ] && [ -z "$PG_DSN_VALUE" ]; then
  fail "--postgres external には --postgres-dsn が必要です / --postgres external requires --postgres-dsn"
fi
if [ "$OLLAMA_MODE" = external ] && [ -z "$OLLAMA_HOST_VALUE" ]; then
  fail "--ollama external には --ollama-host が必要です / --ollama external requires --ollama-host"
fi
if [ "$QDRANT_MODE" = external ] && [ -z "$QDRANT_URL_VALUE" ]; then
  fail "--qdrant external には --qdrant-url が必要です / --qdrant external requires --qdrant-url"
fi

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
# CLI 引数で1つでも指定されていれば、対話質問は一切行わない
# （指定しなかったものは既定の local を使う）。CLI 引数が無く、かつ
# 対話端末があれば質問する。対話端末も無ければ（CI・スクリプト経由の
# 実行など）、全部自前という従来どおりの既定に倒す。
#
# If any CLI flag was given, no interactive questions are asked at all —
# anything not specified falls back to its default (local). With no CLI
# flags and an interactive terminal, ask. With neither, fall back to the
# previous default of self-hosting everything.

if [ "$CLI_SELECTION_GIVEN" -eq 1 ]; then
  note "CLI 引数が指定されているため、対話質問はスキップします "
  note "/ CLI flags were given: skipping interactive prompts"
  [ -n "$POSTGRES_MODE" ] || POSTGRES_MODE=local
  [ -n "$OLLAMA_MODE" ]   || OLLAMA_MODE=local
  [ -n "$QDRANT_MODE" ]   || QDRANT_MODE=local
  [ -n "$WEB_MODE" ]      || WEB_MODE=0
elif [ -t 0 ]; then
  step "PostgreSQL の接続先 / PostgreSQL target"
  note "1) このマシンに自前で立てる（既定）/ run it on this machine (default)"
  note "2) 外部の PostgreSQL に接続する / connect to an external PostgreSQL"
  read -r -p "    選択 / choice [1]: " choice || choice=""
  if [ "$choice" = "2" ]; then
    POSTGRES_MODE=external
    read -r -p "    接続文字列 / connection string (postgresql://...): " PG_DSN_VALUE
    [ -n "$PG_DSN_VALUE" ] || fail "接続文字列が空です / connection string cannot be empty"
  else
    POSTGRES_MODE=local
  fi

  step "Ollama の接続先 / Ollama target"
  note "1) このマシンに自前で立てる（既定）/ run it on this machine (default)"
  note "2) 外部の Ollama に接続する / connect to an external Ollama"
  read -r -p "    選択 / choice [1]: " choice || choice=""
  if [ "$choice" = "2" ]; then
    OLLAMA_MODE=external
    read -r -p "    URL（例 / e.g. http://your-host:11434）: " OLLAMA_HOST_VALUE
    [ -n "$OLLAMA_HOST_VALUE" ] || fail "URL が空です / URL cannot be empty"
  else
    OLLAMA_MODE=local
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
    *)
      QDRANT_MODE=local
      ;;
  esac

  step "WebUI（スマホ向け画面）/ WebUI (mobile-friendly screen)"
  note "CLI だけで完結します。スマホから使う・進捗を見せたい場合だけ要ります。"
  note "The CLI is complete on its own; this only matters for phone access or"
  note "showing progress to someone else."
  read -r -p "    WebUI も入れますか？ / Install it too? (y/N): " choice || choice=""
  case "$choice" in
    y|Y|yes|YES) WEB_MODE=1 ;;
    *) WEB_MODE=0 ;;
  esac
else
  note "非対話環境のため既定値を使います（PostgreSQL・Ollama・Qdrant を"
  note "すべて自前で立てる、WebUI は入れない）/ non-interactive: using"
  note "defaults (self-hosting PostgreSQL, Ollama, and Qdrant all; no WebUI)"
  POSTGRES_MODE=local
  OLLAMA_MODE=local
  QDRANT_MODE=local
  WEB_MODE=0
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
[ "$WEB_MODE"      = 1 ]     && PROFILE_ARGS="$PROFILE_ARGS --profile web"

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
[ "$WEB_MODE" = 1 ] && docker compose build web

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

if [ "$WEB_MODE" = 1 ]; then
  printf '  WebUI を起動しました / WebUI is running:\n'
  printf '    docker compose logs web        # 実行用・閲覧用のURL / operator and viewer URLs\n\n'
else
  printf '  WebUI は入れていません。後から入れる場合は次を実行してください:\n'
  printf '  WebUI was not installed. To add it later, run:\n'
  printf '    %s --web\n\n' "$0"
fi
