# AI-PMO Platform

<a href="https://claude.ai/code/artifact/877371e4-7535-46c8-91bb-027d61dbc1a6" target="_blank">AI-PMO
はじめてのガイド</a> 

PMO 業務のノウハウを「実行可能なテンプレート」として記述し、LLM と外部ツール連携を
組み合わせて自動実行する基盤。

<a href="https://claude.ai/code/artifact/877371e4-7535-46c8-91bb-027d61dbc1a6" target="_blank">AI-PMO Getting Started Guide</a> 

A runtime that encodes PMO know-how as executable templates and runs them by
combining LLM calls with the tools a team already uses.

**すべて無料です。** 機能制限版でも試用版でもありません。MIT License なので、
商用利用も改変も再配布も自由です。

**All of it is free** — not a reduced edition, not a trial. MIT licensed, so
commercial use, modification and redistribution are all permitted.

はじめての方は **[はじめてのガイド](docs/guide/README.md)**（8言語）をどうぞ。
New here? Start with the **[getting-started guide](docs/guide/README.md)**.

---

## できること / What it does

| テンプレート | 内容 |
|---|---|
| `meeting_to_tasks` | 会議 → 議事録 → TODO → Jira 起票 → Slack 通知 |
| `meeting_task_update` | 会議の内容から既存課題を更新（確信度で選別） |
| `overdue_chase` | 期限超過の担当者へ個別に催促 |
| `overdue_triage` | 遅延状況をエージェントが調査して報告 |
| `sprint_health` | スプリントの状況確認（問題があるときだけ通知） |
| `wbs_from_meeting` | 会議の決定事項から WBS の草案 |
| `model_comparison` | 同じプロンプトを複数の AI に同時投稿し、書きぶりを比較 |
| `parallel_notify` | 独立した通知を同時に送り、実行時間を縮める |
| `construction/site_meeting` | 工程会議 → 是正起票・安全指摘の即時通知 |
| `marketing/campaign_check` | キャンペーン進行（承認待ちを分けて扱う） |

連携先 / Integrations: Teams · Jira · Jira Agile · Slack · PostgreSQL · Qdrant
AI: OpenAI · Gemini · Groq · OpenRouter · Ollama · vLLM · LM Studio

---

## Quick start

```bash
# インストーラ / installers
./scripts/install.sh          # macOS / Linux
scripts\install.bat           # Windows
./scripts/install-docker.sh   # Docker (local AI)

# 開発者向け / from source
pip install -e ".[dev]"
pip install -e ".[cloud,data,web]"   # 全部入り / everything

aipmo setup                          # 初回設定 / first-run setup
aipmo validate templates/examples/meeting_to_tasks.yaml
aipmo run templates/examples/overdue_triage.yaml
aipmo serve --host 0.0.0.0           # スマホ向け画面 / mobile interface
aipmo schedule                       # 定時実行 / the scheduler
aipmo doctor                         # 接続確認 / connection check
pytest                               # 543 件
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

ステップ種別は `adapter` / `llm` / `agent` / `expression` のどれを書いたかで
自動判定される。`kind:` を明示することもできる。

The step kind is inferred from which of `adapter` / `llm` / `agent` /
`expression` is present.

参照できる名前空間 / Available namespaces:

| 名前空間 | 内容 |
|---|---|
| `params.*` | 実行時パラメータ / runtime parameters |
| `trigger.*` | 起動イベントのペイロード / trigger payload |
| `run.*` | `id` / `template` / `started_at` / `date` |
| `steps.<id>.output` | 先行ステップの出力 / output of a preceding step |

### 繰り返し / Iteration

値の並びに対して、同じ工程を1件ずつ実行する。担当者ごとに1通ずつ送る、
といった処理はこれが無いと書けない。

```yaml
- id: chase
  for_each: "{{ steps.compose.output.messages }}"
  as: message
  where: "{{ message.confidence }} >= {{ params.threshold }}"
  max_items: 30
  adapter: slack
  action: post_message
```

1件の失敗で全体を止めない。結果は `.count` / `.failed` / `.skipped` で受け取る。
`{{ loop.number }}`（1始まり・表示用）、`{{ loop.index }}`（0始まり）、
`{{ loop.total }}` が使える。

`when` はループの前に一度しか評価されないため、要素自身の値で絞るには `where`
を使う。

One failure does not stop the rest. `when` is evaluated once before the loop, so
filtering on an element's own values needs `where`.

### 並列実行 / Parallel steps

互いに依存しないステップを `parallel:` にまとめると、同時に実行される。
Jira への起票と Slack への通知のように、片方の結果をもう片方が待つ必要が
ない工程を並べるのに使う。

```yaml
- id: notify_everyone
  parallel:
    - id: notify_slack
      adapter: slack
      action: post_message
      inputs: { channel: "#project-updates", text: "..." }
    - id: notify_teams
      adapter: teams
      action: post_message
      inputs: { channel: "{{ params.teams_channel }}", text: "..." }
```

グループの中の工程どうしは、互いの出力を参照できない（ロード時に検証される）。
後続のステップからは `steps.notify_slack.output` のようにそのまま参照できる。
1件の失敗で全体を止めない。全滅したときだけこの工程自体が失敗になる。

Steps inside one group cannot reference each other's output (checked when the
template is loaded, not at run time). Later steps can reference any of them
directly, e.g. `steps.notify_slack.output`. One failure does not stop the
rest; the group itself only fails when every step inside it does.

### エージェント / Agents

決められた工程を流すのではなく、AI が道具を選んで自分で呼ぶ。
手順が事前に決まらない仕事に向く。

```yaml
- id: investigate
  agent:
    tools: [jira.find_overdue]   # 使ってよい道具を列挙する
    allow_writes: false          # 既定。外の世界は変えさせない
    max_iterations: 5
  prompt_inline: 遅延の状況を調べて報告してください
```

詳しくは [docs/AGENTS.md](docs/AGENTS.md)。

### 複数の提供元を同時に呼ぶ / Multiple providers at once

`profile` の代わりに `profiles` を並びで書くと、同じプロンプトを複数の
LLM に同時に投げて、結果を並べて比較できる。

```yaml
- id: draft_minutes
  llm:
    profiles: [ollama, gemini, openai]   # ローカル + クラウド2つを同時に
  prompt: minutes_ja
```

1つが落ちても他は止まらない。全滅したときだけステップが失敗になる。
詳しくは [docs/PROVIDERS.md](docs/PROVIDERS.md)。

Write `profiles` instead of `profile` and the same prompt is sent to several
LLMs at once, with every answer kept for comparison. One provider going down
does not stop the others; the step only fails when all of them do. See
[docs/PROVIDERS.md](docs/PROVIDERS.md).

---

## 設計上の判断 / Design decisions

### プロンプトを YAML から分離した

業界別テンプレートの差分は、ほぼプロンプトに集中する。構造を共通化しておけば、
プロンプトだけ差し替えて別業界向けを作れる。

What differs between industry templates is almost entirely the prompt.

### LLM は論理プロファイル名で指定する

テンプレートには `profile: default` としか書かない。実際の割り当ては config 側。
提供元を乗り換えてもテンプレートは変わらない。

A template only ever says `profile: default`; the mapping lives in config, so
switching providers changes no template.

### 数えれば決まる値を、モデルに数えさせない

完了率、残り日数、経過日数は、数えれば決まる。言語モデルに数えさせると
間違えるし、**間違えても正しそうに見える。** アダプタか組み込み変換
（`days_between` / `count`）で計算し、モデルには解釈だけさせる。
Slack に出す数値も集計結果をそのまま貼る。言い換えの過程で数字が変わっても、
読む側には見分けがつかない。

Countable facts are computed before the model sees them: a language model
miscounts, and does so plausibly. Figures posted to Slack come from the
aggregation, not from the model — if one changed while being rephrased, a reader
would have no way to tell.

### 式評価を意図的に制限した

テンプレートは第三者が書いて配布される想定（教材販売）なので、Jinja2 のような
汎用テンプレートエンジンを入れると配布テンプレートが攻撃面になる。
値の参照と単純な二項比較しか許していない。

Templates are authored by third parties and distributed. A general-purpose
template engine would turn every distributed template into an attack surface.

### SQL とコレクション名をテンプレートに書かせない

同じ理由。PostgreSQL は `queries.yaml` に定義された**クエリ名とパラメータのみ**、
Qdrant は論理スコープ `private` / `public` のみ。`tenant` は接続設定から入るので、
テンプレートが上書きできない。

Same reasoning: a template passes a query name and bound parameters, or a
logical scope — never raw SQL or a collection name. The tenant comes from
connection config and cannot be overridden.

### 公開コレクションへの書き込みをアダプタが拒否する

ナレッジの公開は、人間承認を経た昇格フローだけが行える。テンプレートができるのは
`submit_candidate` で候補として提出するところまで。**自動公開の経路を、
そもそもテンプレートから作れない。**

Publication happens only through the reviewed promotion workflow. The automatic
path does not exist at the adapter level, so no template can construct it.

### 公開可能性スコアは並び順のためだけにある

`submit_candidate` は公開可能性スコアを自動で算出する。テンプレート側が
数値を用意する必要はない。ただし、これは**レビュー待ち一覧の並び順を
決める下書きの値**であって、承認・却下の判定ではない。判定は必ず人間が行う。

数えれば決まる範囲（利用許諾レベル・宣言された一般化の度合い・メール
アドレスや課題番号らしき文字列の有無）だけを見る。**言語モデルには頼らない**
— 公開してよいかは誤ると取り返しがつかない判断で、間違っても
もっともらしく見えるものに任せるべきではない。利用許諾レベル A
（二次利用不可）は、他の要素に関わらず無条件で 0 点にする。

`submit_candidate` computes a publicability score automatically; templates
need not supply one. But it is only **a draft value that orders the review
queue** — never an approve/reject verdict, which stays a human's call.

It looks only at what is countable or matchable — consent level, the declared
degree of generalization, whether an email address or issue-key-shaped string
appears. **No language model is involved**: whether something is safe to
publish cannot be undone if wrong, and that is not a call to hand to something
that is plausible even when mistaken. Consent level A (no secondary use)
forces a score of 0 unconditionally, regardless of anything else.

### 書き込みは読み取りより厳しく扱う

エージェントに `tools: [jira]` を渡しても課題は作られない。外の世界を変える操作には
`allow_writes: true` が別途要る。読み違いはやり直せるが、**書いた誤りはやり直せない。**

さらに更新は作成より危ない。作成の誤りは余計な課題が1件増えるだけだが、
更新の誤りは**すでに正しかった値を消す。**

Naming an adapter does not grant its write actions. A mistaken read can be
retried; a mistaken write cannot. And updating is worse than creating: a
mistaken create adds noise, a mistaken update destroys a value that was right.

### 冪等キーはトリガー由来

`run_id` ではなく `meeting_id` を起点にする。同じ会議を再処理しても Jira の
課題や Qdrant の point が重複しない。Jira には冪等の仕組みが無いので、
キーをラベルとして残し、作る前に検索する。

Keyed on `meeting_id` rather than `run_id`. Jira has no idempotency mechanism,
so the key is carried as a label and searched for before creating.

### 逃した実行は流さない

毎朝9時の報告を、正午に5日分まとめて送っても意味がない。それは通知の洪水で
あって報告ではない。逃したことは記録し、次の予定から再開する。

同じ理由で、**問題が無ければ何も送らない。** 毎朝「順調です」が届く
チャンネルは、そのうち読まれなくなる。読まれなくなった通知は、危ないときにも
読まれない。

Five days of a 9am report delivered at noon is a flood, not a report. For the
same reason, silence is the output when nothing is wrong: a channel that says
"all fine" every morning is not read when it matters either.

### 実行履歴はテンプレートを経由せず記録する

`postgres` アダプタを設定するだけで、実行の開始・各ステップ・終了が
自動で `runs` / `step_results` に記録される。テンプレートは何も書かない
— これはエンジン側の配線であって、DSL の機能にしない。書ける場所を
テンプレートに与えると、書く・書かないがテンプレートごとにばらつく。

履歴の書き込みが失敗しても、本来の業務処理は止めない。通知が届かない方が、
履歴が1件欠けるより困る。ステップ出力が大きい場合は丸ごと保存せず要約に
落とす — 無料枠クラスの小さな DB を議事録の全文だけで埋めないため
（詳しくは [docs/DEPLOY-ORACLE.md](docs/DEPLOY-ORACLE.md)）。並列グループの
中の工程も、それぞれ個別に記録される。

Configuring a `postgres` adapter is enough: a run's start, every step, and its
finish are recorded into `runs` / `step_results` automatically. Templates
write nothing for this — it stays engine-side wiring, not a DSL feature, so
whether history gets recorded never varies template to template.

A failed history write never aborts the actual workflow — a missing
notification is worse than a gap in the history. An oversized step output is
summarized rather than stored whole, so a free-tier database is not filled by
full meeting minutes alone (see
[docs/DEPLOY-ORACLE.md](docs/DEPLOY-ORACLE.md)). Steps inside a parallel group
are each recorded individually.

---

## テスト / Tests

543 件。境界の保証と、黙って壊れる形を潰すことが主眼。

543 tests, aimed at the guarantees and at the failure shapes that look like
success:

- テンプレートから生 SQL を渡せない / raw SQL cannot be passed from a template
- テンプレートが `tenant` やコレクション名を上書きできない
- 公開コレクションへの直接書き込みが拒否される
- 閲覧用トークンでは実行できない（画面ではなくサーバーが拒否する）
- Slack の `200 + ok:false` を成功として扱わない
- エージェントが許可外の道具を呼べない、上限で必ず止まる
- 前方参照・ID 重複・不正な cron はロード時に検出される
- 8言語のガイドとカタログに抜けが無い

---

## ドキュメント / Documentation

| | |
|---|---|
| [docs/guide/](docs/guide/README.md) | 入門ガイド（8言語）/ getting started |
| [INSTALL.md](INSTALL.md) | インストール / installation |
| [docs/MOBILE.md](docs/MOBILE.md) | スマホから使う・権限分離 / phone access and roles |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | AI の提供元 / AI providers |
| [docs/AGENTS.md](docs/AGENTS.md) | エージェント / agents |
| [docs/SCHEDULER.md](docs/SCHEDULER.md) | 定時実行 / scheduling |
| [docs/TEAMS.md](docs/TEAMS.md) | Teams 連携 / Teams |
| [docs/JIRA-SLACK.md](docs/JIRA-SLACK.md) | Jira と Slack |
| [docs/AGILE.md](docs/AGILE.md) | スプリント / sprints |
| [docs/INDUSTRIES.md](docs/INDUSTRIES.md) | 業界別テンプレート / industry templates |
| [docs/DEPLOY-ORACLE.md](docs/DEPLOY-ORACLE.md) | 無料クラウド構成 / free-tier deployment |
| [NOTICE.md](NOTICE.md) | ライセンスと依存ライブラリ / licensing and dependencies |

---

## 未着手 / Not yet built

- **匿名化・一般化エージェント** — `submit_candidate` は候補を受け取る器であって、
  一般化そのものは行わない / it accepts candidates but does not generalize
- **会議議事進行（リアルタイム）** — 別プロダクトラインへ切り出し
- **上記3業界以外のテンプレート**

---

## ライセンス / License

MIT License — Copyright (c) 2026 株式会社エージーネディア / agNedia Inc.

**このリポジトリにあるものは、すべて無料です。** テンプレートもプロンプトも
同じ条件で、使うために支払うものはありません。

**Everything in this repository is free**, templates and prompts included.

詳細は [LICENSE](LICENSE)、依存ライブラリの扱いは [NOTICE.md](NOTICE.md) を
参照してください。 See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
