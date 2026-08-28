# アジャイル対応 / Agile support

スプリントとボードは、課題とは別の API (`/rest/agile/1.0`) にあります。
認証は同じなので、Jira の設定をそのまま使えます。

Sprints and boards live behind a separate API. The credentials are the same, so
the Jira configuration carries over.

---

## 設定 / Configuration

```yaml
adapters:
  mode: real
  jira:
    site: https://yourcompany.atlassian.net
    email: ${JIRA_EMAIL}
    api_token: ${JIRA_API_TOKEN}
    project: PROJ
  agile:
    board_id: 1        # list_boards で確認できます
```

`agile` は `jira` の設定を引き継ぐので、資格情報を二重に書く必要はありません。
The agile block inherits the Jira settings; credentials are not repeated.

ボード ID が分からない場合:

```bash
aipmo run <一時テンプレート>   # agile.list_boards を呼ぶ
```

---

## 使えるアクション / Actions

| アクション | 内容 | 書き込み |
|---|---|---|
| `list_boards` | ボードの一覧 | — |
| `active_sprint` | 進行中のスプリント（残日数つき） | — |
| `sprint_issues` | スプリントの課題と集計 | — |
| `backlog` | バックログ | — |
| `move_to_sprint` | 課題をスプリントに入れる | ✓ |

---

## 集計は AI にやらせません / Arithmetic is not the model's job

完了ポイント数、完了率、残り日数は、**数えれば決まる値**です。
言語モデルに数えさせると間違えますし、**間違えても正しそうに見えます。**

そこでアダプタ側で集計し、モデルには数字を渡して**解釈だけ**させます。
Slack に出す数値も、集計結果をそのまま貼ります。言い換えの過程で数字が
変わっても、読む側には見分けがつかないためです。

Completed points, percentage and days remaining are countable facts. A language
model miscounts them, and does so plausibly. They are computed in the adapter;
the model is given the numbers and asked only what they mean. The figures posted
to Slack come from the aggregation, not from the model — if a number changed
while being rephrased, a reader would have no way to tell.

`sprint_issues` が返すもの:

```
count            課題の総数
done_count       完了した件数
points_total     ポイント合計
points_done      完了ポイント
percent_done     完了率
percent_basis    "points" か "count"（分母が違うので明示します）
unestimated      見積もりの無い課題
unassigned       担当者未定で未完了の課題
```

---

## 知っておくべきこと / What to know

### ストーリーポイントの項目 ID は環境ごとに違います

`customfield_10016` と書いてある記事が多いですが、**それはその人の環境の値**です。
別の環境では別の番号になり、しかも**エラーになりません。値が取れずに全件が
「見積もり無し」になる**だけです。

ボード設定から、そのボードで実際に使われている項目 ID を引き当てています。

Articles quoting `customfield_10016` are quoting their own instance. Elsewhere
the number differs, and nothing errors — the values simply come back empty and
everything looks unestimated. The id is read from the board's configuration.

> **全件が「見積もり無し」に見えたら、設定を疑ってください。**
> チームが本当に見積もっていないのか、項目を引けていないのかは、
> 出力だけでは区別がつきません。
>
> If nothing appears estimated, check the configuration: the output cannot
> distinguish a team that does not estimate from a field that was not found.

### バーンダウンの API はありません

画面のバーンダウンは内部のエンドポイントで描かれており、外からは取れません。
進捗は課題の状態とポイントから、こちらで集計しています。

Jira's burndown chart is drawn from a private endpoint and cannot be fetched.
Progress is aggregated here instead.

### 見積もりの無い課題は、完了率を実態より高く見せます

分母から抜け落ちるためです。**「順調に見えるのに終わらない」の主要な原因**なので、
`unestimated` を必ず出しています。報告のプロンプトでも、あれば触れるよう
指示しています。

Unestimated work drops out of the denominator, which is how a sprint looks
healthy right up until it does not finish.

### カンバンボードにスプリントはありません

`active_sprint` は失敗せず、`active: false` と理由を返します。
設定の事実であって障害ではないためです。スプリント間の谷間も同様です。

A kanban board has no sprints, and there are gaps between sprints. Neither is a
fault, so this returns `active: false` with a reason rather than raising.

---

## WBS との関係 / Where WBS fits

アジャイルでは **WBS は参考程度**の扱いにしています。マイルストーンの確認に
使う程度で、厳密な作業分解はスプリントの運用と噛み合いません。

`wbs_from_meeting` は使えますが、**そのまま計画として使わないでください。**
草案としてチームに渡すためのものです。

Under agile, WBS is treated as a reference — useful for checking milestones,
but a strict work breakdown does not fit how sprints run. The WBS template is
available, but its output is a draft for the team, not a plan.

---

## 動く例 / A working example

`templates/examples/sprint_health.yaml` — 平日の朝、スプリントの状況を確認します。

```bash
aipmo validate templates/examples/sprint_health.yaml
aipmo schedule --list
```

**問題が無ければ何も送りません。** 進行中のスプリントが無いときも同様です。

毎朝「順調です」が届くチャンネルは、そのうち読まれなくなります。
そして**読まれなくなった通知は、危ないときにも読まれません。**

Nothing is sent when there is nothing wrong, and nothing between sprints
either. A channel that receives "all fine" every morning stops being read — and
a notification nobody reads is not read when it matters either.

見積もりが1件も読めなかった場合だけは知らせます。項目 ID を引けていない
可能性が高く、放置すると進捗の報告そのものが無意味になるためです。

The one thing it does report is finding no estimates at all: that usually means
the field id was not resolved, and left alone it makes every later report
meaningless.
