# Microsoft Azure 無料アカウント + 外部 PostgreSQL
# Microsoft Azure free account + external PostgreSQL

Azure の無料アカウントで動かす構成です。**Oracle や GCP の Always Free と
違い、Azure の無料 VM は期限付きです。** ここが設計より先に知っておくべき
最大の違いです。

Runs on an Azure free account, using the same small-VM-plus-external-DB idea
as the other guides. **Unlike Oracle's or GCP's perpetual Always Free VM,
Azure's free VM expires.** That is the biggest thing to know before anything
about the design.

---

## 先に知っておくこと / Read this first

### 無料は「12か月間」であって、永久ではない

Azure の無料アカウントは、新規登録から **30日間 $200 のクレジット**、
それとは別に **12か月間、対象サービスが無料**（B1S 仮想マシンを月750時間
など）という2階建てです。**12か月を過ぎると、B1S 仮想マシンは無料では
なくなります。** Oracle・GCP の Always Free（期限なし）とは根本的に
違う前提です。

Azure's free account is two layers: a **$200/30-day credit**, and
separately, **12 months of free usage** on eligible services (a B1S VM for
750 hours/month, among others). **After 12 months, the B1S VM is no longer
free.** This is fundamentally different from Oracle's or GCP's Always Free,
which do not expire.

12か月経過後にどうするかを、デプロイ前に決めておいてください
（有料継続 / 他クラウドへ移行 / 停止）。

Decide before deploying what happens after month 12 — keep paying, migrate
elsewhere, or shut it down.

### B1S は 1GB RAM。Qdrant を同居させる余地がほぼ無い

GCP の e2-micro と同じ制約です。**この構成では Qdrant を既定で外して
います。**

Same constraint as GCP's e2-micro. **Qdrant is left out by default here.**

### この構成にローカル LLM は入れていません

1GB では現実的な速度で動きません。理由は
[docs/DEPLOY-ORACLE.md](DEPLOY-ORACLE.md) と同じです。

Not viable on 1GB, for the same reason as the Oracle guide.

---

## 構成 / Architecture

```
   スマホ / phone
        │  HTTPS
        ▼
  ┌───────────────────────────────────┐
  │  Azure B1S VM（12か月間無料）        │
  │  1 vCPU / 1GB RAM                  │
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
| Web 画面・実行エンジン | Azure B1S | 無料（12か月間のみ） |
| PostgreSQL | Aiven 外部 | 無料（1GB） |
| TLS 証明書 | Let's Encrypt | 無料 |
| AI | OpenAI 等 | 従量制 |

`deploy/generic/` の構成をそのまま使います。
Uses `deploy/generic/` as-is.

---

## 手順 / Steps

### 1. 仮想マシンを作る / Create the VM

Azure Portal → 仮想マシンの作成

- サイズ: **Standard_B1s**（無料枠の対象）
- イメージ: Ubuntu Server 24.04 LTS
- 認証: SSH 公開鍵
- インバウンドポート: **SSH (22) のみ**をここで開ける（80/443 は次の手順）

> **無料対象かどうかは作成画面で確認してください。** 対象リージョン・
> 対象サブスクリプションの条件は変わることがあります。
>
> Confirm free-tier eligibility on the creation screen itself — the eligible
> regions and subscription conditions can change.

### 2. ネットワークセキュリティグループ (NSG) を開ける / Open the NSG

仮想マシン → ネットワーク → 受信ポートの規則を追加

- 優先度: 任意の空き番号、ポート: `80`、プロトコル: TCP、送信元: Any
- 同様に `443` も追加

Azure の VM は既定で NSG が **22番以外を拒否**します。80/443 を明示的に
追加しないと、Caddy を起動しても外から届きません。

Azure VMs deny everything but 22 by default at the NSG layer. Without
explicitly adding 80 and 443, Caddy will not be reachable from outside even
once it is running.

### 3. ドメインを用意する / Point a domain at it

TLS 証明書には名前が要ります。持っていなければ DuckDNS などの無料
サブドメインで足ります。A レコードを VM のパブリック IP に向けてください。

### 4. 初期設定を流す / Bootstrap the instance

```bash
ssh <ユーザー名>@<パブリックIP>
git clone <repo> && cd aipmo
./deploy/generic/bootstrap.sh
```

1GB RAM なので `--profile full`（Qdrant 追加）は使わないでください。

### 5. Aiven を用意する / Provision Aiven

[docs/DEPLOY-ORACLE.md の該当手順](DEPLOY-ORACLE.md) と同じです。

### 6. 設定して起動する / Configure and start

```bash
cp deploy/generic/.env.example deploy/generic/.env
python3 -c "import secrets;print(secrets.token_urlsafe(24))"
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

### 12か月後にどうするか、今のうちに決める / Decide what happens after month 12

Azure Portal → コスト管理 → 予算 で、12か月の期限が近づいたら通知が
届くよう予算アラートを設定してください。**期限を過ぎると自動的に
課金が始まります。** 気付かないまま放置しないための唯一の防御線です。

Set a budget alert in Cost Management so you are notified as month 12
approaches. **Billing starts automatically once the free period ends** —
the alert is the only real defence against not noticing.

### バックアップ / Backups

```bash
pg_dump "$AIPMO_PG_DSN" | gzip > aipmo-$(date +%F).sql.gz
```

### 無料枠は変わる / Free tiers change

この文書の数値は 2026年8月時点のものです。**設計を数値に固定しないで
ください。** 最新の対象・期間は Azure の無料アカウントのページで
確認してください。

---

## この構成が向かないもの / What this is not for

- **本番の業務データ、機微な会議記録、多人数の同時利用** — 理由は
  [Oracle 版](DEPLOY-ORACLE.md) と同じです
- **12か月を超えて無料で使い続けたい用途** — Azure の無料 VM は期限付き
  です。継続利用には支払いが必要になります

Not for production data, sensitive transcripts, or many concurrent users,
for the same reasons as the Oracle guide — and not for anything that must
stay free past month 12, since Azure's free VM is time-limited.

**12か月のあいだ、試す・小さく回す・型を作る**にはよく機能します。
It works well for trying things, running small, and building templates —
for the 12 months it lasts.
