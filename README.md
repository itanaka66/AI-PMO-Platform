# AI-PMO Platform

[![Tests](https://github.com/itanaka66/AI-PMO-Platform/actions/workflows/tests.yml/badge.svg)](https://github.com/itanaka66/AI-PMO-Platform/actions/workflows/tests.yml)

> **PMO業務を完全自動化。AIが議事録を書き、TODOを作り、遅延を催促する。**

あなたの組織の Project Management Office（PMO）業務にかかっている時間を大幅削減します。
会議終了→議事録作成→TODO抽出→Jira起票→Slack通知まで、**人間を介さず自動実行**。

---

## 🎯 何が解決するのか

### 現状の問題
- 📌 **会議議事録**：毎回30分〜1時間の手作業
- 🎯 **TODO管理**：議事録から手動で課題を起票（記述ズレで手戻り多発）
- ⏰ **遅延追跡**：毎朝「期限切れました」メールを手で作成
- 🔄 **スプリント報告**：毎日「ポイント合計して、進捗を文言で説明」

**→ この全部を AI が勝手にやってくれます**

### AI-PMO でできるようになること
| 業務 | 従来 | AI-PMO |
|------|------|--------|
| **議事録作成** | 手作業 40分 | 自動 1分 |
| **TODO起票** | 手作業 30分 | 自動 1分 |
| **遅延催促** | 手作業毎日 | 毎朝自動 |
| **スプリント報告** | 手作業 20分 | 自動 1分 |

---

## 🚀 動作実績（実装済み機能）

### ソフトウェア開発チーム向け
✅ **Teams 会議 → 自動議事録生成**
- 会議の Transcript から自動的に日本語議事録を生成
- 決定事項と Action Items を自動抽出

✅ **議事録 → Jira 課題 自動起票**
- 抽出された TODO を自動的に Jira に課題として登録
- 担当者・期限を自動割り当て

✅ **期限超過 → Slack 自動催促**
- 毎日朝 9 時に遅延アイテムを自動検出
- 担当者ごとに個別催促メッセージを送信

✅ **スプリント状況 → Slack 自動報告**
- バーンダウン、見積もり未設定課題を検出
- 朝会の前に自動で #pmo に投稿

✅ **WBS 草案 → Slack 自動投稿**
- 会議内容からフェーズごとの WBS を自動生成
- 想定と未知を明確化

### 建設・施工管理向け
✅ **安全指摘 → 即時通知**
- 現場会議から安全に関する記述を即時抽出
- #safety に優先度付けして送信

✅ **進捗レポート → 自動作成**
- 月次進捗を営業秘密を除いて自動集計

### マーケティング向け
✅ **キャンペーン進捗 → 自動追跡**
- 承認待ち と 遅延 を区別して報告
- アクションが必要な件だけをハイライト

---

## 💰 導入メリット

### 時間削減
- **PMO 1人で月 60 時間削減**（完全自動化時）
- 月 20 万円相当の人件費削減

### 品質向上
- **議事録の書き漏れ 0 に**：AI は会議記録から 100% 抽出
- **TODO 二重登録の消滅**：自動化で手作業エラーなし
- **遅延の見逃しなし**：毎日自動チェック

### 導入コスト
- **完全無料版で開始可能**
- インストール 5 分、セットアップ 10 分
- 既存の Teams / Jira / Slack がそのまま使える

---

## 🏃 3ステップで開始

### 1️⃣ インストール（5分）

```bash
# Windows
scripts\install.bat

# macOS / Linux
./scripts/install.sh

# Docker
./scripts/install-docker.sh
```

### 2️⃣ セットアップウィザード（10分）

```bash
aipmo setup
```

対話形式で以下を選択：
- 🤖 AI 提供元（OpenAI / Gemini / Groq / ローカル Ollama）
- 🗂️ データ層（PostgreSQL / Qdrant）
- 🏢 組織名・APIキー

### 3️⃣ テンプレート選択 & 実行

```bash
# スマホで Web UI を見ながら実行
aipmo serve --host 0.0.0.0

# 定時実行（毎朝 9 時）
aipmo schedule
```

---

## 📋 テンプレート一覧（無料版）

| テンプレート | 所要時間 | AI提供元 | 出力先 |
|-------------|--------|--------|-------|
| 議事録生成 | 1分 | Any | Slack |
| TODO抽出・起票 | 1分 | Any | Jira |
| 遅延催促 | 1分 | Any | Slack |
| WBS生成 | 3分 | Any | Slack |
| スプリント報告 | 1分 | Any | Slack |
| 安全指摘（建設向け） | 1分 | Any | Slack |
| キャンペーン進捗（マーケ向け） | 1分 | Any | Slack |

**全テンプレートは MIT ライセンスで無料利用可能**

---

## 🔒 セキュリティ & プライバシー

### 企業秘密を OSS に流さない設計

AI-PMO は、企業データと PMO ノウハウを最初から分離します：

- ✅ **会議記録は非公開**：Docker版は ローカルLLM / VPN のみで処理
- ✅ **テンプレートは配布安全**：第三者が作ったテンプレートでも SQL 注入不可
- ✅ **テナント分離**：複数組織のデータが混ざらない仕組み
- ✅ **人間承認必須**：AI の出力を自動公開しない

> 「データを盗まれない」「モデルに覚えさせない」を **Enterprise 版の売り** にします

---

## 🛠️ 対応する AI / ツール

### 大規模言語モデル
- ✅ OpenAI（gpt-4o）
- ✅ Google Gemini（3.5 Flash）
- ✅ Groq（Llama）
- ✅ OpenRouter（複数モデル）
- ✅ ローカル Ollama（GPU 不要）

### プロジェクト管理
- ✅ Microsoft Teams（会議 Transcript）
- ✅ Jira Cloud（課題管理）
- ✅ Slack（通知）
- ✅ Jira Agile（Sprint 管理）

### データベース
- ✅ PostgreSQL（履歴 & ナレッジ）
- ✅ Qdrant（埋め込みベース検索）

---

## 🎓 ユースケース

### 例 1: ソフトウェア開発チーム（15人）
```
毎日 15:00 に開発会議（1時間）
従来：PMO担当者が議事録・TODO登録で毎日 1時間
→ AI-PMO 導入後：0 分（自動化）
月 20 時間削減 = 月 30 万円相当の効率化
```

### 例 2: 建設現場監督（複数現場）
```
毎日 17:00 に現場会議（30分）
従来：安全指摘を手作業で整理、リスク見落とし多発
→ AI-PMO 導入後：即時通知、見落とし 0 に
月 10 時間削減 + リスク低減 = 月 15 万円相当
```

### 例 3: マーケティング部（8人）
```
毎週月曜 10:00 に戦略会議（1.5時間）
従来：進捗表を手で埋める、キャンペーン詳細は別管理
→ AI-PMO 導入後：進捗レポートが自動生成
月 4 時間削減
```

---

## 🚫 「無料版」と「有償版」の違い

### 無料版（このリポジトリ）
| 機能 | 無料版 |
|-----|-------|
| 基本テンプレート 7個 | ✅ |
| 複数組織対応 | ✅ |
| Teams / Jira / Slack | ✅ |
| ローカル LLM 対応 | ✅ |
| ライセンス | MIT（完全無料） |
| 販売・改造 | OK（MIT ライセンス） |

### 有償テンプレート（別リポジトリ、リリース未定）
- 業界特化テンプレート（金融・医療など）
- 複雑な意思決定フロー
- 自社システム連携
- 導入サポート付き

> 無料版でも **完全に動作**します。有償テンプレートは「より専門的な業界向け」の追加選択肢です。

---

## ❓ よくある質問

### Q: データはどこに保存されるの？
**A:** あなたが決めます。
- Docker版：ローカル PC に全て保存
- クラウド版：Oracle Cloud Always Free（無料枠）に PostgreSQL
- VPN経由：社内サーバーに接続

### Q: AI の学習に使われるの？
**A:** いいえ。ローカル LLM なら学習なし。クラウド AI（OpenAI等）の場合も、組織が OpenAI 利用規約で opt-out 可能です。

### Q: 導入にはどのくらい時間がかかるの？
**A:** 15 分。インストール（5分）→ セットアップ（10分）→ 実行。

### Q: 既存の Teams / Jira / Slack を変更する必要は？
**A:** いいえ。読み取り権限だけで動きます。

---

## 📚 詳しく知りたい方へ

| 目的 | ドキュメント |
|------|-----------|
| インストール手順 | [INSTALL.md](./INSTALL.md) |
| 各プロバイダ設定 | [PROVIDERS.md](./PROVIDERS.md) |
| Teams 認証設定 | [TEAMS.md](./docs/TEAMS.md) |
| Jira / Slack 設定 | [JIRA-SLACK.md](./docs/JIRA-SLACK.md) |
| スケジューラ設定 | [SCHEDULER.md](./docs/SCHEDULER.md) |
| エージェント機能 | [AGENTS.md](./docs/AGENTS.md) |
| アジャイル対応 | [AGILE.md](./docs/AGILE.md) |
| Oracle Cloud 無料構成 | [DEPLOY-ORACLE.md](./DEPLOY-ORACLE.md) |
| テンプレート作成ガイド | [docs/guide/ja.md](./docs/guide/ja.md) |
| 技術詳細（DSL/設計判断） | [Appendix](#-technical-appendix) |

---

## 🌍 8言語対応

ウィザード・Web UI・ガイド は以下の言語に対応：

- 日本語 (ja)
- English (en)
- 中文 (zh)
- 한국어 (ko)
- Español (es)
- Français (fr)
- Deutsch (de)
- Português (pt)

---

## 📞 サポート & コミュニティ

- **GitHub Issues**：バグ報告・機能提案
- **Discussions**：使い方相談
- **ドキュメント**：日英併記で順次拡充中

---

## 📄 ライセンス

MIT License © 2026 agNedia Inc.

**重要：** このリポジトリ内のテンプレート・プロンプトも MIT で無料です。商用利用・改造 OK。

詳細は [LICENSE](./LICENSE) と [NOTICE.md](./NOTICE.md) を参照。

---

## 🚀 次のステップ

```
1. このリポジトリを clone
   git clone https://github.com/itanaka66/AI-PMO-Platform.git

2. インストール実行
   scripts/install.sh  (macOS/Linux)
   scripts\install.bat (Windows)

3. セットアップウィザード
   aipmo setup

4. Web UI で実行
   aipmo serve --host 0.0.0.0

5. 毎朝自動実行に設定
   aipmo schedule
```

**5分で開始。今すぐダウンロード**

---

---

# 🔧 Technical Appendix

## テンプレート DSL

PMO ワークフローを YAML で定義。以下の例：

```yaml
name: meeting_minutes
industry: software
trigger: "event:teams:meeting_ended"

steps:
  - id: fetch_transcript
    adapter: teams
    action: get_transcript
    inputs:
      meeting_id: "{{ trigger.meeting_id }}"

  - id: generate_minutes
    llm: { profile: default, temperature: 0.1 }
    prompt: minutes_ja
    inputs:
      transcript: "{{ steps.fetch_transcript.output.text }}"
    output_format: json

  - id: register_todos
    adapter: jira
    action: create_issues
    inputs:
      project: "{{ params.jira_project }}"
      issues: "{{ steps.generate_minutes.output.action_items }}"
```

## セキュリティ設計

- ✅ テンプレートから生 SQL は書けない（プリセットクエリのみ）
- ✅ テンプレートが tenant を上書きできない
- ✅ 公開コレクション への直接書き込みは拒否（人間承認フロー必須）
- ✅ 式評価は制限的（Jinja2 不採用）

詳細は [README の技術セクション](#) を参照。

## テスト & 検証

- 543 件のテスト がすべてパス
- 境界保証（権限越境・SQL注入等）を重点検証

---

## ライセンス / License

MIT License — Copyright (c) 2026 株式会社エージーネディア / agNedia Inc.

**このリポジトリにあるものは、すべて無料です。** テンプレートもプロンプトも
同じ条件で、使うために支払うものはありません。

**Everything in this repository is free**, templates and prompts included.

詳細は [/LICENSE](/LICENSE)、依存ライブラリの扱いは [/NOTICE.md](/NOTICE.md) を
参照してください。 See [/LICENSE](/LICENSE) and [/NOTICE.md](/NOTICE.md).

---

**作成：agNedia Inc.**  
**メンテナ：itanaka**  
**最終更新：2026年9月**
