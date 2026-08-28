#!/usr/bin/env bash
# Docker 構成のインストーラ / Docker deployment installer
#
# ローカル LLM を使う構成を一括で立ち上げる。
# Brings up the full local-LLM stack in one go.

set -euo pipefail

CHAT_MODEL="qwen2.5:14b"
EMBED_MODEL="bge-m3"

step() { printf '\n==> %s\n' "$1"; }
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

step "コンテナを起動しています / Starting the containers"
printf '    初回はイメージ取得に時間がかかります / First run downloads images.\n'
docker compose up -d postgres qdrant ollama

step "モデルを取得しています / Pulling models"
printf '    数 GB のダウンロードです / This downloads several GB.\n'
docker compose exec -T ollama ollama pull "$CHAT_MODEL"
docker compose exec -T ollama ollama pull "$EMBED_MODEL"

step "アプリをビルドしています / Building the application"
docker compose build aipmo

step "動作確認 / Smoke test"
docker compose run --rm aipmo validate templates/examples/meeting_minutes.yaml

printf '\n  完了しました / Done.\n\n'
printf '  使い方 / Usage:\n'
printf '    docker compose run --rm aipmo doctor\n'
printf '    docker compose run --rm aipmo run templates/examples/meeting_minutes.yaml\n\n'
