# AI-PMO Platform — Step 1: DSL + Execution Engine

PMO 業務のノウハウを「実行可能なテンプレート」として記述し、LLM と外部ツール連携を
組み合わせて自動実行する基盤。

A runtime that encodes PMO know-how as executable templates and runs them by
combining LLM calls with integrations into the tools a team already uses.

Step 1 の範囲は **基盤のみ**。Teams / Jira / Slack の実アダプタは Step 2 以降で、
現時点ではモックアダプタが入っている。PostgreSQL と Qdrant のアダプタは実装済み。

Step 1 covers **the runtime only**. Real Teams / Jira / Slack adapters land in
Step 2; mocks stand in for now. The PostgreSQL and Qdrant adapters are real.

---

## Quick start

PC に不慣れな方は [INSTALL.md](INSTALL.md) を見てください。
Windows インストーラ、Mac/Linux スクリプト、Docker の3通りがあります。

If you are not comfortable with a terminal, see [INSTALL.md](INSTALL.md) —
there is a Windows installer, a macOS/Linux script, and a Docker option.

```bash
# インストーラ / installers
./scripts/install.sh          # macOS / Linux
scripts\install.bat           # Windows
./scripts/install-docker.sh   # Docker (local AI)

# 開発者向け / from source
pip install -e ".[dev]"          # 基盤のみ / runtime only
pip install -e ".[cloud,data]"   # クラウド LLM + Postgres/Qdrant

aipmo setup                      # 初回設定 / first-run setup
aipmo serve --host 0.0.0.0       # スマホ向け画面 / mobile interface

aipmo validate templates/examples/meeting_minutes.yaml
aipmo --config config.dev.yaml run templates/examples/meeting_minutes.yaml \
      --trigger '{"meeting_id":"MTG-001"}' --json
aipmo adapters                   # 登録済みアダプタとアクション / registered actions
aipmo --config config.docker.yaml doctor   # 接続確認 / connection check
pytest
```

---

## テンプレート DSL / Template DSL

```yaml
name: meeting_minutes
industry: software
trigger: "event:teams:meeting_ended"

params:
  jira_project: PROJ

steps:
  - id: fetch_transcript
    adapter: teams
    action: get_transcript
    inputs:
      meeting_id: "{{ trigger.meeting_id }}"
    retry: { max_attempts: 3, backoff_seconds: 5 }

  - id: minutes
    llm: { profile: default, temperature: 0.1 }
    prompt: minutes_ja
    inputs:
      transcript: "{{ steps.fetch_transcript.output.text }}"
    output_format: json
    output_schema:
      required: [title, decisions, action_items]

  - id: register_jira
    adapter: jira
    action: create_issues
    when: "{{ steps.todos.output.items }}"
    inputs:
      project: "{{ params.jira_project }}"
      issues: "{{ steps.todos.output.items }}"
```

ステップ種別は `adapter` / `llm` / `expression` のどれを書いたかで自動判定される。
`kind:` を明示することもできる。

The step kind is inferred from which of `adapter` / `llm` / `expression` is
present. `kind:` can also be stated explicitly.

参照できる名前空間 / Available namespaces:

| 名前空間 / Namespace | 内容 / Contents |
|---|---|
| `params.*` | 実行時パラメータ / runtime parameters |
| `trigger.*` | 起動イベントのペイロード / trigger payload |
| `run.*` | `id` / `template` / `started_at` / `date` |
| `steps.<id>.output` | 先行ステップの出力 / output of a preceding step |

---

## データ層 / Data layer

| | 用途 / Purpose |
|---|---|
| **PostgreSQL** | 実行履歴、ナレッジ昇格ワークフロー、テナント利用許諾レベル / run history, knowledge promotion workflow, per-tenant consent level |
| **Qdrant** | テナント別の非公開ナレッジと、一般化済みの公開ナレッジ / per-tenant private knowledge and generalized public knowledge |

```
Qdrant
├── tenant_company_a        非公開 / private
├── tenant_company_b        非公開 / private
└── public_pmo_knowledge    公開 / public
```

`sql/schema.sql` にスキーマ、`queries.yaml` に名前付きクエリがある。

Schema lives in `sql/schema.sql`; named queries in `queries.yaml`.

---

## 設計上の判断 / Design decisions

### プロンプトを YAML から分離した / Prompts are separated from the YAML

業界別テンプレートの差分は、ほぼプロンプトに集中する。構造を共通化しておけば、
プロンプトだけ差し替えて別業界向けテンプレートを作れる。
ソフトウェア開発版 → 建設版の展開コストがここで決まる。

What differs between industry templates is almost entirely the prompt. Keeping
the structure shared means a new industry variant is a prompt swap. The cost of
going from the software build to the construction build is decided here.

### LLM は論理プロファイル名で指定する / LLMs are named by logical profile

テンプレートには `profile: default` としか書かない。実際の割り当ては config 側:

- `config.docker.yaml` → ollama (ローカル LLM)
- `config.laptop.yaml` → openai (クラウド)

同一テンプレートが Docker 版と非 Docker 版の両方でそのまま動く、という要件を満たすため。
会議 Transcript は機微情報を含むので、Docker 版はローカル LLM を既定にしている。
埋め込みモデルも同じ扱い。

A template only ever says `profile: default`; the mapping lives in config. This
is what lets one template run unchanged on both builds. Because transcripts are
sensitive, the Docker build defaults to a local model. Embedding models follow
the same pattern.

### 式評価を意図的に制限した / The expression evaluator is deliberately limited

テンプレートは第三者が書いて配布する想定（教材販売）なので、Jinja2 のような
汎用テンプレートエンジンを入れると配布テンプレートが攻撃面になる。
値の参照と単純な二項比較しか許していない。

Templates are authored by third parties and distributed — they are sold as
teaching material. A general-purpose template engine would turn every
distributed template into an attack surface. Only value lookup and simple
binary comparison are permitted.

### SQL はテンプレートに書かせない / Templates cannot contain SQL

同じ理由。テンプレートが指定できるのは `queries.yaml` に定義された
**クエリ名とパラメータのみ**。値は必ずドライバのパラメータ機構を通り、
文字列連結は一切行わない。`tenant` はテンプレートの入力ではなく接続設定から入るので、
テンプレートが `tenant: company_b` と書いても上書きできない。

Same reasoning. A template may pass **a query name and bound parameters**, and
nothing else. Values always go through driver-level parameter binding; no string
concatenation anywhere. The `tenant` value is injected from connection config
rather than template input, so a template that writes `tenant: company_b` cannot
override it.

### Qdrant のコレクション名もテンプレートに書かせない / Nor collection names

テンプレートが選べるのは論理スコープ `private` / `public` だけ。実コレクション名は
接続設定で解決する。配布テンプレートに `tenant_company_b` と直書きされていても、
他社データには届かない。

A template selects the logical scope `private` or `public`; the concrete
collection is resolved from connection config. A distributed template that
hardcodes `tenant_company_b` cannot reach another tenant's data.

### 公開コレクションへの書き込みをアダプタが拒否する / The adapter refuses public writes

ナレッジの公開は、人間承認を経た昇格フローだけが行える。テンプレートができるのは
`submit_candidate` で公開候補として提出するところまでで、`review_status: pending`
のまま非公開コレクションに入る。自動公開の経路を、そもそもテンプレートから
作れないようにしてある。

Publication happens only through the reviewed promotion workflow. A template can
call `submit_candidate`, which lands the record in the private collection marked
`review_status: pending`. It cannot publish. The automatic-publication path does
not exist at the adapter level, so no template — including one written by a third
party — can construct it.

### 冪等キーはトリガー由来 / Idempotency keys derive from the trigger

`run_id` ではなく `meeting_id` を起点にする。同じ会議を再処理しても Jira の Issue や
Qdrant の point が重複しない。Qdrant の point ID はこのキーから UUIDv5 で導出するため、
別プロセスでの再実行でも同じ ID になる。

Keyed on `meeting_id` rather than `run_id`, so reprocessing the same meeting does
not duplicate Jira issues or Qdrant points. Point IDs are derived from that key
via UUIDv5, so a re-run in a separate process produces the same ID.

### アクション探索は MRO を遡る / Action discovery walks the MRO

`@action` を付けずにサブクラスでメソッドをオーバーライドすると
アクションが黙って消える、という壊れ方をテストで踏んだので修正済み。

A subclass overriding a method without re-applying `@action` used to silently
unregister it. A test caught this; the lookup now walks the MRO.

---

## テスト / Tests

28 件。境界の保証が主眼:

28 tests. The isolation guarantees are the point:

- テンプレートから生 SQL を渡せない / raw SQL cannot be passed from a template
- テンプレートが `tenant` を上書きできない / a template cannot override the tenant
- テンプレートがコレクション名を指定できない / a template cannot name a collection
- 公開コレクションへの直接書き込みが拒否される / direct public writes are refused
- 許諾レベル A のテナントでは候補提出そのものが走らない / consent level A skips candidate submission entirely
- 前方参照・ID 重複はロード時に検出される / forward references and duplicate IDs fail at load time

---

## 未着手 / Not yet built

- 実アダプタ / real adapters (Teams Graph API, Jira, Slack)
- スケジューラ / scheduler (cron 常駐実行 / resident cron execution)
- Web UI
- 匿名化・一般化エージェント / anonymization and generalization agents —
  現状 `submit_candidate` は「候補を受け取る器」であって、一般化そのものは行わない /
  `submit_candidate` currently accepts candidates; it does not itself generalize
- 公開可能性スコアの算出 / publicability scoring — 保存はできるが計算は未実装 /
  the field is stored, the computation is not implemented
- 並列ステップ実行 / parallel steps — 現状は逐次のみ / sequential only
- 実行履歴の永続化配線 / run-history persistence wiring — スキーマとクエリはあるが、
  エンジンからの自動書き込みは未接続 / schema and queries exist, the engine does not
  write to them automatically yet
