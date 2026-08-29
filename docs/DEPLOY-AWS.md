# AWS EC2 無料枠 + RDS（または外部 PostgreSQL）
# AWS EC2 free tier + RDS (or an external PostgreSQL)

AWS の無料利用枠で動かす構成です。Azure と同じく **期限付き**（新規
アカウントから12か月間）なので、その前提から書きます。EC2 と対になる
無料 PostgreSQL（RDS）が AWS 自身にあるので、Oracle・GCP・Azure の
どれとも少し違う選択肢があります。

Runs on AWS's free usage tier, using the same idea as the other guides.
Like Azure, it is **time-limited** (12 months from a new account), so that
comes first. AWS has its own free PostgreSQL (RDS) that pairs naturally
with EC2, giving a slightly different choice than the Oracle, GCP, or Azure
guides.

---

## 先に知っておくこと / Read this first

### 無料は「12か月間」であって、永久ではない

新規 AWS アカウントから **12か月間**、`t2.micro` または `t3.micro`
（リージョンによりどちらか）を **月750時間**まで無料で使えます。
Oracle・GCP の Always Free（期限なし）とは違う前提です。12か月後に
どうするかを、デプロイ前に決めておいてください。

A new AWS account gets **12 months** of `t2.micro` or `t3.micro` (which one
depends on the region) for up to **750 hours/month**, free. Unlike Oracle's
or GCP's Always Free, this expires. Decide what happens after month 12
before deploying.

### RDS の無料枠を使うか、外部の Aiven を使うか

AWS には **RDS の無料枠**（`db.t3.micro` 相当、20GB、こちらも新規
アカウントから12か月間）があります。EC2 と同じ12か月で切れるので、
どちらにしても期限は揃います。

- **RDS を使う** — AWS 1社で完結する。同じ VPC 内なので TLS 証明書検証の
  やり取りが単純
- **Aiven を使う**（他ガイドと同じ外部無料プラン）— AWS 依存を減らせる。
  ただし RDS の12か月とは無関係に、Aiven 自体の無料プランの条件に従う

このガイドは Oracle 版との一貫性を優先し、**Aiven を使う手順**を主に
書きます。RDS を使う場合の違いは各手順に注記します。

AWS also has an **RDS free tier** (`db.t3.micro`-class, 20GB, also 12 months
from a new account) — its expiry lines up with EC2's regardless of which you
pick. Using RDS keeps everything inside AWS; using Aiven (the same external
free plan as the other guides) reduces AWS-specific lock-in but follows
Aiven's own terms independently of RDS's 12 months. This guide mainly
documents **the Aiven path**, for consistency with the other guides, and
notes where RDS differs at each step.

### t2/t3.micro は 1GB RAM。Qdrant を同居させる余地がほぼ無い

GCP・Azure と同じ制約です。**この構成では Qdrant を既定で外しています。**

Same constraint as GCP and Azure. **Qdrant is left out by default here.**

---

## 構成 / Architecture

```
   スマホ / phone
        │  HTTPS
        ▼
  ┌───────────────────────────────────┐
  │  EC2 t2/t3.micro（12か月間無料）     │
  │  1 vCPU (burst) / 1GB RAM          │
  │                                    │
  │   Caddy ──── TLS 終端・自動更新     │
  │     │                              │
  │   aipmo ─── Web 画面 + 実行エンジン  │
  └─────┼──────────────────────────────┘
        │
        ▼
   Aiven PostgreSQL          （または RDS db.t3.micro）
   1GB・外部                  （or RDS db.t3.micro instead）
```

| | 置き場所 | 費用 |
|---|---|---|
| Web 画面・実行エンジン | EC2 t2/t3.micro | 無料（12か月間・月750時間まで） |
| PostgreSQL | Aiven 外部 or RDS | 無料（どちらも条件あり） |
| TLS 証明書 | Let's Encrypt | 無料 |
| AI | OpenAI 等 | 従量制 |

`deploy/generic/` の構成をそのまま使います。
Uses `deploy/generic/` as-is.

---

## 手順 / Steps

### 1. インスタンスを作る / Launch the instance

EC2 コンソール → インスタンスを起動

- インスタンスタイプ: **t2.micro**（対象外リージョンでは t3.micro）
- AMI: Ubuntu Server 24.04 LTS
- キーペア: 新規作成し、`.pem` を安全に保管
- セキュリティグループ: 新規作成（次の手順で編集）

> **「無料利用枠の対象」ラベルが付いているものを選んでください。**
> ラベルの無いタイプ・容量を選ぶと、その時点で課金対象です。
>
> Pick only what is labelled "Free tier eligible" in the console — anything
> without that label is billed immediately.

### 2. セキュリティグループを開ける / Open the security group

インスタンスのセキュリティグループ → インバウンドルールを編集

- タイプ: HTTP、ポート: 80、ソース: `0.0.0.0/0`
- タイプ: HTTPS、ポート: 443、ソース: `0.0.0.0/0`

標準の Ubuntu AMI は Oracle のイメージと違い、**インスタンス内部の
iptables では 80/443 を塞いでいません。** セキュリティグループの設定
だけで届きます。

Unlike Oracle's images, a standard Ubuntu AMI does **not** block 80/443 at
the instance's own iptables level — the security group setting alone is
enough.

### 3. ドメインを用意する / Point a domain at it

TLS 証明書には名前が要ります。持っていなければ DuckDNS などの無料
サブドメインで足ります。A レコードをインスタンスのパブリック IP に
向けてください（Elastic IP を割り当てておくと、再起動でも IP が
変わりません）。

A certificate needs a name; a free subdomain from DuckDNS is enough.
Associating an Elastic IP keeps the address stable across reboots.

### 4. 初期設定を流す / Bootstrap the instance

```bash
ssh ubuntu@<パブリックIP> -i your-key.pem
git clone <repo> && cd aipmo
./deploy/generic/bootstrap.sh
```

1GB RAM なので `--profile full`（Qdrant 追加）は使わないでください。

### 5-A. Aiven を使う場合 / Using Aiven

[docs/DEPLOY-ORACLE.md の該当手順](DEPLOY-ORACLE.md) と同じです。

### 5-B. RDS を使う場合 / Using RDS instead

RDS コンソール → データベースの作成

- エンジン: PostgreSQL、テンプレート: **無料利用枠**
- インスタンスクラス: `db.t3.micro`（対象外リージョンでは `db.t3.micro`
  相当の無料対象クラス）
- パブリックアクセス: いいえ（EC2 と同じ VPC 内から接続）
- セキュリティグループ: EC2 のセキュリティグループからの 5432 を許可

RDS はデフォルトで AWS の CA 証明書チェーンを使います。`sslmode=verify-full`
に必要な CA 証明書は AWS のドキュメントにある `global-bundle.pem` を
使ってください（Aiven のような個別ダウンロードは不要です）。

RDS uses AWS's own CA chain by default. For `sslmode=verify-full`, use the
`global-bundle.pem` documented by AWS rather than a per-instance download
like Aiven's.

### 6. 設定して起動する / Configure and start

```bash
cp deploy/generic/.env.example deploy/generic/.env
python3 -c "import secrets;print(secrets.token_urlsafe(24))"
vi deploy/generic/.env
cp /path/to/ca-bundle.pem deploy/generic/db-ca.pem   # Aiven か AWS の CA

psql "$AIPMO_PG_DSN" -f sql/schema.sql

cd deploy/generic && docker compose up -d --build
```

### 7. スマホで開く / Open it on your phone

```
https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>
```

---

## 運用上の注意 / Operating notes

### 12か月後にどうするか、今のうちに決める / Decide what happens after month 12

Billing → Budgets で、無料枠の使用量に対する予算アラートを設定して
ください。**750時間/月を超えるか、12か月を過ぎると課金が始まります。**
EC2 と RDS の両方を見ておいてください。

Set a budget alert in Billing → Budgets. **Exceeding 750 hours/month, or
passing month 12, starts billing** — watch both EC2 and RDS if you used it.

### バックアップ / Backups

```bash
pg_dump "$AIPMO_PG_DSN" | gzip > aipmo-$(date +%F).sql.gz
```

RDS を使っている場合は自動バックアップ（既定7日保持）も有効ですが、
無料枠のストレージ 20GB を圧迫するので、保持期間を長くしすぎないで
ください。

RDS's automatic backups (7-day retention by default) also count against the
20GB free-tier storage, so avoid extending retention too far.

### 無料枠は変わる / Free tiers change

この文書の数値は 2026年8月時点のものです。**設計を数値に固定しないで
ください。** 最新の対象タイプ・期間は AWS Free Tier のページで
確認してください。

---

## この構成が向かないもの / What this is not for

- **本番の業務データ、機微な会議記録、多人数の同時利用** — 理由は
  [Oracle 版](DEPLOY-ORACLE.md) と同じです
- **12か月を超えて無料で使い続けたい用途** — EC2 の無料枠は期限付きです

Not for production data, sensitive transcripts, or many concurrent users,
for the same reasons as the Oracle guide — and not for anything that must
stay free past month 12.

**12か月のあいだ、試す・小さく回す・型を作る**にはよく機能します。
