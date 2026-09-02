# Hetzner Cloud + 自前 PostgreSQL
# Hetzner Cloud + self-hosted PostgreSQL

Hetzner Cloud 1台に完結させる構成です。[一般的な VPS 版](DEPLOY-VPS.md)
と考え方は同じですが、Hetzner には**他のガイドと逆向きの注意点**が
あります。他社の VPS・クラウドは「既定で閉じている」ものが多く、
80/443 を開ける作業が中心でした。**Hetzner は既定で開いています。**

A single Hetzner Cloud server running everything, using the same idea as
the [generic VPS guide](DEPLOY-VPS.md). Hetzner has a gotcha that runs
**the opposite direction** from every other guide here: most other
providers ship closed by default, so the work was opening 80/443.
**Hetzner ships open by default.**

---

## 先に知っておくこと / Read this first

### 既定ではファイアウォールが無い — 閉じる作業が要る

Hetzner Cloud のサーバーには、**Cloud Firewall を明示的に作ってアタッチ
しない限り、ファイアウォールが一切適用されません。** つまり作成直後の
サーバーは、OS 側の ufw を自分で設定しない限り**すべてのポートが外から
到達可能**です。他のクラウドで詰まる理由が「開け忘れ」なのに対し、
Hetzner で詰まる理由は逆に「閉じ忘れ」です。

A Hetzner Cloud server has **no firewall applied at all** unless you
explicitly create and attach a Cloud Firewall. A freshly created server is
therefore **reachable on every port** until you configure the OS-level ufw
yourself. Where every other guide here warns about forgetting to *open*
something, Hetzner's warning is about forgetting to *close* something.

`deploy/generic/bootstrap.sh` は ufw で 22/80/443 だけを許可しますが、
**Hetzner の Cloud Firewall も別途、同じ3つに絞って作成してください。**
二重に絞ることで、片方の設定ミスがもう片方で吸収されます。

`deploy/generic/bootstrap.sh` locks ufw down to 22/80/443, but **also
create a matching Hetzner Cloud Firewall restricted to the same three
ports.** Locking down both layers means a mistake in one is caught by the
other.

### 価格性能比がよい — RAM に余裕がある

最小クラス (CX22 相当、2 vCPU / 4GB RAM) でも月4〜5ユーロ程度からと、
他の有料 VPS よりさらに安価な傾向があります。4GB あれば PostgreSQL・
Qdrant・aipmo・Caddy が余裕をもって同居できます。

Even the smallest class (CX22-equivalent, 2 vCPU / 4GB RAM) tends to run a
few euros a month — cheaper than most paid VPS alternatives. 4GB gives
PostgreSQL, Qdrant, aipmo, and Caddy comfortable headroom together.

---

## 構成 / Architecture

```
   スマホ / phone
        │  HTTPS
        ▼
  ┌───────────────────────────────────────┐
  │  Hetzner Cloud CX22 相当 (2vCPU/4GB)   │
  │  + Cloud Firewall（22/80/443のみ）      │
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
| Web 画面・実行エンジン・Qdrant・PostgreSQL | 同じサーバー | サーバー月額のみ |
| TLS 証明書 | Let's Encrypt | 無料 |
| AI | OpenAI 等 | 従量制 |

`deploy/generic/` を `--profile full --profile selfhosted` で起動します。
[一般的な VPS 版](DEPLOY-VPS.md) と同じ構成です。

Starts `deploy/generic/` with `--profile full --profile selfhosted` — the
same shape as the generic VPS guide.

---

## 手順 / Steps

### 1. サーバーを作る / Create the server

Hetzner Cloud コンソール → Add Server

- Type: **CX22** 相当（2 vCPU / 4GB）以上
- Image: Ubuntu 24.04
- SSH key: 事前に登録した公開鍵を選択（パスワード認証は使わない）

### 2. Cloud Firewall を作ってアタッチする / Create and attach a Cloud Firewall

コンソール → Firewalls → Create Firewall

- Inbound: TCP `22`（自分の IP に絞れるとなお安全）、TCP `80`、TCP `443`
  をそれぞれ Source `0.0.0.0/0`（22 は可能なら自分の IP のみ）で許可
- 作成したファイアウォールをサーバーに **Apply to** で明示的に紐付ける

**紐付けを忘れると、Firewall ルールは何の意味も持ちません。** 作成しただけ
ではサーバーに適用されません。

**Forgetting to attach it makes the rules meaningless** — creating a
firewall does not apply it to anything by itself.

### 3. ドメインを用意する / Point a domain at it

TLS 証明書には名前が要ります。持っていなければ DuckDNS などの無料
サブドメインで足ります。A レコードをサーバーの IP に向けてください。

### 4. 初期設定を流す / Bootstrap the instance

```bash
ssh root@<サーバーのIP>
git clone <repo> && cd aipmo
./deploy/generic/bootstrap.sh
```

ufw で 22/80/443 に絞ります。Cloud Firewall と合わせて二重に絞られます。

Locks ufw down to 22/80/443, doubling up with the Cloud Firewall from step 2.

### 5. 設定して起動する / Configure and start

```bash
cp deploy/generic/.env.example deploy/generic/.env
python3 -c "import secrets;print(secrets.token_urlsafe(24))"
vi deploy/generic/.env
# AIPMO_PG_DSN=postgresql://aipmo:aipmo@postgres:5432/aipmo にする
touch deploy/generic/db-ca.pem   # 自前 Postgres には要らないので空でよい

cd deploy/generic && docker compose --profile full --profile selfhosted up -d --build
```

スキーマは Postgres コンテナの初回起動時に自動で読み込まれます。

### 6. スマホで開く / Open it on your phone

```
https://<DOMAIN>/?token=<AIPMO_WEB_TOKEN>
```

---

## 運用上の注意 / Operating notes

### ファイアウォールの二重確認 / Double-check both firewall layers

不具合が起きたら、まず ufw と Cloud Firewall の**両方**を確認してください。
どちらか一方だけが許可されていても、もう一方が塞いでいれば届きません。

If connectivity breaks, check **both** ufw and the Cloud Firewall — one
being open does not help if the other is closed.

```bash
sudo ufw status
```

Cloud Firewall 側はコンソールの Firewalls 画面で確認します。

### バックアップ / Backups

[一般的な VPS 版](DEPLOY-VPS.md#バックアップ--backups) と同じです。
自前 Postgres・Qdrant のデータはこのサーバーと運命を共にするため、
外部への定期的な持ち出しが要ります。

Same as the generic VPS guide — both self-hosted Postgres and Qdrant live
and die with this one server, so copy backups off it periodically.

```bash
docker compose exec postgres pg_dump -U aipmo aipmo | gzip > aipmo-$(date +%F).sql.gz
docker compose exec qdrant tar czf - /qdrant/storage > qdrant-$(date +%F).tgz
```

---

## この構成が向かないもの / What this is not for

- **機微な会議記録** — AI 呼び出し自体はクラウドに出ます
- **高可用性が要る用途** — 単一サーバー構成です
- **ファイアウォール設定を後回しにしてよい用途** — 既定で全ポート開放
  なので、これだけは他のガイドより優先度が高い作業です

Not for sensitive transcripts, not for anything needing high availability —
and the firewall step is not optional here the way it might feel elsewhere,
since the default is wide open rather than closed.

**社内の小規模チームで、無料枠の制約無しに常用する**構成としてよく
機能します。[一般的な VPS 版](DEPLOY-VPS.md) と同じ立ち位置です。

Works well for a small in-house team running this day to day — the same
niche as the generic VPS guide.
