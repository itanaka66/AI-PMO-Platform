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
