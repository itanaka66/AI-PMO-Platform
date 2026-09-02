# Oracle Cloud Always Free + Aiven PostgreSQL + スマホ
# Oracle Cloud Always Free + Aiven PostgreSQL + phone access

月額 0 円で、スマホから使える構成です。ただし無料枠には実際に効く制約があり、
それが設計を決めています。まず制約から書きます。

A zero-cost deployment you can drive from a phone. The free tiers have limits
that genuinely bite, and those limits shape the design — so they come first.

---

## 先に知っておくこと / Read this first

### Oracle の無料枠は 2026年6月に半減した

Always Free の Ampere A1 枠は **4 OCPU / 24GB から 2 OCPU / 12GB** に変更されました
（2026年6月15日付、事前告知なし）。上限を超えたインスタンスは 2026年8月18日以降、
終了の対象です。古い記事の「4 OCPU / 24GB」を前提に設計すると、後で止まります。

Oracle changed the Always Free Ampere A1 allowance from 4 OCPU / 24GB to
**2 OCPU / 12GB** on 15 June 2026, without an announcement. Instances above the
limit are subject to termination from 18 August 2026. Designs copied from older
write-ups that assume 4 OCPU / 24GB will eventually be shut down.

`bootstrap.sh` は起動時にこれを確認して警告します。

### Aiven の無料 PostgreSQL は 1GB、しかもアイドルで停止する

- ストレージ **1GB**（2025年5月に 5GB から変更）
- 1 CPU / 1GB RAM / 単一ノード / 高可用性なし
- **使われていないサービスは電源が落とされます**

3つ目が一番効きます。夜間に誰も使わなければ停止し、翌朝の最初の接続は
コールドスタートになります。このため Postgres アダプタは接続が切れていたら
張り直し、起床を数回待つようにしてあります。これが無いと、毎朝一度目の実行が
必ず失敗します。

Storage is **1GB** (reduced from 5GB in May 2025), on a single node with no HA,
and **an unused service gets powered off**. That last one matters most: nobody
touches it overnight, it stops, and the first connection next morning is a cold
start. The Postgres adapter therefore redials a dead connection and waits out
the wake-up. Without that, the first run of every morning fails.

### この構成にローカル LLM は入れていません

GPU なし・12GB の ARM では、7B 量子化モデルで概ね 5〜8 tok/s です。
1時間の会議 Transcript は数万トークンあるので、議事録1本に数十分かかります。
無料枠に収まっても、待てないものは使われません。

**AI はクラウドか、別途用意した推論サーバーを使ってください。**

There is no Ollama in this stack. A quantised 7B model on GPU-less 12GB ARM
runs at roughly 5-8 tok/s; an hour of transcript is tens of thousands of tokens,
so one set of minutes would take tens of minutes. It fits the free tier and
still nobody would use it. Use cloud AI, or an inference server you provide.

---

## 構成 / Architecture

```
   スマホ / phone
        │  HTTPS
        ▼
  ┌─────────────────────────────────────────┐
  │  Oracle Cloud Always Free               │
  │  Ampere A1 · arm64 · 2 OCPU / 12GB      │
  │                                         │
  │   Caddy ──── TLS 終端・自動更新          │
  │     │                                   │
  │   aipmo ─── Web 画面 + 実行エンジン       │
  │     │                                   │
  │   Qdrant ── ベクトル検索（3GB 上限）      │
  └─────┼───────────────────┼───────────────┘
        │                   │
        ▼                   ▼
   Aiven PostgreSQL     AI サーバー
   1GB · 外部           OpenAI 互換・任意
```

| | 置き場所 | 費用 |
|---|---|---|
| Web 画面・実行エンジン | Oracle A1 | 無料 |
| Qdrant | Oracle A1（同居） | 無料 |
| PostgreSQL | Aiven 外部 | 無料（1GB） |
| TLS 証明書 | Let's Encrypt | 無料 |
| AI | OpenAI 等 | 従量制 |

**Qdrant を Oracle 側に同居させる理由** — Aiven の 1GB に会議の記録や埋め込みは
入りません。ブロックストレージは 200GB 無料なので、量が出るものはそちらに置きます。
PostgreSQL には実行履歴とナレッジ候補という「小さくて構造化されたもの」を置く前提です。
スキーマと `queries.yaml` はありますが、エンジンからの自動書き込みは未接続です。
画面の履歴はメモリ上の直近 50 件です。

Qdrant lives on the Oracle box because transcripts and embeddings will not fit
in Aiven's 1GB, while Oracle's block storage is 200GB free. PostgreSQL is meant
to hold the small structured things: run history and knowledge candidates.
The schema and named queries exist; the engine does not write them yet. The
web UI keeps the last 50 runs in memory.

---

## 手順 / Steps

### 1. インスタンスを作る / Create the instance

OCI コンソール → Compute → Instance → Create

- Shape: **VM.Standard.A1.Flex**、**2 OCPU / 12GB**（上限どおりに)
- Image: Ubuntu 24.04 (aarch64)
- 公開 IP を割り当てる / assign a public IP

> **「Out of host capacity」が出る場合**
> A1 は人気があり、リージョンによっては数時間から数日空きません。
> 時間をおいて再試行するか、別リージョンを試してください。
>
> A1 capacity is contended; some regions take hours or days. Retry later or
> pick a different region.

### 2. ドメインを用意する / Point a domain at it

TLS 証明書には名前が要ります。持っていなければ DuckDNS などの無料サブドメインで
足ります。A レコードをインスタンスの公開 IP に向けてください。

A certificate needs a name. A free subdomain from DuckDNS is enough. Point an
A record at the instance's public IP.

### 3. 初期設定を流す / Bootstrap the instance

```bash
ssh ubuntu@<公開IP>
git clone <repo> && cd aipmo
./deploy/oracle/bootstrap.sh
```

Docker の導入、スワップ 4GB の作成、iptables の 80/443 開放を行います。

> **ここが一番詰まります。**
> Oracle の Ubuntu イメージは iptables で 22 番以外を落とします。
> OCI コンソールのセキュリティリストを開けても、**インスタンス側の
> iptables を開けないと届きません**。両方必要です。
> `bootstrap.sh` は iptables 側だけを行うので、コンソール側は手動です。
>
> Oracle's Ubuntu images drop everything but port 22 in iptables. Opening the
> console Security List is **not sufficient on its own** — both are required.
> The script handles iptables; the console rule is yours to add.

OCI コンソール → VCN → セキュリティリスト → イングレス規則に
`0.0.0.0/0` の TCP 80 と 443 を追加してください。

### 4. Aiven を用意する / Provision Aiven

1. [aiven.io](https://aiven.io) で PostgreSQL の Free プランを作成
2. **CA 証明書をダウンロード**し、`deploy/oracle/aiven-ca.pem` に置く
3. 接続文字列をコピー

```bash
psql "$AIPMO_PG_DSN" -f sql/schema.sql
```

> DSN には `sslmode=verify-full` と `sslrootcert` を必ず付けてください。
> `require` だけでは証明書を検証しないので、中間者攻撃を防げません。
> 外部のマネージド DB に公衆網経由でつなぐ以上、ここは省略できません。
>
> Keep `sslmode=verify-full` and `sslrootcert` in the DSN. `require` alone
> encrypts but does not verify the certificate, so it does not stop an
> in-path attacker — and this connection crosses the public internet.

### 5. 設定して起動する / Configure and start

```bash
cp deploy/oracle/.env.example deploy/oracle/.env
python3 -c "import secrets;print(secrets.token_urlsafe(24))"   # アクセスキー
vi deploy/oracle/.env

cd deploy/oracle && docker compose up -d --build
```

初回のビルドは arm64 で 5〜10 分ほどかかります。
The first build takes 5-10 minutes on arm64.

### 6. スマホで開く / Open it on your phone

```
https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>
```

キーは初回アクセスで Cookie に移り、アドレス欄から消えます。
ホーム画面に追加すると、アプリのように使えます。

The key moves into a cookie on first open. Add it to the home screen.

---

## 運用上の注意 / Operating notes

### 容量 / Capacity

Aiven の 1GB は、実行履歴だけを入れる分には長く保ちますが、
**ステップ出力を丸ごと保存すると数週間で埋まります**。
エンジンが書き込むようになったら、議事録の全文を `step_results.output` に
入れないでください。長い出力は Qdrant 側に置き、PostgreSQL には参照だけを残します。

Aiven's 1GB lasts a long time for run metadata, but **storing whole step
outputs will fill it within weeks**. Once the engine writes history, keep
full transcripts and minutes out of `step_results.output`; put the bulk in
Qdrant and keep a reference in PostgreSQL.

使用量の確認 / Check usage:

```sql
SELECT pg_size_pretty(pg_database_size(current_database()));
```

### バックアップ / Backups

Aiven の無料プランはバックアップが限られ、Qdrant のデータは
インスタンスと運命を共にします。**Oracle は無料インスタンスを
再利用のために停止・削除することがあります。**

```bash
# 週次で十分 / weekly is enough
docker compose exec qdrant tar czf - /qdrant/storage > qdrant-$(date +%F).tgz
pg_dump "$AIPMO_PG_DSN" | gzip > aipmo-$(date +%F).sql.gz
```

### アイドル停止 / Idle power-off

Aiven は使われないサービスを止めます。止まった直後の実行は数秒から
数十秒かかりますが、アダプタが待つので失敗はしません。
どうしても避けたい場合は Developer tier（月 $5）で停止しなくなります。

Aiven stops unused services. The first run after a stop takes seconds to tens
of seconds; the adapter waits it out rather than failing. The $5/month
Developer tier does not power off if that matters.

### 無料枠は変わる / Free tiers change

この文書の数値は 2026年8月時点のものです。Oracle は告知なく半減させました。
Aiven は 5GB を 1GB にしました。**設計を数値に固定しないでください。**

The numbers here are as of August 2026. Oracle halved its allowance without
notice; Aiven cut storage from 5GB to 1GB. Do not hard-code a free tier's
current limits into your assumptions.

---

## この構成が向かないもの / What this is not for

- **本番の業務データ** — 無料プランにバックアップと可用性の保証はありません
- **機微な会議記録** — AI はクラウドに出ます。社外に出せないなら
  GPU のある機械を用意し、`base_url` をそちらに向けてください
- **多人数の同時利用** — 2 OCPU / 1GB DB では数人が上限です

Not for production business data (no backup or availability guarantees), not
for sensitive transcripts (the AI call leaves the network — point `base_url` at
your own GPU machine instead), and not for many concurrent users.

**試す・小さく回す・型を作る**にはよく機能します。
It works well for trying things, running small, and building the templates.
