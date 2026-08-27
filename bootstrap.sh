#!/usr/bin/env bash
# Oracle Cloud Always Free インスタンスの初期設定
# First-time setup for an Oracle Cloud Always Free instance
#
#   curl -fsSL <repo>/deploy/oracle/bootstrap.sh | bash
#
# Ubuntu 22.04 / 24.04 (arm64) を想定 / assumes Ubuntu 22.04 or 24.04 on arm64.

set -euo pipefail

step() { printf '\n==> %s\n'   "$1"; }
note() { printf '    %s\n'     "$1"; }
warn() { printf '\n[!] %s\n'   "$1"; }
fail() { printf '\nエラー / Error: %s\n\n' "$1" >&2; exit 1; }

printf '\n  AI-PMO — Oracle Cloud 初期設定 / bootstrap\n'
printf '  ---------------------------------------------\n'

[ "$(uname -m)" = "aarch64" ] || warn "arm64 以外です。Ampere A1 想定の構成です / not arm64; this stack targets Ampere A1."

# --- 割り当ての確認 / check the allowance ---------------------------------
# Oracle は 2026年6月15日に Always Free 枠を 2 OCPU / 12GB へ半減した。
# 超過インスタンスは終了対象なので、最初に気づけるようにする。
# Oracle halved the Always Free allowance to 2 OCPU / 12GB on 15 June 2026 and
# terminates instances above it, so surface this before anything else.
step "割り当てを確認しています / Checking the allowance"
CPUS=$(nproc)
MEM_GB=$(( $(getconf _PHYS_PAGES) * $(getconf PAGE_SIZE) / 1024 / 1024 / 1024 ))
note "OCPU: ${CPUS}  RAM: ${MEM_GB}GB"
if [ "$CPUS" -gt 2 ] || [ "$MEM_GB" -gt 13 ]; then
  warn "Always Free の現行上限（2 OCPU / 12GB）を超えています。
    終了または課金の対象になり得ます。コンソールでシェイプを縮小してください。
    Above the current Always Free limit (2 OCPU / 12GB). This can be terminated
    or billed. Resize the shape in the console."
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

# --- ファイアウォール / firewall --------------------------------------------
# Oracle の Ubuntu イメージは iptables で 22 以外を落とす。
# コンソールのセキュリティリストだけ開けても届かない。ここで最も多く詰まる。
# Oracle's Ubuntu images drop everything but port 22 in iptables. Opening the
# console Security List alone is not enough — this is where most people stall.
step "ファイアウォールを設定しています / Configuring the firewall"
if sudo iptables -L INPUT -n | grep -q "REJECT"; then
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
  sudo netfilter-persistent save >/dev/null 2>&1 || \
    sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
  note "80/443 を開放しました / opened 80 and 443"
else
  note "既存の規則を変更しませんでした / left the existing rules alone"
fi

warn "OCI コンソール側の作業が別途必要です / one step remains in the OCI console:
    VCN > セキュリティリスト > イングレス規則に 0.0.0.0/0 の TCP 80 と 443 を追加。
    Add ingress rules for TCP 80 and 443 from 0.0.0.0/0 in the VCN security list.
    ここを忘れると、上の iptables 設定だけでは外から届きません。
    Without it the iptables change above is not enough."

# --- スワップ / swap ---------------------------------------------------------
# 12GB でビルドと Qdrant を同時に走らせると足りなくなることがある。
# スワップが無いと OOM Killer がコンテナを落とす。
# Builds and Qdrant together can exhaust 12GB. Without swap the OOM killer
# takes a container down instead of the machine slowing.
step "スワップを確認しています / Checking swap"
if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  note "4GB のスワップを作成しました / created a 4GB swap file"
else
  note "設定済み / already configured"
fi

# --- Aiven の CA 証明書 / Aiven CA certificate ------------------------------
step "次の作業 / What to do next"
cat <<'NEXT'

    1. Aiven コンソールから CA 証明書を取得し、次の場所に置く
       Download the CA certificate from the Aiven console and save it as:
         deploy/oracle/aiven-ca.pem

    2. .env を作る / create .env
         cp deploy/oracle/.env.example deploy/oracle/.env
         # DOMAIN / AIPMO_WEB_TOKEN / AIPMO_PG_DSN / OPENAI_API_KEY を埋める

    3. スキーマを流す / load the schema
         psql "$AIPMO_PG_DSN" -f sql/schema.sql

    4. 起動する / start
         cd deploy/oracle && docker compose up -d --build

    5. スマホで開く / open on your phone
         https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>

NEXT
