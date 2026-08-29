# ファイル一覧 / What is in here

AI-PMO Platform — テンプレート駆動の PMO 業務自動化基盤
A template-driven runtime for automating PMO work.

まず読むもの / Start here:
[docs/guide/README.md](docs/guide/README.md) — 8言語の入門ガイド

---

## 中身 / Contents

| 場所 | 内容 |
|---|---|
| `aipmo/dsl/` | テンプレート言語（スキーマ・読み込み・式評価） |
| `aipmo/engine/` | 実行エンジン、エージェント、cron、スケジューラ |
| `aipmo/adapters/` | Teams / Jira / Jira Agile / Slack / PostgreSQL / Qdrant |
| `aipmo/llm/` | 提供元プリセット（OpenAI・Gemini・Groq・OpenRouter・ローカル） |
| `aipmo/web/` | スマホ向け画面（権限分離つき） |
| `templates/examples/` | ソフトウェア開発向けテンプレート 7本 |
| `templates/industries/` | 建設・マーケティング向けテンプレート |
| `prompts/` | 業務ごとのプロンプト 8本 |
| `docs/` | 機能ごとの手引き（日英併記） |
| `docs/guide/` | 入門ガイド 8言語 |
| `scripts/` `installer/` | インストーラ（Windows / Mac / Linux / Docker） |
| `deploy/oracle/` | Oracle Cloud Always Free + Aiven 構成 |
| `tests/` | テスト 603件 |

## 対応している PMO 業務 / What it automates

| テンプレート | 内容 |
|---|---|
| `meeting_to_tasks` | 会議 → 議事録 → TODO → Jira 起票 → Slack 通知 |
| `meeting_minutes` | 会議 → 議事録（会議ID直指定） |
| `meeting_task_update` | 会議の内容から既存課題を更新（確信度で選別） |
| `overdue_chase` | 期限超過の担当者へ個別に催促 |
| `overdue_triage` | 遅延状況をエージェントが調査して報告 |
| `sprint_health` | スプリントの状況確認（問題があるときだけ通知） |
| `wbs_from_meeting` | 会議の決定事項から WBS の草案 |
| `construction/site_meeting` | 工程会議 → 議事録・是正起票・安全指摘の即時通知 |
| `marketing/campaign_check` | キャンペーン進行確認（承認待ちを分けて扱う） |

## 動かす / Running it

```bash
pip install -e ".[dev]"
pytest                                   # 603件
aipmo setup                              # 初回設定
aipmo validate templates/examples/meeting_to_tasks.yaml
aipmo serve --host 0.0.0.0               # スマホ向け画面
aipmo schedule                           # 定時実行
```

インストーラを使う場合は [INSTALL.md](INSTALL.md) を見てください。

## ライセンス / License

MIT License（[LICENSE](LICENSE)）— Copyright (c) 2026 株式会社エージーネディア。

**このリポジトリにあるものは、すべて無料です。** 商用利用・改変・再配布が
可能で、テンプレートとプロンプトも同じ条件です。詳細は [NOTICE.md](NOTICE.md)。

MIT. Commercial use, modification and redistribution are permitted, and that
includes the templates and prompts.

## まだ決めていないこと / Not yet decided

**証明書がありません。** コード署名の仕組み自体は配線済みで、証明書さえ
用意すれば `aipmo.exe` とインストーラの両方に自動で署名されます。
証明書の購入自体はこのリポジトリの範囲外です。詳しくは
[INSTALL.md](INSTALL.md)。それまでは、`.exe` は
これまでどおり未署名のままです。

**There is no certificate yet.** The signing mechanism itself is wired up —
provide a certificate and both `aipmo.exe` and the installer are signed
automatically. Purchasing one is outside what this repository can do; see
[INSTALL.md](INSTALL.md). Until then, the `.exe`
stays unsigned exactly as before.
