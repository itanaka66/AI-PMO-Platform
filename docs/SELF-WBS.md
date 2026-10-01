# PMO AI 自身の開発を WBS で管理する運用 / Managing this project's own development with a WBS

PMO AI が顧客のプロジェクトにやらせようとしていること — WBS を持ち、進捗を数え、
遅れを予測し、人が承認して計画を直す — を、**このプロジェクト自身の開発**に使います。
自分で使えないものを他人に勧めない、というだけでなく、WBS 運用の弱点を
自分の手で先に踏むためです。

What PMO AI asks of its users — keep a WBS, count progress, forecast slippage,
revise the plan with human approval — this project applies to **its own
development**.

| 部品 | 場所 |
|---|---|
| WBS 本体（人が書き、PR でレビューする） | [wbs/aipmo.yaml](../wbs/aipmo.yaml) |
| 読み込み・検証・集計 | [aipmo/wbs.py](../aipmo/wbs.py) |
| テンプレートから使うアダプタ（読み取り専用） | [aipmo/adapters/wbs_file.py](../aipmo/adapters/wbs_file.py) |
| 週次の報告 | [templates/examples/self_development.yaml](../templates/examples/self_development.yaml) |
| CI での検証 | `.github/workflows/tests.yml`（`aipmo wbs check`） |

---

## ルール / Rules

1. **WBS を変えるのは人だけ、PR で。** AI（エージェント・テンプレート）は WBS ファイルを
   読むだけで、書き換えない。変更の記録は git の履歴。
2. **「完了」には証拠がいる。** `status: done` の作業は、`evidence` に挙げたファイル
   （`path::語句` なら、そのファイルに語句が含まれること）が実在しなければならない。
   無ければ CI が落ちる。申告だけで完了にならない。
3. **着手したら `in_progress`、終えたら `done` と証拠。** 証拠が先に揃っているのに
   `done` にし忘れた作業は、`maybe_done` の警告で知らせる。
4. **見積り（`effort`）は相対的な大きさの目安**（1 = 半日程度）。実績を測った値では
   ない。見積りの無い未完了は `unestimated` で警告される（予測から外れる）。
5. **作業を足す・分けるのも PR。** 依存（`depends_on`）は葉どうしにだけ書く。

## 見方 / Reading it

```bash
aipmo wbs check            # 誤りと証拠の欠けを調べる（error があれば終了コード 1）
aipmo wbs check --strict   # warning も失敗にする
aipmo wbs status           # 進捗・速度・完了見込み・クリティカルパス・次に着手できる作業
aipmo wbs status --json
```

- **error**（CI を落とす）: id の重複／引用符の無い id（`1.10` は数値 1.1 になる）、
  存在しない依存先、循環、不正な status・日付・effort、**完了なのに証拠が無い**、
  リポジトリ外を指す証拠のパス。
- **warning**: 証拠の無い完了（`done_without_evidence`）、証拠が揃っているのに未完了
  （`maybe_done`）、完了なのに依存先が未完了、期限超過、見積りなし、全体の期限超過。
- 構造に error があるときは、**予測をしない**（偽の数字を出さないため）。

### 速度と完了見込み

速度 = 直近 `velocity_window_days`（既定 28）日に完了した作業の見積り合計 ÷ 日数。
完了日 `done_on` は、証拠が最後に揃った日（git の履歴から取った）。実績が無ければ
`velocity_per_day` の申告値、それも無ければ**予測しない**。`deadline` を決めると、
期限に対する遅れ（日）も出る。

> 見積りが事後の目安である以上、完了見込みは「このペースが続けば」の目安です。
> 日付を約束するものではありません。


## PR で更新漏れを知らせる / Telling the PR about drift

WBS は人が PR で更新します。実装を足したのに WBS を更新し忘れる（証拠が揃ったのに未完了＝`maybe_done`）、
証拠のファイルを消したのに「完了」のままにする（`evidence_missing`）、は `aipmo wbs check` では warning や error に
なるだけで、PR を見ている人の目に入りません。`wbs-drift` ワークフロー（`.github/workflows/wbs-drift.yml`）が、
PR に**コメント**で知らせます。

```bash
aipmo wbs notify --base origin/main                      # コメントの本文を表示するだけ（何も書かない）
aipmo wbs notify --base origin/main --pr 12 --post       # PR #12 に、目印つきのコメントを作る／更新する
aipmo wbs notify --changed src/a.py docs/b.md            # git を使わず、変更ファイルを直接渡す
```

- **この PR に関係するものを先に出す。** PR で変えたファイル（`--base` との差分。消したファイルも含む）が、ある作業の
  証拠に当たるとき、「この PR に関係する」として先頭に並べる。証拠の一部だけ揃った作業は、進み具合として添える。
- **完了にする書き方を添える。** `status: done` と `done_on:`（基準日）。WBS を書き換えるのは PR の著者で、ここは書かない。
- **コメントは 1 つだけ。** 目印（`<!-- aipmo-wbs-drift -->`）で始まる、**Bot（`GITHUB_TOKEN`）が書いた**コメントを
  見つけて更新する。PR を更新するたびに増えない。変わらなければ書かない。直れば「ありません」に更新し、もともと
  指摘が無ければ何も投稿しない。個人のトークンで書くときは `--author その login` を渡す。他の人が目印を真似て
  書いたコメントは、更新の対象にしない。
- **PR の著者が変えられる文字を、そのまま流さない。** 作業名・パスは PR で変えられるので、HTML・メンション・
  バッククォート・見出しを無害にし、長さを切る。
- **PR を失敗にしない。** 知らせるだけ（失敗にするのは CI の `aipmo wbs check`）。ワークフローは `continue-on-error`、
  権限は `contents: read` と `pull-requests: write` だけ、`pull_request_target` は使わない。**fork からの PR では
  `GITHUB_TOKEN` が読み取り専用なので、動かさない**（コメントされない）。
- 既定は表示だけ。GitHub に書くのは `--post` を付けたときだけ（トークンは環境変数 `GITHUB_TOKEN`、リポジトリは
  `GITHUB_REPOSITORY`、API の場所は `GITHUB_API_URL`）。

## 週次の運用 / Weekly cadence

`config.yaml` にアダプタを有効にして、`aipmo schedule` を動かします。

```yaml
adapters:
  wbs_file: { root: ., file: wbs/aipmo.yaml }
  slack: { ... }
```

`self_development` テンプレートが毎週月曜 9:00 に WBS を読み、状況を Slack の
`report_channel` に報告します。同じ出力は **Task Engine** にも入るので、未完了の作業は
他のテンプレートのタスクと同じ台帳（`aipmo tasks`）で順位付けされ、担当の提案
（`aipmo assign`）も出ます。画面（`aipmo serve`）の PMO Core 欄にも出ます。

| 週に一度、人がやること |
|---|
| 報告の「要修正」「要確認」を見て、WBS を PR で直す（`maybe_done` は完了にする、など） |
| 「次に着手できる」から、今週の作業を選ぶ |
| 速度が落ちていれば、原因を見て作業を分けるか、期限を見直す |


## WBS の変更提案を承認して、ファイルへ反映する / Approving a replan proposal

WBS 再計画 AI（`wbs_replan`）の提案は、PostgreSQL に**承認待ち**で記録されます。提案の `diff` に、決まった形の
変更の一覧（`changes`）が入っていれば、人が承認したときに **WBS ファイルへ反映**されます
（`templates/examples/wbs_file_replan.yaml` が、WBS ファイルを読んでそれを提案する例）。

```json
{"changes": [
  {"op": "set", "node": "3.5", "field": "due", "value": "2026-11-01"},
  {"op": "add_evidence", "node": "3.5", "values": ["aipmo/foo.py::def bar"]},
  {"op": "add", "parent": "3", "node": {"id": "3.13", "name": "分けた作業", "effort": 2}}
]}
```

操作は `set`（`name` `status` `effort` `priority` `due` `owner` `notes` `done_on` `depends_on`）、`add_evidence`、
`add`（すでに子を持つ親の下に作業を足す）だけです。**消す・id を変える・動かすことはできません。**
詳しい仕様は [aipmo/wbs_edit.py](../aipmo/wbs_edit.py)。

```yaml
adapters:
  postgres: { ... }
  wbs_replan: { file: wbs/aipmo.yaml, root: . }   # 反映先。あると提案の時点でファイルに当てて確かめる
```

```bash
aipmo wbs proposals                  # 承認待ちの一覧（反映できる形かも出る）
aipmo wbs proposals show ID          # 提案の中身と、WBS ファイルがどう変わるか（何も書かない）
aipmo wbs proposals approve ID       # 承認して、WBS ファイルへ反映する
aipmo wbs proposals reject ID        # 却下する（ファイルは変えない）
aipmo wbs proposals apply ID         # 承認済みの提案を反映し直す（反映に失敗したとき）
```

Web 画面の承認ボタンも同じです（`adapters.wbs_replan.file` があるとき、承認が反映までひと続きになる）。

守ること:

- **反映の前に、反映後の内容まで作って検証する。** 結果を WBS として読み直し、(1) 新しい誤りが増えない、
  (2) 完了にするものには `done_on` と実在する証拠がある、(3) 頼んでいないノードが変わっていない、を満たさなければ、
  **何も書かず、提案は承認待ちのまま**理由を返す。
- **書式を壊さない。** YAML を作り直さず、対象の行だけを書き換える（コメント・並び・引用符・改行コードはそのまま）。
  反映後は `git diff` で確かめてコミット（PR）する。
- **対象の WBS が違えば反映しない。** 提案の `wbs_id` とファイルの `wbs.id` が同じときだけ。
- **競合しない。** 読んだ後にファイルが書き換えられていたら書かない（承認は済み、`apply` で反映し直せる）。
- **二重に反映しない。** 反映した提案は判断ログに記録され、もう一度反映すると、その後の人の修正を戻して
  しまうので `apply` は止まる（`--force` で上書き）。
- **決まった形でない提案**（自由な文章の再計画案）は、これまでどおり承認の記録だけで、ファイルは変わらない。
- 反映を実行するのは**人の確定した操作だけ**（CLI・Web の承認）。常駐は WBS ファイルを書かない。

## できないこと / Not (yet) done

- **WBS の変更提案の置き場は PostgreSQL**（`wbs_replan`）。PostgreSQL が無い環境では、提案を
  貯めて承認する流れは使えない（台帳に置く版は未実装。WBS 項 5.7）。
- **担当者（`owner`）の書き戻しは無い。** Task Engine で担当を確定しても、台帳に
  残るだけで WBS ファイルは変わらない（読み取り専用のため）。
- 証拠は「ファイルと語句が在る」ことの確認で、**中身が正しく動くこと**までは
  確かめない（それはテストの仕事）。証拠に対応するテストを挙げておくと、
  「完了」の根拠が強くなる。
