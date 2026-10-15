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
| `aipmo/adapters/` | Teams / Jira / Jira Agile / GitHub Projects / Plane / OpenProject / Azure DevOps / Slack / PostgreSQL / ベクトルストア5種 |
| `aipmo/llm/` | 提供元プリセット（OpenAI・Gemini・Groq・OpenRouter・ローカル） |
| `aipmo/web/` | スマホ向け画面（権限分離つき、WBS再計画の承認 API を含む） |
| `templates/examples/` | 汎用テンプレート 23本（会議・WBS・Project Digital Twin・ナレッジのレビュー/自己学習サイクルなど） |
| `templates/roles/` | 役割特化エージェント（開発・テスト・調査・文書・営業）5本 |
| `templates/industries/` | 建設・マーケティング・法務等、11業界向けテンプレート |
| `prompts/` | 業務ごとのプロンプト 24本 |
| `docs/` | 機能ごとの手引き（日英併記） |
| `docs/guide/` | 入門ガイド 8言語 |
| `scripts/` `installer/` | インストーラ（Windows / Mac / Linux / Docker） |
| `deploy/oracle/` | Oracle Cloud Always Free + Aiven 構成 |
| `deploy/generic/` | GCP / Azure / AWS / VPS 共通のデプロイ構成 |
| `tests/` | テスト 1899件 |

## 対応している PMO 業務 / What it automates

`templates/examples/` の23本（`templates/roles/` の役割特化エージェント5本・
`templates/industries/` の11業界向けテンプレートは別掲。いずれも
[README.md](README.md) に詳しい説明がある）:

| テンプレート | 内容 |
|---|---|
| `meeting_to_tasks` | 会議 → 議事録 → TODO → Jira 起票 → Slack 通知 |
| `meeting_minutes` | 会議 → 議事録（会議ID直指定） |
| `meeting_task_update` | 会議の内容から既存課題を更新（確信度で選別） |
| `overdue_chase` | 期限超過の担当者へ個別に催促 |
| `overdue_triage` | 遅延状況をエージェントが調査して報告 |
| `portfolio_investigation` | 複数プロジェクトを独立したサブエージェントで並行調査 |
| `sprint_health` | スプリントの状況確認（問題があるときだけ通知） |
| `self_development` | PMO AI 自身の開発を、自身の WBS（wbs/aipmo.yaml）で週次管理 |
| `wbs_from_meeting` | 会議の決定事項から WBS の草案 |
| `wbs_risk_forecast` | WBS の遅延予測・ドリフト検出、承認待ち提案の記録 |
| `portfolio_risk_forecast` | 複数 WBS を横断して遅延リスクを決定論的に採点、危険なものだけ週次報告 |
| `wbs_replan` | WBS の遅延を踏まえ、AI が実際の再計画案を考え承認待ちの提案として記録 |
| `wbs_replan_jira` | wbs_replan と同じだが、タスク一覧を実際の Jira スプリントから取得 |
| `wbs_file_replan` | wbs_replan と同じ判定だが、WBS ファイルへ反映できる決まった形の変更を提案 |
| `wbs_replan_options` | wbs_replan と同じだが、依存関係・クリティカルパスを踏まえた2つの代替案（A/B）を記録 |
| `wbs_proposal_cleanup` | 承認待ちのまま放置された WBS 再計画提案を定期的に無効化 |
| `digital_twin_sync` | Jira から Project Digital Twin（プロジェクト・WBS・タスク）を毎日同期 |
| `digital_twin_diagnose` | 5軸を決定論的に採点し、AI が所見と推奨アクションを記述 |
| `model_comparison` | 同じプロンプトを複数の AI 提供元に同時投稿し、書きぶりを比較 |
| `parallel_notify` | 独立した通知を同時に送り、実行時間を縮める |
| `crawl_watch` | 外部サイトの1ページを取得し、見出し・リンク・メタデータを Slack へ通知 |
| `generalize_knowledge` | 社内知見を匿名化・一般化し、レビュー待ちの候補として提出 |
| `self_learning_cycle` | ローカル LLM で架空の課題を作り自己判断、別モデルで検証してナレッジ候補として提出（デモ・練習用） |
| `construction/site_meeting` | 工程会議 → 議事録・是正起票・安全指摘の即時通知 |
| `marketing/campaign_check` | キャンペーン進行確認（承認待ちを分けて扱う） |

## 動かす / Running it

```bash
pip install -e ".[dev]"
pytest                                   # 1899件
aipmo setup                              # 初回設定
aipmo validate templates/examples/meeting_to_tasks.yaml
aipmo serve --host 0.0.0.0               # スマホ向け画面
aipmo schedule                           # 定時実行
```

インストーラを使う場合は [INSTALL.md](INSTALL.md) を見てください。

## ライセンス / License

GPL-3.0 License（[LICENSE](LICENSE)）— Copyright (C) 2026 株式会社エージーネディア。

**このリポジトリにあるものは、すべて無料です。** 商用利用・改変・再配布が
可能で（コピーレフト——再配布・改変版も GPL-3.0 で公開）、テンプレートと
プロンプトも同じ条件です。詳細は [NOTICE.md](NOTICE.md)。

GPL-3.0. Commercial use, modification and redistribution are permitted
(copyleft — a redistributed or modified copy must also be GPL-3.0), and that
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
