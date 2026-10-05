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
aipmo agents                 # 役割AIごとの件数（実行中・待ち）と、直近の実行・結果・レビュー
aipmo agents run PROJ-12     # 役割AIに割り当てたタスクを、いま任せて結果を待つ（再試行にも）
aipmo agents review          # 人のレビューを待つ成果の一覧
aipmo agents review PROJ-12 --accept --by sato --note "問題なし"
aipmo agents review PROJ-12 --reject --by sato --note "例外処理が足りない"
```

## 成果のレビュー / Reviewing a result

役割AIの成果は、人が確かめるまで「確かめた」ことになりません。確かめた結果を、台帳のタスクの実行記録に
残せます（`aipmo agents review`、Web 画面の「役割AIの成果（人のレビュー待ち）」欄のボタン）。

- **レビュー待ち** … 直近の実行が完了していて、まだ誰も確かめていない成果。実行中・失敗・時間切れは対象外
  （失敗は警告で人に渡る）。
- **認める（accepted）/ 差し戻す（rejected）** … 記録は実行記録の `review`（だれが・いつ・メモ）。
  **差し戻しには理由が要る。** 差し戻した成果は警告（`agent_attention`）になり、人が引き取るか、
  `aipmo agents run` でもう一度任せる。認めても警告にはならない。
- **役割AI自身はレビューできない。** AI が AI の成果を認めても、人が確かめたことにならない。
- **確かめ直せる。** 同じ実行をもう一度確かめると上書きされ、前の判断は `previous` に残る。
- **全件が判断ログに残る**（`agent_reviewed`）。実行記録は新しい 5 件しか持たないので、古いレビューも
  そちらに残る。役割AIごとの「認めた／差し戻した」件数は、判断ログから数える（確かめ直した実行は最後の
  判断だけ）— `aipmo agents` とブリーフィングに出る。
- 認めたタスクを閉じるのは、これまでどおり人（台帳だけのタスクは `aipmo generated done`、課題管理ツールの
  タスクはそちらで）。レビューは完了を自動では起こさない。
- Web は実行用トークン（operator）だけが記録できる。名前は申告（入力欄、無ければ `web operator`）で、
  認証された本人の確認ではない。

### まだ無いこと
- 差し戻しの理由を、もう一度任せるときのテンプレートの引数に渡すこと（今は人が見て直す／任せ直す）。
- レビュー結果を、役割AIの割り当て（提案の優先）に反映すること。

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

---

### 他言語の要約 / Summary in other languages

**中文**：为 `agent` 步骤赋予**特定角色的工具和提示词**的模板（`templates/roles/`）。机制与 AGENTS.md 相同，区别只在于给了什么、没给什么。

**한국어**：`agent` 단계에 **역할별 도구와 프롬프트**를 부여한 템플릿입니다(`templates/roles/`). 메커니즘은 AGENTS.md와 같고, 차이는 "무엇을 주고 무엇을 안 주는가"뿐입니다.

**Español**：Plantillas que dan a un paso `agent` **las herramientas y el prompt de un rol** (`templates/roles/`). El mecanismo es el mismo que en AGENTS.md; la diferencia está solo en qué se le da y qué no.

**Français**：Des modèles qui donnent à une étape `agent` **les outils et le prompt d'un rôle** (`templates/roles/`). Le mécanisme est le même que dans AGENTS.md ; la seule différence est ce qu'on lui donne ou non.

**Deutsch**：Vorlagen, die einem `agent`-Schritt **die Werkzeuge und den Prompt einer Rolle** geben (`templates/roles/`). Der Mechanismus ist derselbe wie in AGENTS.md; der Unterschied liegt nur darin, was man gibt und was nicht.

**Português**：Modelos que dão a uma etapa `agent` **as ferramentas e o prompt de uma função** (`templates/roles/`). O mecanismo é o mesmo de AGENTS.md; a diferença está apenas no que se dá e no que não se dá.
