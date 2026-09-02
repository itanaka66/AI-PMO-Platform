#!/usr/bin/env bash
# 汎用インスタンスの初期設定 / first-time setup for a generic instance
#
#   curl -fsSL <repo>/deploy/generic/bootstrap.sh | bash
#
# GCP・Azure・AWS EC2・Hetzner・さくらの VPS など、Ubuntu 22.04/24.04 の
# 標準イメージを想定しています。クラウド固有のファイアウォール
# （セキュリティグループ・NSG・パケットフィルタ等）は別途、各クラウドの
# コンソール側で 80/443 を開ける必要があります — このスクリプトが扱うのは
# インスタンス内部の ufw だけです。
#
# Assumes a standard Ubuntu 22.04/24.04 image, on GCP, Azure, AWS EC2,
# Hetzner, Sakura VPS, or similar. Each cloud's own firewall layer (security
# group, NSG, packet filter, ...) needs 80/443 opened separately in its own
# console — this script only handles ufw inside the instance.

set -euo pipefail

step() { printf '\n==> %s\n'   "$1"; }
note() { printf '    %s\n'     "$1"; }
warn() { printf '\n[!] %s\n'   "$1"; }

printf '\n  AI-PMO — 汎用インスタンス初期設定 / generic bootstrap\n'
printf '  -----------------------------------------------------\n'

# --- 割り当ての確認 / check what we actually have -------------------------
step "リソースを確認しています / Checking resources"
CPUS=$(nproc)
MEM_GB=$(( ($(getconf _PHYS_PAGES) * $(getconf PAGE_SIZE) + 1073741823) / 1073741824 ))
note "CPU: ${CPUS}  RAM: ${MEM_GB}GB"
if [ "$MEM_GB" -le 1 ]; then
  warn "RAM が 1GB クラスです。既定の docker-compose.yml（Qdrant 無し）を
    そのまま使ってください。--profile full は足りなくなります。
    1GB-class RAM: stick to the default docker-compose.yml (no Qdrant).
    --profile full will not fit."
fi

# --- Docker -----------------------------------------------------------------
step "Docker を導入しています / Installing Docker"
if command -v docker >/dev/null 2>&1; then
  note "導入済み / already present: $(docker --version)"
else
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  note "グループ反映のため再ログインが必要です / log out and back in for group membership"
fi

# --- ファイアウォール (ufw) / firewall (ufw) --------------------------------
# ここで開けるのはインスタンス内部だけ。クラウド側のファイアウォールは
# 別途、各クラウドのコンソールで開ける必要がある。
# This only opens the instance's own firewall. The cloud's firewall layer
# still needs opening separately, in that cloud's own console.
step "ufw を設定しています / Configuring ufw"
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 22/tcp  >/dev/null
  sudo ufw allow 80/tcp  >/dev/null
  sudo ufw allow 443/tcp >/dev/null
  sudo ufw --force enable >/dev/null
  note "22/80/443 を開放しました / opened 22, 80 and 443"
else
  note "ufw が無いのでスキップしました。distribution 標準の方法で
    80/443 を開けてください / ufw not found; open 80/443 with whatever this
    distribution uses instead."
fi

warn "クラウド側のファイアウォールも別途必要です / the cloud's own firewall
    still needs 80 and 443 opened — see this platform's deploy guide in
    docs/ for where that setting lives."

# --- スワップ / swap ---------------------------------------------------------
# RAM が小さい機械でビルドすると足りなくなることがある。
# スワップが無いと OOM Killer がコンテナを落とす。
# A small-RAM machine can run out of memory while building. Without swap the
# OOM killer takes a container down instead of the build merely slowing.
step "スワップを確認しています / Checking swap"
if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  # RAM が小さいほど、相対的に大きめのスワップを積む。
  # The smaller the RAM, the more swap it relatively needs.
  if [ "$MEM_GB" -le 1 ]; then SWAP_GB=2; else SWAP_GB=1; fi
  sudo fallocate -l "${SWAP_GB}G" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  note "${SWAP_GB}GB のスワップを作成しました / created a ${SWAP_GB}GB swap file"
else
  note "設定済み / already configured"
fi

# --- 次の作業 / what to do next ---------------------------------------------
step "次の作業 / What to do next"
cat <<'NEXT'

    1. .env を作る / create .env
         cp deploy/generic/.env.example deploy/generic/.env
         # DOMAIN / AIPMO_WEB_TOKEN / AIPMO_PG_DSN / OPENAI_API_KEY を埋める

    2. DB の証明書（外部の Aiven / RDS などを使う場合）
       CA cert (only if using an external DB such as Aiven or RDS):
         cp /path/to/downloaded-ca.pem deploy/generic/db-ca.pem
       使わない場合は空ファイルで構いません（マウント先として必要）:
       Not using one? An empty file satisfies the mount:
         touch deploy/generic/db-ca.pem

    3. スキーマを流す（外部 DB の場合。自前 Postgres は起動時に自動で流れる）
       Load the schema (external DB only; a self-hosted Postgres loads it
       automatically on first start):
         psql "$AIPMO_PG_DSN" -f sql/schema.sql

    4. 起動する / start
         cd deploy/generic && docker compose up -d --build
       RAM に余裕があれば / with RAM to spare:
         docker compose --profile full --profile selfhosted up -d --build

    5. スマホで開く / open on your phone
         https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>

NEXT
