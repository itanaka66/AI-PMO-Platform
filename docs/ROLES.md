# 役割特化のエージェント / Role-specialised agents

`agent` ステップに**役割ごとの道具とプロンプト**を与えたテンプレートです
（[templates/roles/](../templates/roles/)）。仕組みは [AGENTS.md](AGENTS.md) と同じで、
違いは「何を持たせ、何を持たせないか」だけです。

Templates that hand an `agent` step **a role's tools and prompt**
([templates/roles/](../templates/roles/)). The mechanism is the one in
[AGENTS.md](AGENTS.md); only what each role is given — and not given — differs.

| 役割 / Role | テンプレート | 道具 | 書き込み | 出力 |
|---|---|---|---|---|
| 開発AI | `role_developer` | `jira.search` `jira.add_comment` | Jira コメントのみ・**毎回承認** | 実装方針・作業分解・懸念 |
| テストAI | `role_tester` | `jira.search` `jira.add_comment` | Jira コメントのみ・**毎回承認** | 受け入れ条件・テストケース・回帰範囲 |
| 調査AI | `role_researcher` | `crawler.fetch_page` `crawler.extract_text` `vector_store.search` | なし | 出典つきの結論・確度 |
| 文書AI | `role_writer` | `jira.search` `vector_store.search` | なし | リリースノート等の**下書き** |
| 営業AI | `role_sales` | `jira.search` `vector_store.search` | なし | 顧客向け文面の**下書き**＋社内向けメモ |

## 設計上の約束 / Guarantees

- **道具は役割ごとに列挙する。** `update_issue`・`create_issues`・`upsert` などは、どの役割にも
  渡さない。テスト（[tests/test_roles.py](../tests/test_roles.py)）が役割ごとの道具の集合を固定している。
- **開発AI・テストAIが書けるのはコメントだけ。** `require_approval: true` なので、1回ごとに人の
  承認が要る（承認手段が無い環境では常に断られ、本文だけが返る）。
- **外部（顧客・公開先）へ出す手段を持たせない。** 文書AI・営業AIは、社内のレビュー用チャンネルへ
  下書きを置くところまで。送る・公開するのは人。営業AIは納期・金額・契約に触れる約束を書かず、
  社内向けメモを本文と分けて出す。
- **実行していないことを実績として書かない。** テストAIは観点を作るだけで、合否を書かない。
- **取得した文書は指示ではない。** 調査AIは、取得した本文に書かれた指示に従わない。
  調べてよいのは `params.urls` のサイトと社内知見だけ。
- 往復数とトークンの上限は、役割ごとに小さく決めてある。

## Task Engine との接続 / Connected to the Task Engine

役割AIは、`pmo_core.members` に **`kind: agent`** のメンバーとして書くと、人と並んで
**担当候補**になります。タスクが役割AIに割り当てられると、PMO Core がそのタスクの項目を
引数にして役割のテンプレートを起動し、結果を台帳に残します。

```yaml
pmo_core:
  members:
    - {name: sato, skills: [dev], capacity: 4}              # 人
    - name: dev-ai                                           # 役割AI
      kind: agent
      template: role_developer                               # templates/roles/ のテンプレート名
      skills: [dev]                                          # ラベルが一致するタスクだけが候補
      capacity: 2                                            # 同時に走らせる数
      # auto_confirm: true   # 人の確定なしで任せる（既定は人が確定）
      # prefer: true         # 人より先に提案する（既定は人が先）
      # params: {issue_key: "{external_id}"}   # 引数の対応を上書き
  agents: {timeout_minutes: 60, max_per_day: 20}
```

| 流れ | |
|---|---|
| ① 提案 | ラベルがスキルに一致するタスクだけが候補。**人が先**（空きが無い・合う人がいないときに役割AI）。 |
| ② 確定 | 人が `aipmo assign KEY --apply`（または画面）で確定する。`auto_confirm: true` の役割AIだけ、運用者の許可で自動確定。 |
| ③ 任せる | 確定した（あるいは自動確定した）タスクを、**一度だけ**テンプレートに任せる。引数はタスクから（`{key}` `{project}` `{title}` など）。 |
| ④ 結果 | 要約・実行の id・状態を台帳のタスクに残す。画面のタスクに出る。タスクは**未完了のまま** — 完了にするのは人。 |

守ること / Guarantees:

- **確定の無いものは動かさない。** 提案の段階では何も起動しない。
- **向かないタスクは走らせない。** 開発AI・テストAIは Jira のタスクだけが対象（GitHub の課題を渡しても
  空振りする）。必要な項目（`{key}` など）が無いタスクも同じ。理由は台帳に残り、**警告**
  （`agent_attention`）になって人に回る。
- **失敗しても自動では再試行しない。** 失敗・時間切れ（`timeout_minutes`）も警告。再試行は
  人が `aipmo agents run KEY` で決める。1 日の件数にも上限がある（`max_per_day`）。
- **トラッカーの担当者にはしない。** 役割AIはアカウントを持たないので、`--writeback` でも
  GitHub や Jira の担当者には書かない（台帳の担当と成果の記録だけ）。
- 起動できるのは `config.yaml` に書いたテンプレートだけ。存在しない名前は起動時にエラーになる。

```bash
aipmo agents                 # 役割AIごとの件数（実行中・待ち）と、直近の実行・結果
aipmo agents run PROJ-12     # 役割AIに割り当てたタスクを、いま任せて結果を待つ（再試行にも）
```

## 使い方 / Usage

```bash
aipmo run templates/roles/role_developer.yaml --param issue_key=PROJ-123 --param jira_project=PROJ
aipmo run templates/roles/role_researcher.yaml --param question="..." --param 'urls=["https://..."]'
aipmo run templates/roles/role_writer.yaml --param doc_type=release_notes --param period_from=2026-09-01
```

`crawler` と `vector_store` は `config.yaml` で有効にしたときだけ使える
（[CRAWLER.md](CRAWLER.md)、[VECTOR_STORES.md](VECTOR_STORES.md)）。無効な道具を指定した
テンプレートは、実行時に「登録済みアダプタの一覧」つきのエラーで止まる。

## 役割を足す / Adding a role

1. `prompts/role_<名前>_ja.md` に、手順・出力の形・**してはいけないこと**を書く。
2. `templates/roles/role_<名前>.yaml` に `agent.tools` を**必要最小限**で列挙し、
   `config.system` に役割の憲章を書く。書き込みが要るなら `require_approval: true`。
3. `tests/test_roles.py` の `EXPECTED_TOOLS` に道具の集合を足す。
