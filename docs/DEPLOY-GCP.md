# Google Cloud Free Tier + 外部 PostgreSQL
# Google Cloud Free Tier + external PostgreSQL

月額 0 円で動かせる構成です。Oracle 版と同じ考え方（無料の小さな VM +
外部の無料 PostgreSQL）ですが、GCP の無料枠は Oracle よりさらに小さく、
制約がそのまま設計を決めます。まず制約から書きます。

A zero-cost deployment using the same idea as the Oracle guide — a small
free VM plus an external free PostgreSQL — but GCP's free tier is smaller
than Oracle's, and that limit shapes the design directly. It comes first.

---

## 先に知っておくこと / Read this first

### e2-micro は「常に無料」だが、地域が固定される

GCP の Always Free 対象は **e2-micro インスタンス1台**（バースト可能な
共有 vCPU・**RAM 1GB**）、かつ **`us-west1` / `us-central1` / `us-east1`
のいずれか**に限られます。日本など他地域に作ると、その時点で課金対象です。
新規アカウントには別途、90日間 $300 の無料トライアル枠もありますが、
これは e2-micro の Always Free とは別物です。

GCP's Always Free covers **one e2-micro instance** (burstable shared vCPU,
**1GB RAM**), and only in **`us-west1`, `us-central1`, or `us-east1`**.
Creating it in any other region (including Japan) is billed from the start.
New accounts separately get a $300/90-day trial credit — a different program
from the perpetual e2-micro allowance.

### RAM 1GB では Qdrant を同居させる余地がほぼ無い

Oracle 版の 12GB と違い、e2-micro の 1GB では Caddy + aipmo +
scheduler だけでほぼ埋まります。**この構成では Qdrant を既定で外しています。**
ベクトル検索・ナレッジ機能が要る場合は、有料の e2-small（2GB）以上に
上げてください。

Unlike Oracle's 12GB, e2-micro's 1GB is largely spent just on Caddy + aipmo +
scheduler. **Qdrant is left out by default here.** If you need vector search
or the knowledge features, move up to a paid e2-small (2GB) or larger.

### この構成にローカル LLM は入れていません

1GB・共有 vCPU では、ローカル LLM は現実的な速度で動きません。
Oracle 版と同じ理由です — 詳しくは [docs/DEPLOY-ORACLE.md](DEPLOY-ORACLE.md#先に知っておくこと--read-this-first)。

A local LLM is not viable on 1GB of shared vCPU, for the same reason as the
Oracle guide.

---

## 構成 / Architecture

```
   スマホ / phone
        │  HTTPS
        ▼
  ┌───────────────────────────────────┐
  │  GCP e2-micro (us-west1 等)        │
  │  1 vCPU (burst) / 1GB RAM          │
  │                                    │
  │   Caddy ──── TLS 終端・自動更新     │
  │     │                              │
  │   aipmo ─── Web 画面 + 実行エンジン  │
  └─────┼──────────────────────────────┘
        │
        ▼
   Aiven PostgreSQL
   1GB・外部
```

| | 置き場所 | 費用 |
|---|---|---|
| Web 画面・実行エンジン | GCP e2-micro | 無料（対象リージョンのみ） |
| PostgreSQL | Aiven 外部 | 無料（1GB） |
| TLS 証明書 | Let's Encrypt | 無料 |
| AI | OpenAI 等 | 従量制 |

`deploy/generic/` の構成をそのまま使います。詳しくは
[deploy/generic/docker-compose.yml](../deploy/generic/docker-compose.yml)。

Uses `deploy/generic/` as-is — see
[deploy/generic/docker-compose.yml](../deploy/generic/docker-compose.yml).

---

## 手順 / Steps

### 1. インスタンスを作る / Create the instance

Console → Compute Engine → VM インスタンス → 作成

- Machine type: **e2-micro**
- Region: **us-west1 / us-central1 / us-east1 のいずれか**（これ以外は課金）
- Boot disk: Ubuntu 24.04 LTS、**標準永続ディスク 30GB 以下**
  （Always Free の対象は標準ディスク30GBまで。SSD 永続ディスクは対象外）
- 外部 IP を割り当てる（既定で ON）

> Region must be one of `us-west1`, `us-central1`, `us-east1` — anything
> else is billed. Keep the boot disk to a **standard** persistent disk of
> 30GB or less; an SSD persistent disk is not covered by Always Free.

### 2. ファイアウォールを開ける / Open the firewall

VPC ネットワーク → ファイアウォール → ルールを作成

- 上り (ingress)、ソース `0.0.0.0/0`、TCP ポート `80,443`
- 対象タグをインスタンスに付ける（例: `http-server`, `https-server` の
  既定タグを使うなら、インスタンス作成時に「HTTP トラフィックを許可する」
  「HTTPS トラフィックを許可する」にチェックするだけでよい）

If you tick "Allow HTTP traffic" / "Allow HTTPS traffic" when creating the
instance, GCP creates these firewall rules for you automatically.

### 3. ドメインを用意する / Point a domain at it

TLS 証明書には名前が要ります。持っていなければ DuckDNS などの無料
サブドメインで足ります。A レコードをインスタンスの外部 IP に向けてください。

A certificate needs a name; a free subdomain from DuckDNS is enough. Point
an A record at the instance's external IP.

### 4. 初期設定を流す / Bootstrap the instance

```bash
ssh <ユーザー名>@<外部IP>
git clone <repo> && cd aipmo
./deploy/generic/bootstrap.sh
```

Docker の導入、ufw での 80/443 開放、スワップの作成を行います。
1GB RAM なので、`--profile full`（Qdrant 追加）は使わないでください。

Installs Docker, opens 80/443 via ufw, and creates swap. Do not use
`--profile full` (adds Qdrant) — there is not enough RAM for it here.

### 5. Aiven を用意する / Provision Aiven

Oracle 版と同じ手順です。[docs/DEPLOY-ORACLE.md の該当箇所](DEPLOY-ORACLE.md#4-aiven-を用意する--provision-aiven)
を参照してください。

Same steps as the Oracle guide — see its
[Aiven section](DEPLOY-ORACLE.md#4-aiven-を用意する--provision-aiven).

### 6. 設定して起動する / Configure and start

```bash
cp deploy/generic/.env.example deploy/generic/.env
python3 -c "import secrets;print(secrets.token_urlsafe(24))"   # アクセスキー
vi deploy/generic/.env
cp /path/to/aiven-ca.pem deploy/generic/db-ca.pem

psql "$AIPMO_PG_DSN" -f sql/schema.sql

cd deploy/generic && docker compose up -d --build
```

### 7. スマホで開く / Open it on your phone

```
https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>
```

---

## 運用上の注意 / Operating notes

### 無料枠を超えないように / Staying inside the free allowance

**インスタンスは1台まで。** 2台目の e2-micro や、対象リージョン外への
デプロイは全額課金です。GCP の請求アラートを 0円〜数百円で設定しておくと、
超過に早く気付けます。

**Only one instance.** A second e2-micro, or one outside the covered
regions, is billed in full. Set a GCP billing alert at a low threshold so an
overage is caught early.

### バックアップ / Backups

```bash
pg_dump "$AIPMO_PG_DSN" | gzip > aipmo-$(date +%F).sql.gz
```

インスタンスのスナップショットは無料枠の対象外（課金）です。設定ファイル
（`deploy/generic/.env`、`config.yaml` の変更点）は別途ローカルに控えて
おいてください。

Instance snapshots are billed, not covered by the free tier. Keep a local
copy of your configuration changes (`.env`, any edits to `config.yaml`)
separately.

### 無料枠は変わる / Free tiers change

この文書の数値は 2026年8月時点のものです。**設計を数値に固定しないで
ください。** 最新の対象リージョン・スペックは Google Cloud の Free Tier
のページで確認してください。

The numbers here are as of August 2026. Do not hard-code a free tier's
current limits into your assumptions — check Google Cloud's own Free Tier
page for the current regions and specs.

---

## この構成が向かないもの / What this is not for

- **本番の業務データ** — 単一インスタンス・単一 DB でバックアップも
  可用性の保証もありません
- **機微な会議記録** — AI はクラウドに出ます
- **ベクトル検索が要る用途** — 1GB では Qdrant が同居できません。
  有料の e2-small 以上か、[Oracle 版](DEPLOY-ORACLE.md) を検討してください
- **多人数の同時利用** — 共有 vCPU 1つでは数人が上限です

Not for production data, sensitive transcripts, workloads needing vector
search (upgrade to a paid e2-small or use the Oracle guide instead), or many
concurrent users.

**試す・小さく回す・型を作る**にはよく機能します。
It works well for trying things, running small, and building the templates.
