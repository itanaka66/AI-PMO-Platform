# 一般的な VPS（さくらの VPS を例に）+ 自前 PostgreSQL
# A generic VPS (using Sakura VPS as the example) + self-hosted PostgreSQL

有料の VPS 1台に完結させる構成です。無料枠クラスの 1GB RAM と違い、
2GB 以上あれば PostgreSQL も Qdrant も同じ機械に同居できるので、
**外部の無料 DB に頼る必要がありません。** ここではさくらの VPS を
具体例にしますが、同じ手順は ConoHa・Vultr・DigitalOcean のような
標準的な Ubuntu VPS 全般に当てはまります。

A single paid VPS running everything. With 2GB RAM or more — unlike the
1GB-class free tiers — PostgreSQL and Qdrant both fit on the same machine,
so **there is no need for an external free database.** Sakura VPS is the
concrete example here, but the same steps apply to any standard Ubuntu VPS
(ConoHa, Vultr, DigitalOcean, and similar).

---

## 先に知っておくこと / Read this first

### 有料だが、無料枠より単純になる

月額は数百円〜千円台（プランと為替により変動。さくらの VPS は最小構成
で概ね1,000円/月未満から）ですが、その分 RAM に余裕があります。**外部の
Aiven や RDS を使わず、同じ VPS 内で PostgreSQL を動かす**構成にできる
ので、外部サービスの登録・DSN の管理・アイドル停止からの復帰待ち、
といった無料枠特有の手間がまるごと無くなります。

Costs a few hundred to a few thousand yen a month (varies by plan and
exchange rate — Sakura's smallest plan runs under ¥1,000/month), but buys
enough RAM to run **PostgreSQL on the same VPS instead of an external
service.** That removes the free-tier-specific overhead entirely: no
external signup, no DSN to manage, no waiting out an idle-triggered cold
start.

### 想定スペック: 2GB RAM 以上

さくらの VPS なら 2GB プラン以上を推奨します。1GB プランでは無料枠と
同じ制約（Qdrant を外す）が必要になります。

Recommend 2GB RAM or more (Sakura's 2GB plan or above). A 1GB plan hits the
same constraint as the free tiers (Qdrant has to be left out).

---

## 構成 / Architecture

```
   スマホ / phone
        │  HTTPS
        ▼
  ┌───────────────────────────────────────┐
  │  VPS（さくらの VPS 等）2GB 以上          │
  │                                         │
  │   Caddy ──── TLS 終端・自動更新          │
  │     │                                   │
  │   aipmo ─── Web 画面 + 実行エンジン       │
  │     │                                   │
  │   Qdrant ── ベクトル検索                 │
  │     │                                   │
  │   Postgres ── 実行履歴・ナレッジ候補      │
  └─────────────────────────────────────────┘
```

| | 置き場所 | 費用 |
|---|---|---|
| Web 画面・実行エンジン・Qdrant・PostgreSQL | 同じ VPS | VPS 月額のみ |
| TLS 証明書 | Let's Encrypt | 無料 |
| AI | OpenAI 等 | 従量制 |

`deploy/generic/` を `--profile full --profile selfhosted` で起動し、
すべて1台に収めます。

Starts `deploy/generic/` with `--profile full --profile selfhosted`,
keeping everything on one machine.

---

## 手順 / Steps

### 1. サーバーを作る / Create the server

さくらの VPS コントロールパネル → サーバー追加

- プラン: **2GB 以上**
- OS: Ubuntu 24.04
- ディスクの初期化時に **公開鍵認証**を設定（パスワード認証は無効に
  しておく）
- 「スタートアップスクリプト」は使わず、後で `bootstrap.sh` を実行する

Set up public-key authentication during disk initialisation and leave
password authentication off. Skip the startup-script feature — run
`bootstrap.sh` by hand afterward instead.

### 2. パケットフィルタを開ける / Open the packet filter

コントロールパネル → パケットフィルタ

- 追加ポリシー: **80番 (TCP)** を許可
- 追加ポリシー: **443番 (TCP)** を許可

さくらの VPS は**既定でパケットフィルタが有効**な場合が多く、22番以外は
すべて塞がれています。ここを開けないと、サーバー内部の ufw を開けても
外から届きません。ConoHa・Vultr など他社 VPS でも、同種のコントロール
パネル側フィルタが無いか確認してください。

Sakura VPS often has the packet filter **enabled by default**, blocking
everything but port 22. Without opening it here, the instance's own ufw
rules are not enough to be reachable from outside. Check for an equivalent
control-panel-level filter on other VPS providers too.

### 3. ドメインを用意する / Point a domain at it

TLS 証明書には名前が要ります。持っていなければ DuckDNS などの無料
サブドメインで足ります。A レコードをサーバーの IP に向けてください。

### 4. 初期設定を流す / Bootstrap the instance

```bash
ssh <ユーザー名>@<サーバーのIP>
git clone <repo> && cd aipmo
./deploy/generic/bootstrap.sh
```

### 5. 設定して起動する / Configure and start

外部 DB を使わないので、`AIPMO_PG_DSN` は同じ compose 内の Postgres を
指します。

Since there is no external DB, `AIPMO_PG_DSN` points at the Postgres
service in this same compose file.

```bash
cp deploy/generic/.env.example deploy/generic/.env
python3 -c "import secrets;print(secrets.token_urlsafe(24))"
vi deploy/generic/.env
# AIPMO_PG_DSN=postgresql://aipmo:aipmo@postgres:5432/aipmo にする
touch deploy/generic/db-ca.pem   # 自前 Postgres には要らないので空でよい

cd deploy/generic && docker compose --profile full --profile selfhosted up -d --build
```

スキーマは Postgres コンテナの初回起動時に自動で読み込まれます。手動で
`psql` を流す必要はありません。

The schema loads automatically the first time the Postgres container
starts — no manual `psql` step needed.

### 6. スマホで開く / Open it on your phone

```
https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>
```

---

## 運用上の注意 / Operating notes

### バックアップ / Backups

自前 Postgres も Qdrant のデータも、**このサーバーと運命を共にします。**
外部マネージド DB と違い、自動バックアップはありません。

Both the self-hosted Postgres and Qdrant's data live and die with this one
server — unlike an external managed DB, there is no automatic backup.

```bash
# 週次で十分 / weekly is enough
docker compose exec postgres pg_dump -U aipmo aipmo | gzip > aipmo-$(date +%F).sql.gz
docker compose exec qdrant tar czf - /qdrant/storage > qdrant-$(date +%F).tgz
```

生成物はサーバー外（自分の PC・オブジェクトストレージ等）にも
定期的に持ち出してください。

Copy these off the server periodically too (your own machine, object
storage, etc.) — a backup that lives on the same disk as what it backs up
protects against less than it appears to.

### リソースの余裕 / Headroom

2GB プランなら4サービス（Caddy・aipmo・Qdrant・Postgres）が同居しても
まだ余裕があります。会議の同時処理が増えてきたら、プランを4GB以上に
上げるか、Qdrant を専用機に分けることを検討してください。

A 2GB plan still has headroom running all four services together. If
concurrent meeting processing grows, move to a 4GB+ plan or split Qdrant
onto its own machine.

---

## この構成が向かないもの / What this is not for

- **機微な会議記録** — AI 呼び出し自体はクラウドに出ます（自前推論
  サーバーに切り替えれば別）
- **高可用性が要る用途** — 単一サーバー構成なので、落ちれば全部止まります
- **急な負荷増** — VPS は無料枠と違って上限が緩いですが、無制限ではありません

Not for sensitive transcripts unless you also switch the LLM call to your
own inference server, not for anything needing high availability (one
server, one point of failure), and not built to absorb a sudden spike —
a VPS has more headroom than a free tier, not unlimited headroom.

**社内の小規模チームで、無料枠の制約無しに常用する**構成としてよく
機能します。

Works well for a small in-house team running this day to day, without the
constant juggling a free tier's limits impose.
