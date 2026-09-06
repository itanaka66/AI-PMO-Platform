# 接続先の選び方 — PostgreSQL・Ollama・Qdrant
# Choosing targets — PostgreSQL, Ollama, and Qdrant

[deploy/generic/](../deploy/generic/)（GCP・Azure・AWS・VPS・Hetzner の各
ガイドが共通で使う構成）で、PostgreSQL・Ollama・Qdrant のそれぞれを
「このマシンの compose 内で自前に立てる（内部）」か「外部のものに
接続する（外部）」かを選ぶ手順です。3つは互いに独立に選べます
——たとえば Postgres は外部、Ollama だけ自前、という組み合わせも
そのまま成立します。

Step-by-step for choosing, independently for PostgreSQL, Ollama, and
Qdrant, between running it inside this same compose file (internal) or
pointing at an external one — for [deploy/generic/](../deploy/generic/),
the setup shared by the GCP/Azure/AWS/VPS/Hetzner guides. The three
choices are independent of each other; "external Postgres, self-hosted
Ollama only" is a perfectly valid combination.

外部を選んだものは、コンテナも Docker の named volume も一切作られません
——ローカルにデータは残りません。

Whichever you choose external for gets neither a container nor a Docker
named volume — nothing local is left behind.

---

## PostgreSQL

### 内部（この compose 内で自前に立てる）/ Internal (self-hosted in this compose)

1. `deploy/generic/.env` に既定の接続文字列を書く（コンテナ名 `postgres`
   宛て）:
   ```
   AIPMO_PG_DSN=postgresql://aipmo:aipmo@postgres:5432/aipmo
   ```
2. `db-ca.pem` は使わないので空ファイルで足りる:
   ```bash
   touch deploy/generic/db-ca.pem
   ```
3. `--profile selfhosted` を付けて起動する:
   ```bash
   docker compose --profile selfhosted up -d --build
   ```
4. スキーマは Postgres コンテナの**初回起動時に自動で読み込まれる**
   （`sql/schema.sql` が `docker-entrypoint-initdb.d` にマウントされて
   いるため）。手動で `psql` を流す必要はない。
5. 確認する:
   ```bash
   docker compose run --rm aipmo doctor
   ```

Point `.env`'s `AIPMO_PG_DSN` at the container name (`postgres`), touch
an empty `db-ca.pem` (not needed here), start with `--profile
selfhosted`. The schema loads automatically the first time the container
starts (`sql/schema.sql` is mounted into `docker-entrypoint-initdb.d`) —
no manual `psql` step. Verify with `aipmo doctor`.

### 外部（Aiven・RDS などマネージド DB）/ External (a managed DB such as Aiven or RDS)

1. 外部サービス側で DB を用意し、接続文字列と（要る場合は）CA 証明書を
   入手する。
2. `deploy/generic/.env` にその接続文字列を書く。**`sslmode=verify-full`
   を必ず付ける**——`sslmode=require` は暗号化はするが証明書検証をしない:
   ```
   AIPMO_PG_DSN=postgresql://user:PASSWORD@host.aivencloud.com:PORT/defaultdb?sslmode=verify-full&sslrootcert=/app/db-ca.pem
   ```
3. ダウンロードした CA 証明書を配置する:
   ```bash
   cp /path/to/downloaded-ca.pem deploy/generic/db-ca.pem
   ```
4. `--profile selfhosted` は**付けない**——付けると要らない自前
   Postgres まで立ってしまう。
5. **スキーマは自動で読み込まれないので、初回だけ手動で流す:**
   ```bash
   psql "$AIPMO_PG_DSN" -f sql/schema.sql
   ```
6. 起動して確認する:
   ```bash
   docker compose up -d --build
   docker compose run --rm aipmo doctor
   ```

Provision the DB externally, get its connection string (and CA cert if
needed). Keep `sslmode=verify-full` — `sslmode=require` alone encrypts
but does not verify the certificate. Place the CA cert at `db-ca.pem`.
**Do not** add `--profile selfhosted` (it would also start an unwanted
local Postgres). **The schema does not load automatically here — run
`psql "$AIPMO_PG_DSN" -f sql/schema.sql` once, by hand,** before or
after the first `docker compose up`. Then verify with `aipmo doctor`.

---

## Ollama

### 内部（この compose 内で自前に立てる）/ Internal (self-hosted in this compose)

1. `deploy/generic/.env`:
   ```
   OLLAMA_HOST=http://ollama:11434
   ```
2. `deploy/generic/config.yaml` の `llm.default` / `llm.fast` を
   ollama ブロックに切り替える（`host` は省略してよい——省略すると
   `.env` の `OLLAMA_HOST` をそのまま使う）:
   ```yaml
   llm:
     default:
       provider: ollama
       model: qwen2.5:14b
     fast:
       provider: ollama
       model: qwen2.5:7b
   ```
3. `--profile ollama` を付けて起動する:
   ```bash
   docker compose --profile ollama up -d --build
   ```
4. モデルを取得する（数 GB のダウンロード）:
   ```bash
   docker compose exec ollama ollama pull qwen2.5:14b
   ```
5. 確認する:
   ```bash
   docker compose run --rm aipmo doctor
   ```

Set `.env`'s `OLLAMA_HOST` to the container name, switch
`config.yaml`'s `llm` block to `provider: ollama` (omit `host` — it falls
back to `.env`'s `OLLAMA_HOST`), start with `--profile ollama`, pull the
model, then verify.

### 外部（LAN 内の推論機など）/ External (e.g. your own inference box on the LAN)

1. 外部の Ollama サーバーを用意し、`11434` ポートがこのインスタンスから
   到達できることを確認する。
2. `deploy/generic/.env`:
   ```
   OLLAMA_HOST=http://your-ollama-host:11434
   ```
3. `config.yaml` の `llm` ブロックは内部の場合と**同じ**ように
   `provider: ollama` に切り替える（`host` は省略のままでよい）。
4. `--profile ollama` は**付けない**——付けると要らない自前コンテナまで
   立ってしまう。
5. モデルはその外部サーバー側で事前に取得しておく（このインスタンスから
   は取得しない）。
6. 起動して確認する:
   ```bash
   docker compose up -d --build
   docker compose run --rm aipmo doctor
   ```

Make sure the external Ollama server's port 11434 is reachable from this
instance, point `OLLAMA_HOST` at it, switch `config.yaml`'s `llm` block
the same way as the internal case (still `provider: ollama`, `host`
still omitted). **Do not** add `--profile ollama`. Pull the models on
that external server yourself — not from this instance. Then verify.

---

## Qdrant（ナレッジ機能・任意）/ Qdrant (the knowledge features, optional)

### 内部（この compose 内で自前に立てる）/ Internal (self-hosted in this compose)

1. `deploy/generic/.env`:
   ```
   QDRANT_URL=http://qdrant:6333
   QDRANT_API_KEY=
   ```
2. `config.yaml` の `qdrant:` ブロックのコメントを外す:
   ```yaml
   qdrant:
     url: ${QDRANT_URL}
     api_key: ${QDRANT_API_KEY}
     public_collection: public_pmo_knowledge
     embedding:
       provider: openai
       model: text-embedding-3-small
       dimension: 1536
   ```
3. `--profile full` を付けて起動する:
   ```bash
   docker compose --profile full up -d --build
   ```
4. 確認する:
   ```bash
   docker compose run --rm aipmo doctor
   ```

Set `.env`'s `QDRANT_URL` to the container name (no API key needed),
uncomment `config.yaml`'s `qdrant:` block, start with `--profile full`,
then verify.

### 外部（Qdrant Cloud など）/ External (e.g. Qdrant Cloud)

1. Qdrant Cloud（または他のマネージド Qdrant）でクラスタを作り、URL と
   API key を控える。1GB クラスの無料枠インスタンスでも、こちらは
   compose の RAM を消費しないのでそのまま使える。
2. `deploy/generic/.env`:
   ```
   QDRANT_URL=https://xxxx.us-east-1-0.aws.cloud.qdrant.io
   QDRANT_API_KEY=...
   ```
3. `config.yaml` の `qdrant:` ブロックのコメントを外す——内部の場合と
   **書き方は変わらない**（`url`・`api_key` とも `.env` の値をそのまま
   参照するだけ）。
4. `--profile full` は**付けない**——付けると要らない自前コンテナまで
   立ってしまう。
5. 起動して確認する:
   ```bash
   docker compose up -d --build
   docker compose run --rm aipmo doctor
   ```

Create a Qdrant Cloud cluster (or other managed Qdrant), note its URL
and API key — this works even on a 1GB-class free-tier instance, since
it costs the compose no RAM. Set both in `.env`, uncomment `config.yaml`'s
`qdrant:` block (unchanged from the internal case). **Do not** add
`--profile full`. Then verify.

### 使わない / Not using it

1. `deploy/generic/.env` の `QDRANT_URL` と `QDRANT_API_KEY` を両方とも
   空のままにする。
2. `config.yaml` の `qdrant:` ブロックはコメントアウトのまま触らない。
3. `--profile full` は付けない。

ナレッジ機能（`vector_store` アダプタ）だけが使えなくなり、他の機能には
影響しない。

Leave both `.env` vars empty and `config.yaml`'s `qdrant:` block
commented out, and don't add `--profile full`. Only the knowledge
features (the `vector_store` adapter) go unused — nothing else is
affected.

---

## 組み合わせ早見表 / Combination cheat sheet

選んだものだけ `--profile` を足す。外部にしたものは何も付けない。

Add a profile only for what you're self-hosting; add nothing for what's
external.

| PostgreSQL | Ollama | Qdrant | コマンド / Command |
|---|---|---|---|
| 外部 / external | 外部 / external | 使わない / none | `docker compose up -d --build` |
| 内部 / internal | 外部 / external | 使わない / none | `docker compose --profile selfhosted up -d --build` |
| 外部 / external | 内部 / internal | 外部 / external | `docker compose --profile ollama up -d --build` |
| 内部 / internal | 内部 / internal | 内部 / internal | `docker compose --profile full --profile selfhosted --profile ollama up -d --build` |

すべて内部にする組み合わせは、RAM に余裕がある有料 VPS 向け
（[docs/DEPLOY-VPS.md](DEPLOY-VPS.md)・[docs/DEPLOY-HETZNER.md](DEPLOY-HETZNER.md)
を参照）。1GB クラスの無料枠では PostgreSQL・Ollama は外部が前提
（[docs/DEPLOY-GCP.md](DEPLOY-GCP.md)・[docs/DEPLOY-AZURE.md](DEPLOY-AZURE.md)・
[docs/DEPLOY-AWS.md](DEPLOY-AWS.md) を参照）だが、Qdrant だけは外部の
Qdrant Cloud を使えば無料枠のままでも足りる。

Self-hosting everything is for a paid VPS with RAM to spare (see
DEPLOY-VPS.md / DEPLOY-HETZNER.md). The 1GB-class free tiers assume
external PostgreSQL and Ollama (see DEPLOY-GCP.md / DEPLOY-AZURE.md /
DEPLOY-AWS.md) — but Qdrant alone still fits even there, via an external
Qdrant Cloud plan.
