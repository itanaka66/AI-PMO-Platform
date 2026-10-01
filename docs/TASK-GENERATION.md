# タスクの生成と進捗の自動収集 / Task generation and automatic progress collection

台帳（Task Engine）に入るタスクは、これまで**外から来るもの**だけでした（課題管理ツール、
テンプレートの出力）。次の二つで、台帳自身が動けるようにしました。どちらも設定を書いた
ときだけ動きます（既定は何もしない）。

Until now the ledger only received tasks from outside. Two additions let it act itself;
both do nothing unless configured.

| | |
|---|---|
| **進捗の自動収集** | 課題管理ツールを定期的に**読み**、状態・担当・期限の変化と、閉じられた課題の完了を観測する |
| **タスクの生成** | 定期タスクを期間ごとに作る。続く重大な警告から、対応タスクを**提案**する |

---

## 進捗の自動収集 / Collecting progress

```yaml
pmo_core:
  collect:
    interval_minutes: 30            # 常駐の周に乗せ、この間隔ごとに
    refresh_known: true             # 走査に現れなかった未完了タスクも読み直す(既定)
    max_refresh: 50                 # 1 回に読み直す上限
    sources:
      - {id: jira-proj, adapter: jira, params: {jql: 'project = PROJ AND statusCategory != Done'}}
      - {id: gh-open, adapter: github_projects, params: {query: 'is:open'}, project: widgets}
```

1. **収集元の走査** … `sources` の検索を走らせ、見つかった課題を台帳へ取り込む。
2. **既知タスクの再読み込み** … 台帳の未完了タスクのうち、1 で現れなかったものを課題管理ツールから
   読み直す。**閉じられて検索条件から外れた課題**は、ここで完了と分かり、実績（期限に対する遅れ・
   見積りと実日数）として残る — 学習の材料になる。長く読んでいないものから、`max_refresh` 件まで。

守ること / Guarantees:

- **読み取り専用。** 呼べるのは読み取りのアクションだけ。書き込み系（`writes=True`）は、設定に
  書かれていても拒否して、理由を結果に残す。
- **失敗は止まる理由にしない。** 収集元が落ちても、ほかの収集元と再読み込みは続く。読めなかった
  タスクは状態を変えず、結果（`aipmo pmo`・判断ログの `collected`）に理由が残る。
- 再読み込みの対象は課題管理ツールのタスク（Jira・GitHub Projects・Plane・OpenProject・Azure DevOps）。
  台帳だけのタスクと WBS ファイルは対象外。
- 動かすのは常駐の `aipmo schedule` だけ。`aipmo pmo` などの表示専用のコマンドは、読みに行かない。

```bash
aipmo collect      # いま収集する。収集元ごとの件数と、新たに完了と分かった件数を表示
```

## タスクの生成 / Generating tasks

生成したタスクは、既定では**台帳だけ**にあり、課題管理ツールには作りません（外の世界を変えるのは、人が確定した
操作だけ、という方針のまま）。課題管理ツールにも作りたいときは、下の「課題管理ツールへの起票」を設定します。

```yaml
pmo_core:
  generate:
    recurring:                      # 運用者が書いた定期タスク
      - {id: weekly-review, title: "週次レビュー: 進捗と警告を確認する",
         every: week, weekday: MON, assignee: sato, priority: Medium, project: aipmo, due_in_days: 3}
    followups: true                 # 続く重大な警告から、対応タスクを提案する
    # followups:                    # 細かく指定するなら
    #   - {rule: overdue_severe, after_days: 1, priority: High, due_in_days: 3}
```

### 定期タスク / Recurring tasks

- `every` は `day` / `week` / `month`。`weekday`（MON〜SUN）や `day`（月の日、1〜28）を指定すると、
  その日**以降**の最初の周で作る（その日に常駐が止まっていても、期間のうちに作られる）。
- **設定に書いてあること自体が許可**なので、承認なしで作る。同じ期間には一度だけ
  （id は `PMO:rec:<id>:<期間>`）。`timezone` で日付の区切りを決める（既定 UTC）。

### 対応タスクの提案 / Follow-up proposals

- `overdue_severe` / `blocked_long` / `agent_attention` のような**重大な警告が `after_days` 日以上続いた**とき、
  「対応を決める: …」というタスクを**提案**する。`followups: true` は、この 3 つを 1 日以上続いたときに。
- **提案は承認待ち。** 承認されるまで、順位にも担当提案にも役割AIへの依頼にも入らない。
  承認すると普通の仕事になる（担当の提案が出て、役割AIにも任せられる）。
- **却下しても記録が残り**、同じ警告の回では出し直さない（警告が解消して再び起きたら、新しい回として出る）。
  却下は実績にもしない。
- **連鎖しない。** 生成したタスク自身の警告からは、提案を作らない。
- 未決の提案は 30 日で期限切れになって消える（台帳に溜めない）。作ったときは、通知先（Slack）にも知らせる。

```bash
aipmo generated                    # 承認待ちの提案と、開いている台帳だけのタスク
aipmo generated approve <id>       # 承認する(仕事になる)
aipmo generated reject <id>        # 却下する(記録は残る)
aipmo generated done <id>          # 台帳だけのタスクを完了にする(実績になる)
```

Web 画面の PMO Core 欄にも「提案されたタスク」が出て、実行用トークンなら承認・却下できます。

### 台帳だけのタスクについて / About ledger-only tasks

- 課題管理ツールに存在しないので、**完了もここで記録する**（`aipmo generated done`）。課題管理ツール側の
  タスクを台帳から閉じることはできない（次の同期で食い違う）— そちらで閉じる。
  （起票したものは課題管理ツール側のタスクになるので、そちらで閉じる。下記）
- 担当を確定しても、書き戻す先が無いので台帳にだけ残る（起票すれば、書き戻せる）。
- 開いている間は期限切れで消えない。

## WBS の更新漏れ・証拠の欠け / WBS drift

PMO AI 自身の開発を管理する WBS（[docs/SELF-WBS.md](SELF-WBS.md)）は、人が PR で更新する。更新を忘れたり、
証拠（evidence）が消えたりしたことを、PMO Core が見つけて**対応を決めるタスクの提案**にする。設定を書いたときだけ動く。

```yaml
pmo_core:
  generate:
    wbs:                              # true だけでも可(下の既定で動く)
      file: wbs/aipmo.yaml            # config.yaml のあるディレクトリから見た場所
      root: .                         # 証拠(evidence)のパスの基準
      codes: [maybe_done, done_without_evidence, evidence_missing]   # 既定。ほかに done_before_dependency, overdue
      priority: Medium
      due_in_days: 7
      interval_minutes: 60            # 証拠の確認はファイルを読むので、間引く
```

| 問題 | 意味 | 提案の例 |
|---|---|---|
| `maybe_done` | 証拠がすべて揃っているのに未完了 | 完了にできるか確認する |
| `done_without_evidence` | 完了なのに証拠が書かれていない | 証拠を足す |
| `evidence_missing` | 完了なのに証拠のファイル／語句が見つからない | 証拠を直す（完了が嘘になっている） |

- **WBS ファイルは読むだけ。** 書き換えるのは人の PR だけ（提案を承認しても、ファイルは変わらない）。
- **提案は承認待ち。** 承認するまで順位にも入らない。承認すれば普通のタスクで、`aipmo generated done` で閉じる
  （[課題管理ツールへの起票](#課題管理ツールへの起票--filing-into-the-tracker)にも回せる）。
- **問題の「回」ごとに 1 つ。** 同じ問題で提案を重ねず、却下したら同じ回では出し直さない。
- **直ったら取り下げる。** 問題が消えたら、まだ承認待ちの提案は台帳から取り下げる（古い指摘を人に見せ続けない）。
  承認・却下が済んだものは記録として残す。直ってからまた起きたら、新しい回として出る。
- **読めないときは何もしない。** WBS が壊れている・消えているときは、提案を足しも取り下げもしない
  （読めないことを「直った」と取り違えない）。理由はブリーフィングの `wbs_drift.error` に出る。

## 課題管理ツールへの起票 / Filing into the tracker

承認した対応タスクと、定期タスクを、課題管理ツール（Jira・GitHub Projects・Plane・OpenProject・
Azure DevOps）にも課題として作れます。チームが日々見ているのはそちらだからです。設定を書いたときだけ動きます。

```yaml
pmo_core:
  filing:
    tracker: jira                    # 課題を作る先(アダプタ名。config の adapters に要る)
    params: {project: PROJ}          # create_issues にそのまま渡す(Jira の project、ADO の work_item_type など)
    labels: [pmo-ai]                 # 課題に付けるラベル(ADO ではタグ)
    origins: [followup, recurring]   # 起票の対象にする由来(既定は両方)
    auto: []                         # 承認なしで常駐が起票してよい由来(既定は無し)
    max_auto_per_cycle: 5
```

- **承認が先、起票は別の許可。** 対応タスクの提案を承認した（＝仕事にしてよい）ことと、課題管理ツールに課題を
  作ってよいことは別。起票は、人が `aipmo file` か Web の「起票」ボタンで決める。承認前の提案・却下したもの・
  見送ったものは対象外。
- **`auto` は運用者が明示した由来だけ。** たとえば `auto: [recurring]` と書くと、設定に書かれた定期タスクは
  常駐の PMO Core が承認なしで起票する（設定に書くこと自体が許可なので）。対応タスクは `auto` にしても
  よいが、書かない限り人が決める。常駐が一周に起票するのは `max_auto_per_cycle` 件まで。失敗したものは
  1 時間は再試行しない。表示専用のコマンドと Web は、`auto` でも自動では起票しない。
- **冪等。** タスクの id から決めた冪等キー（ラベル・タグ・外部 ID）を課題に付けるので、作った直後に落ちて
  やり直しても課題は二重にならない（既に作られていれば、それに結び付けて `すでに作成済み` と表示する）。
- **起票したら結び付く。** 台帳のタスクの id は変えず（提案の同一性は id にある）、課題のキー（`GH:42`・
  `PROJ-7`）を記録する。以後は収集がその課題の状態・担当・完了を台帳に反映し、**同じ課題が別のタスクとして
  増えることはない**。課題管理ツール側で閉じれば、台帳でも完了（実績）になる。起票したタスクは、台帳側から
  完了にできない（課題管理ツールで閉じる）。担当の確定は、起票後はその課題にも書き戻せる。
- **担当は推測しない。** 台帳に担当がいて、そのメンバーの `accounts` にこのトラッカーのアカウントがあるときだけ
  課題の担当にする（Jira はアダプタが名前を引き当てる。Plane・OpenProject は、`accounts` が無ければ担当候補の一覧から
  名前の**完全一致**で引き当てる — [docs/TICKET-TRACKERS.md](TICKET-TRACKERS.md#担当者の引き当てplaneopenproject--resolving-assignees-by-name)）。
  定まらなければ**担当なしで起票**して、その旨を表示する。役割AIはトラッカーの担当にしない。
- **失敗は止まる理由にしない。** 失敗は台帳（`起票待ち` の一覧）に理由つきで残り、課題は作られない。直してから
  やり直せる。

```bash
aipmo file                      # 起票待ちの一覧(何も作らない)
aipmo file <id> --apply         # 起票する(課題管理ツールに課題を作る)
aipmo file --all --apply        # 起票待ちを全部
aipmo file <id> --skip          # 起票を見送る(台帳だけで使う)
```

Web 画面の PMO Core 欄に「起票待ち」が出て、実行用トークンならボタンで起票・見送りができます（閲覧用トークンには出ません）。

## できないこと / Not (yet) done

- 起票先は 1 つのトラッカーだけ（プロジェクトごとに振り分ける、はまだ）。Jira の課題タイプや優先度の名前が
  相手の設定に無いと、課題管理ツール側が拒否する（その場合は失敗として残る）。
- 実サービス（Jira・GitHub など）に対しては未検証。偽サーバーを使った実プロセスの通し確認と単体テストのみ。
- 収集は課題管理ツールの**状態**を集めるもので、コミットや PR からの進捗の推定はしない。
