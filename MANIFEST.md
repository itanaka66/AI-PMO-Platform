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
| `templates/examples/` | 動くテンプレート 7本 |
| `prompts/` | 業務ごとのプロンプト 6本 |
| `docs/` | 機能ごとの手引き（日英併記） |
| `docs/guide/` | 入門ガイド 8言語 |
| `scripts/` `installer/` | インストーラ（Windows / Mac / Linux / Docker） |
| `deploy/oracle/` | Oracle Cloud Always Free + Aiven 構成 |
| `tests/` | テスト 465件 |

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

## 動かす / Running it

```bash
pip install -e ".[dev]"
pytest                                   # 465件
aipmo setup                              # 初回設定
aipmo validate templates/examples/meeting_to_tasks.yaml
aipmo serve --host 0.0.0.0               # スマホ向け画面
aipmo schedule                           # 定時実行
```

インストーラを使う場合は [INSTALL.md](INSTALL.md) を見てください。

## まだ決めていないこと / Not yet decided

**ライセンスが入っていません。** OSS として公開するなら必要です。
無料公開と Enterprise 版を両立させる方針次第で選ぶものが変わるため、
こちらでは選んでいません。

There is no LICENSE file. Publishing as open source needs one, and which one
depends on how the free and enterprise offerings are meant to coexist — so it
has been left to you rather than chosen here.

**署名されていません。** 未署名の `.exe` は Windows が警告を出します。
[INSTALL.md](INSTALL.md) に回避策はありますが、緩和であって解決ではありません。
