# Web クローラ / Web crawler

外部サイトの1ページを取得し、その HTML からテキスト・リンク・
メタデータを抜き出すための、読み取り専用アダプタ。認証情報は要らない。

A read-only adapter that fetches a single external page and pulls text,
links, and metadata out of its HTML. No credentials required.

---

## 導入 / Installation

HTML の解析に beautifulsoup4 を使う（`html.parser` バックエンドのみで、
追加のコンパイル済み依存は無い）。追加の extra として分けてあるので、
使わない場合は入れる必要がない。

HTML parsing uses beautifulsoup4 (the `html.parser` backend only — no
compiled dependency beyond it). It ships as its own extra, so installs that
don't need it stay lean.

```bash
pip install "aipmo[crawler]"
```

`scripts/install.sh` / `scripts/install.ps1` 経由でインストールした場合は、
インストール後に追加してください:

If you installed via `scripts/install.sh` / `scripts/install.ps1`, add it
afterwards:

```bash
~/.local/share/ai-pmo/.venv/bin/pip install beautifulsoup4   # macOS / Linux
```

```powershell
& "$env:LOCALAPPDATA\AI-PMO\.venv\Scripts\pip.exe" install beautifulsoup4   # Windows
```

---

## 設定 / Configuration

他の実アダプタと同じく、`config.yaml` に明示的に書かないと有効にならない
（risk_forecast と同じ理由 — 認証情報は要らないが、外部サイトに実際に
到達するアダプタなので、有効かどうかを config から読み取れるようにする）。

Like every other real adapter, this must be explicitly opted into via
`config.yaml` (the same reasoning as `risk_forecast` — no credentials
needed, but it does reach real external sites, so whether it's active
should be visible from config alone).

```yaml
adapters:
  mode: real
  crawler:
    timeout: 30        # 秒 / seconds, default 30
    max_links: 100      # extract_links が返す上限 / cap on extract_links, default 100
    user_agent: "AI-PMO-Crawler/1.0"   # 既定 / default
```

---

## アクション / Actions

すべて読み取り専用（`writes` を持つアクションは無い）。

All read-only — no action carries `writes=True`.

| アクション / Action | 引数 / Args | 返り値 / Returns |
|---|---|---|
| `fetch_page` | `url`, `headers`(省略可 / optional) | `url`, `status`, `content`（生の HTML / raw HTML） |
| `extract_text` | `html`, `tag`(既定 `body` / default `body`) | `tag`, `count`, `texts`（指定タグ内のテキスト一覧 / text inside each matching tag） |
| `extract_links` | `html`, `base_url`(省略可 / optional) | `count`, `links`（`url`・`text` の配列 / array of `url`, `text`） |
| `extract_metadata` | `html` | `title`, `description`, `og_image`, `og_type` |

**`fetch_page` は HTTP エラーを例外にしない。** 404 のようなエラーは
`status` に入れて返す — テンプレート側で `when` により許容したい場合が
あるため。接続自体ができない場合（DNS 失敗など）だけ例外になる。

**`fetch_page` does not raise on an HTTP error.** A 404, for example, comes
back in `status` rather than an exception, since a template may want to
tolerate it via `when`. Only a connection failure (DNS, refused, etc.)
raises.

---

## テンプレート例 / Example template

[templates/examples/crawl_watch.yaml](../templates/examples/crawl_watch.yaml)
が、1ページを取得して見出し・リンク・メタデータを Slack へ通知する完全な例。

[templates/examples/crawl_watch.yaml](../templates/examples/crawl_watch.yaml)
is a complete example that fetches a page and posts its headline, links,
and metadata to Slack.

```bash
aipmo validate templates/examples/crawl_watch.yaml
aipmo run templates/examples/crawl_watch.yaml --param url=https://example.com/news
```

---

## 定期的にクロールする / Crawling on a schedule

テンプレートの `trigger` を `schedule:...` にすれば、
[docs/SCHEDULER.md](SCHEDULER.md) の `aipmo schedule` が定時実行する。
専用の常駐プロセスは不要——既存のスケジューラと同じ仕組みに乗る。

Set the template's `trigger` to `schedule:...` and
[docs/SCHEDULER.md](SCHEDULER.md)'s `aipmo schedule` runs it on that cron.
No dedicated daemon is needed — it rides the same scheduler as every other
template.

```yaml
trigger: "schedule:0 * * * *"   # 毎時 / hourly
```

Docker では `scheduler` コンテナが既にこれを行っている
（[docker-compose.yml](../docker-compose.yml)）。裸の Linux ホストで
常駐させたい場合は、`aipmo schedule` を systemd で自動起動させる:

In Docker, the `scheduler` container already does this
([docker-compose.yml](../docker-compose.yml)). To keep `aipmo schedule`
running on a bare Linux host, start it under systemd:

```ini
# /etc/systemd/system/aipmo-scheduler.service
[Unit]
Description=AI-PMO Platform scheduler
After=network.target

[Service]
Type=simple
User=aipmo
WorkingDirectory=/home/aipmo/AI-PMO-Platform
ExecStart=/home/aipmo/.local/bin/aipmo schedule
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aipmo-scheduler.service
sudo journalctl -u aipmo-scheduler.service -f
```

`/home/aipmo/.local/bin/aipmo` は [scripts/install.sh](../scripts/install.sh)
が作るランチャ。別の場所に入れた場合はそちらのパスに読み替えてください。

`/home/aipmo/.local/bin/aipmo` is the launcher
[scripts/install.sh](../scripts/install.sh) creates. Adjust the path if you
installed elsewhere.
