# Jira と WBS の設定・運用ガイド / Setting up and running Jira and the WBS

AI-PMO Platform を、**Jira（実作業の課題）**と**WBS（計画）**につないで運用するための手順です。
接続の細かい仕様は [JIRA-SLACK.md](JIRA-SLACK.md)・[SELF-WBS.md](SELF-WBS.md)・[TASK-GENERATION.md](TASK-GENERATION.md)
に書いてあり、ここでは**設定して動かし、毎日・毎週どう回すか**を 1 か所にまとめます。

How to connect AI-PMO Platform to Jira (the work items) and a WBS (the plan), and how to run it day to day.
Details live in the linked documents; this is the end-to-end procedure.

---

## 0. 全体像

| | Jira | WBS ファイル |
|---|---|---|
| 何か | 日々の作業の課題（バグ・タスク） | 計画（成果物・工程・依存・期限）を書いた YAML |
| 置き場 | Jira Cloud | リポジトリ（`wbs/aipmo.yaml` など） |
| 書き換える人 | チーム（Jira 上で） | **人だけ、PR で**（AI は読むだけ） |
| PMO AI がすること | **読む**（収集）。担当の書き戻しと起票は**人が確定したとき**だけ | **読む**・検証する・ずれを**提案**にする。ファイルは承認後に反映（人の操作） |
| 台帳での id | `JIRA:PROJ-123` | `WBS:3.5`（`project` は WBS の `id`） |

どちらも PMO Core の**同じ台帳**（Task Engine）に入るので、`aipmo pmo`・`aipmo tasks`・画面で一緒に順位付けされ、
警告・担当の提案・学習の対象になります。

**守られること（どちらにも共通）**: 外の世界に書くのは、人が確定した操作と、運用者が `auto` と明示した範囲だけ。
収集は読み取り専用。点数や判定は決定論的な計算で、LLM は使いません。

---

# 第 1 部　Jira

## 1. つなぐ

### 1-1. API トークンと設定

1. <https://id.atlassian.com/manage-profile/security/api-tokens> で API トークンを作ります（**値は作った直後にしか見られません**）。
2. `.env` に置きます。

   ```bash
   JIRA_EMAIL=you@example.com          # トークンを作った本人のアドレス（違うと 401）
   JIRA_API_TOKEN=...
   ```

3. `config.yaml` に書きます。

   ```yaml
   adapters:
     mode: real                         # mock のままだと、どこにも繋がらない
     jira:
       site: https://yourcompany.atlassian.net
       email: ${JIRA_EMAIL}
       api_token: ${JIRA_API_TOKEN}
       project: PROJ                    # 起票先の既定のプロジェクトキー
       issue_type: Task                 # 起票する課題タイプ（Jira にある名前）
   ```

4. 確かめます。

   ```bash
   aipmo doctor
   ```

   接続できない・認証できないときは、第 1-7 節の表を見ます。

### 1-2. 読む（進捗の自動収集）

`pmo_core.collect` に、読みたい範囲を JQL で書きます。常駐の `aipmo schedule` が一定間隔で読み、台帳を更新します。

```yaml
pmo_core:
  collect:
    interval_minutes: 30
    refresh_known: true                 # 検索から外れた(=閉じられた)課題も ID で読み直して、完了を観測する
    max_refresh: 50
    sources:
      - id: proj
        adapter: jira
        params: {jql: 'project = PROJ AND statusCategory != Done'}
```

- **読み取り専用です。** 書き込みのアクションは、設定に書いても拒否されます。
- 一度だけ試すなら `aipmo collect`（収集元ごとの件数と、新たに完了と分かった件数が出ます）。
- `refresh_known: true` が大事です。完了した課題は `statusCategory != Done` の検索から消えるので、
  読み直さないと「いつ終わったか」が分からず、完了実績（学習の材料）が残りません。
- テンプレート（`overdue_chase` など）から Jira を読んだ出力も、同じ台帳に入ります（収集はそれと別の入口）。

### 1-3. 台帳での扱われ方（Jira の運用で守ること）

| Jira の値 | PMO AI の解釈 |
|---|---|
| 課題キー `PROJ-123` | 台帳の id は `JIRA:PROJ-123`。プロジェクトはキーの接頭辞（`PROJ`） |
| ステータス名 | **名前で判定します**（下の注意を参照） |
| 完了とみなす名前 | `Done` `Closed` `Resolved` `Complete(d)` `完了` `クローズ` `解決済み` |
| 着手とみなす名前 | `In Progress` `Doing` `In Review` `進行中` `レビュー中`（着手の時刻が、ペース学習の起点になる） |
| ブロックとみなす名前 | `Blocked` `ブロック` `ブロック中`（またはブロックのフラグ）。ブロックが何日続いているかを数える |
| 優先度 | `Highest/Blocker/Critical`=40・`High/Major`=30・`Medium`=15・`Low`=5・`Lowest`=0 点（日本語の「最高」「高」「中」「低」も可） |
| 期限 `duedate` | 期限切れ・期限間近の警告と、順位の加点に使う |
| ラベル | メンバーの `skills` と突き合わせて担当を提案する。「遅れやすいラベル」の学習にも使う |
| ストーリーポイント / 見積り | 見積りとして、ペース（1 ポイントあたりの日数）を学習する |

> **ステータス名に注意。** ワークフローの状態名が上の表に無い（例: `Finished`・`対応済`）と、完了と**認識されません**
> （完了した課題が未完了のままに見える）。標準の名前に合わせるか、運用の初めに `aipmo collect` の結果で
> 完了の件数が合っているかを確かめてください。

### 1-4. メンバーを書く（担当の提案・書き戻し）

```yaml
pmo_core:
  members:
    - {name: "田中 太郎", skills: [dev], capacity: 5}      # name は Jira の表示名と同じにする
    - {name: "鈴木 花子", skills: [qa, dev], capacity: 4}
```

- `name` を **Jira の担当者の表示名と同じ**にすると、既存の担当がそのままメンバーの負荷に数えられます。
- `capacity` は同時に持てる未完了の数。実績から**補正**されます（遅れがちな人は下がる）。
- ラベルがスキルに合う人が提案されます。確定は人です。

### 1-5. 担当を Jira に書き戻す（人が確定したとき）

```bash
aipmo assign                                  # 担当の提案の一覧（理由つき）
aipmo assign PROJ-12 --apply --writeback      # 確定して、Jira の担当者も更新する
```

- Jira は**表示名やメールからアカウントを自分で引き当てます**（`accounts` の設定は不要）。
- 引き当てられない・存在しない名前のときは、**別人に割り当てず**に止まり、台帳も変わりません。
- 画面（`aipmo serve`）の「担当を確定」ボタンも同じです（operator のみ）。

### 1-6. 課題を Jira に起票する（承認つき）

PMO Core が自分で作ったタスク（定期タスク・警告からの対応タスク）を、Jira にも課題として作れます。

```yaml
pmo_core:
  filing:
    tracker: jira
    params: {project: PROJ}             # 起票先。issue_type は adapters.jira.issue_type
    labels: [pmo-ai]
    # auto: [recurring]                 # 運用者が明示した由来だけ、常駐が承認なしで起票する(既定は無し)
```

```bash
aipmo file                              # 起票待ち（承認済み・未起票）
aipmo file <ID> --apply                 # 起票する
```

- 対応タスクは**承認してから**起票待ちになります（`aipmo generated approve <ID>`）。
- 二重に作りません（冪等キーのラベル `aipmo-pmo-...` を付け、作る前に検索）。
- 優先度の名前が相手の Jira に無いと、Jira が拒否します（失敗として台帳に残り、直してやり直せます）。
- 起票した課題は、以後の収集が同じタスクとして更新します（別のタスクが増えない）。

詳しくは [TASK-GENERATION.md](TASK-GENERATION.md)。

### 1-7. 運用の流れ

**常駐を動かす**:

```bash
aipmo schedule              # 定時テンプレートと、PMO Core の周を回す
aipmo serve                 # 画面（別のホストでもよい。台帳が PostgreSQL なら同じブリーフィングが出る）
```

| いつ | 誰が | 何をする |
|---|---|---|
| 毎日 | 担当者 | Jira で普段どおり更新する（これが PMO AI への入力。専用の入力は要らない） |
| 毎日 | PM | `aipmo pmo`（または画面）で警告・担当の提案・承認待ちを見る。**警告 → 対応タスクの提案**を承認／却下する |
| 毎日 | PM | 担当未定に対する提案を確定する（`aipmo assign KEY --apply --writeback`） |
| 週次 | PM | 順位とその理由（`aipmo tasks --why`）、学習した補正（`aipmo pmo` の末尾）を見て、上限や見積りを見直す |
| 随時 | PM | 役割AIの成果をレビュー（`aipmo agents review`）。自律的な判断の提案を承認（`aipmo judgment`） |

### 1-8. うまくいかないとき

| 症状 | 原因・対処 |
|---|---|
| 401 | `email` が**トークンを作った本人**のものではない。トークンの期限切れ |
| 403 | そのプロジェクトを見る／作る権限が無い |
| 検索結果が空、または中身が空 | JQL を確かめる。古い検索エンドポイント（410）は使っていない。項目は明示して取っている |
| 完了した課題が「未完了」のまま | ステータス名が表に無い（1-3）。`refresh_known: true` か |
| 起票が失敗する | 優先度・課題タイプの名前が Jira に無い。`aipmo file` の「前回失敗」の理由を見る |
| 担当が付かない | 表示名が Jira に無い／曖昧。`unassigned` に名前が出る |
| 担当の書き戻しで止まる | 引き当てられない名前。**別人に付けないための停止**なので、Jira 側の表示名を確かめる |
| 収集が失敗する | `aipmo collect` の理由。常駐は他の収集元を続け、`aipmo pmo` に理由が残る。続くと自律的な判断が診断する |

---

# 第 2 部　WBS

## 2. WBS ファイルを書く

WBS は YAML です。**人が書き、PR でレビューします**（AI は読むだけで、書き換えません）。

```yaml
wbs:
  id: shop-renewal                 # 台帳の project になる。提案の対象を取り違えないための印にも使う
  name: ショップ刷新
  deadline: 2026-12-31             # 全体の期限（決まっていなければ null。遅れの予測をしない）
  velocity_window_days: 28         # 速度は直近これだけの日数の完了実績から出す
  nodes:
    - id: "1"                      # id は必ず引用符で囲む（1.10 は数値 1.1 になってしまう）
      name: "企画"
      children:
        - id: "1.1"
          name: "要件定義"
          status: done             # todo | in_progress | blocked | done
          effort: 3                # 相対的な大きさ（1 = 半日程度）
          done_on: 2026-09-10      # 完了した日（速度の計算に使う）
          owner: 田中
          evidence:                # 完了の証拠。実在しないと error
            - "docs/requirements.md::要件"      # ファイル::その中に含まれる語句
        - id: "1.2"
          name: "画面設計"
          status: in_progress
          effort: 5
          due: 2026-10-20
          depends_on: ["1.1"]      # 依存は葉どうしにだけ
          priority: High
          notes: "デザインレビュー待ち"
```

| 項目 | 意味 |
|---|---|
| `id` `name` | 必須。id は引用符つきの文字列で、重複しない |
| `status` | `todo` / `in_progress` / `blocked` / `done` |
| `effort` | 見積り（相対値）。未完了で無いと `unestimated` の警告（予測から外れる） |
| `done_on` `evidence` | 完了のとき。**証拠のファイル（と語句）が実在しないと完了にならない**（CI が落ちる） |
| `depends_on` | 先に終わっているべき作業の id（循環・存在しない id は error） |
| `due` `owner` `priority` `notes` | 任意 |

**ルール**: 完了には証拠がいる／着手したら `in_progress`／作業を足す・分けるのも PR／AI は書き換えない
（[SELF-WBS.md](SELF-WBS.md) のルール）。

### 検証と見方

```bash
aipmo wbs check                      # 誤りと証拠の欠けを調べる（error があれば終了コード 1）
aipmo wbs check --strict             # warning も失敗にする
aipmo wbs status                     # 進捗・速度・完了見込み・クリティカルパス・次に着手できる作業
aipmo wbs status --json
```

（既定のファイルは `wbs/aipmo.yaml`。別のファイルは `aipmo wbs check path/to/wbs.yaml --root .`。`--root` は証拠のパスの基準）

| コード | 種類 | 意味 |
|---|---|---|
| `evidence_missing` | error | 「完了」だが証拠のファイル／語句が無い |
| 循環・存在しない依存先・id の重複・不正な status/日付/effort | error | 構造の誤り（直すまで予測しない） |
| `maybe_done` | warning | 証拠がすべて揃っているのに未完了（**更新漏れ**） |
| `done_without_evidence` | warning | 「完了」だが証拠が書かれていない |
| `done_before_dependency` / `overdue` / `unestimated` | warning | 依存先が未完了／期限超過／見積りなし |

速度 = 直近の完了の見積り合計 ÷ 日数。実績が無ければ予測しません。**日付を約束するものではなく、「このペースが続けば」の目安です。**

## 3. 運用に載せる

### 3-1. CI で守る（PR のたび）

```yaml
# .github/workflows/tests.yml の例
- run: python -m aipmo.cli wbs check wbs/aipmo.yaml      # error（証拠の欠け・循環）があれば失敗
```

さらに `.github/workflows/wbs-drift.yml` を置くと、PR に**コメント**で更新漏れを知らせます（PR は失敗にしません。fork では動かない）。
この PR が変えたファイルに関係する作業を先に出し、完了にする書き方（`status: done` / `done_on:`）を添えます。

```bash
aipmo wbs notify --base origin/main                    # コメントの本文を表示するだけ（何も書かない）
aipmo wbs notify --base origin/main --pr 12 --post     # PR #12 に書く（GITHUB_TOKEN が要る）
```

### 3-2. 台帳に入れて、PMO Core に見せる

WBS の葉は、`wbs_file` アダプタを通してタスクとして台帳に入り、Jira の課題と**同じ順位付け**に載ります。

```yaml
adapters:
  mode: real
  wbs_file: {root: ., file: wbs/aipmo.yaml}
  slack: {token: ${SLACK_BOT_TOKEN}, default_channel: "#pmo"}
```

`templates/examples/self_development.yaml`（毎週月曜 9:00 に WBS を読んで Slack に報告）を使います。台帳での id は
`WBS:1.2`、プロジェクトは WBS の `id` です。**WBS からは書き戻しません**（読み取り専用）。

### 3-3. 更新漏れ・証拠の欠けを「提案」にする

```yaml
pmo_core:
  generate:
    wbs:
      file: wbs/aipmo.yaml
      root: .
      codes: [maybe_done, done_without_evidence, evidence_missing]   # 既定
      interval_minutes: 60
```

`maybe_done`・証拠の欠けが、「WBS を確かめる」という**承認待ちの提案**になります（`aipmo generated`）。承認すれば
普通のタスクになり、直れば未決の提案は取り下げられます。WBS ファイルは**変わりません**。

### 3-4. AI の再計画案を、承認して WBS ファイルへ反映する（PostgreSQL が要る）

WBS 再計画 AI（`wbs_replan`）の提案の `diff.changes`（決まった形の変更）を、人が承認したときに WBS ファイルへ反映できます。

```yaml
adapters:
  postgres: {dsn: ...}
  wbs_replan: {file: wbs/aipmo.yaml, root: .}
```

```bash
aipmo wbs proposals                  # 承認待ちの一覧
aipmo wbs proposals show <ID>        # ファイルがどう変わるか（何も書かない）
aipmo wbs proposals approve <ID>     # 承認して反映（反映後の内容まで検証してから書く）
aipmo wbs proposals reject <ID>
```

反映後は `git diff` で確かめて、コミット（PR）します。詳しくは [SELF-WBS.md](SELF-WBS.md)。

### 3-5. 運用の流れ

| いつ | 誰が | 何をする |
|---|---|---|
| 着手したとき | 担当者 | WBS を PR で `in_progress` にする |
| 終えたとき | 担当者 | `status: done`・`done_on`・`evidence` を足す PR。PR のコメントで更新漏れに気づける |
| 毎週 | PM | 報告（Slack）か `aipmo wbs status` で、速度・完了見込み・クリティカルパス・次に着手できる作業を見る |
| 毎週 | PM | `aipmo generated` の「WBS を確かめる」提案を承認／却下 |
| 速度が落ちたとき | PM | 原因を見て作業を分ける・期限を見直す（PR）。再計画案は 3-4 |

---

# 第 3 部　Jira と WBS を併用する

**WBS の作業と Jira の課題は、自動では結び付きません。** WBS の `3.5` と Jira の `PROJ-123` が同じ仕事でも、
PMO AI はそれを知らないので、**別々のタスクとして台帳に並びます**（同じ仕事が 2 回数えられる）。次のどれかの割り切りで使います。

| 使い方 | 向いている場面 | 注意 |
|---|---|---|
| **A. 粒度で分ける**（推奨）: WBS = 成果物・工程（週〜月単位）、Jira = 日々の作業 | 計画と実行を別々に見たい | 同じ仕事を両方に書かない。WBS の `notes` に Jira のキーや Epic を書いておくと、人が辿れる |
| **B. Jira だけ** | 計画の管理が Jira（Epic・スプリント）で足りる | WBS の機能（証拠に基づく完了・速度・クリティカルパス）は使わない |
| **C. WBS だけ** | 計画をコードと一緒に管理したい（このプロジェクト自身がこの形） | 担当者の更新が Jira に比べて重い（PR が要る） |

どれでも、警告・担当の提案・学習・承認の流れは共通です。

---

## 設定の全体例（Jira + WBS）

```yaml
tenant: company_a
lang: ja

llm: {default: {provider: openai, model: gpt-4o-mini}}

adapters:
  mode: real
  jira: {site: https://yourcompany.atlassian.net, email: ${JIRA_EMAIL}, api_token: ${JIRA_API_TOKEN},
         project: PROJ, issue_type: Task}
  wbs_file: {root: ., file: wbs/plan.yaml}
  slack: {token: ${SLACK_BOT_TOKEN}, default_channel: "#pmo"}

task_engine: {}                         # 台帳（SQLite）。複数ホストなら backend: postgres（LEDGER-STORAGE.md）

pmo_core:
  members:
    - {name: "田中 太郎", skills: [dev], capacity: 5}
    - {name: "鈴木 花子", skills: [qa, dev], capacity: 4}
  collect:
    interval_minutes: 30
    sources:
      - {id: proj, adapter: jira, params: {jql: 'project = PROJ AND statusCategory != Done'}}
  generate:
    followups: true                     # 続く重大な警告から、対応タスクを「提案」する
    wbs: {file: wbs/plan.yaml, root: .}
  filing:
    tracker: jira
    params: {project: PROJ}
  judgment: {}                          # 自律的な判断（既定は提案どまり）
```

## 立ち上げのチェックリスト

1. `aipmo doctor` が通る（Jira の認証）。
2. `aipmo collect` で、Jira の未完了と、閉じた課題の完了が観測できる（件数を目で確かめる）。
3. `aipmo wbs check` が error 0。CI に載せた。
4. `aipmo schedule` を常駐させた。`aipmo serve` を開いて、警告・提案が出る。
5. メンバーの `name` と `capacity` が実情に合っている（学習で補正されるが、初期値は人が決める）。
6. 外の世界に書く機能（担当の書き戻し・起票）は、**まず 1 件を人の操作で**試し、Jira 側の結果を確かめてから `auto` を考える。

## 限界

- 実際の Jira Cloud に対する通しは、運用者側で確かめてください（偽のサーバーとテストで確認した範囲です）。
- Jira のステータス名は上の表の名前で判定します（独自の名前は認識されない）。
- WBS の作業と Jira の課題の自動の対応付けはありません（第 3 部）。
- WBS からの書き戻し（`owner` など）はありません。WBS を書き換えるのは人（と、承認した再計画案の反映）だけです。
