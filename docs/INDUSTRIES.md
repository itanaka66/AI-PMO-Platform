# 業界別テンプレート / Industry templates

`templates/industries/<業界>/` にあります。

| 業界 | テンプレート | 内容 |
|---|---|---|
| ソフトウェア開発 | `templates/examples/` | 会議・課題・スプリント |
| 建設・施工管理 | `construction/site_meeting.yaml` | 工程会議と安全指摘 |
| マーケティング | `marketing/campaign_check.yaml` | キャンペーン進行と承認待ち |
| 製造 | `manufacturing/line_downtime_triage.yaml` | 生産ライン停止の仕分け |
| 法務・コンプライアンス | `legal/matter_deadline_triage.yaml` | 案件期限の確認と秘匿特権の扱い |

---

## 業界ごとに変わるのは語彙だけではありません

用語を差し替えただけのテンプレートは、その業界では使われません。
**業務上、扱いを変えるべき性質**があります。

Swapping the vocabulary is not enough: each field has something that must be
handled differently, not merely named differently.

### 建設 — 安全は、まとめて送ってはいけない

工程の遅れは明日取り戻せますが、**けがは取り戻せません。**

そのため安全に関する指摘は、進捗報告とは**別のチャンネルに、単独で、先に**
送ります。「本日の議事録」の見出しの下に他の報告と並べて書くと、
流し読みで飛ばされます。

さらに緊急度で分けています。人がけがをしうる状態が今あるものは即時に1件ずつ、
予定して直すものはまとめて1通に。**予定分まで即時通知にすると、
即時の重みが失われます。**

プロンプトでは「迷ったら immediate にする」と指示しています。
見落として事故になるより、確認の手間をかけるほうがはるかによいためです。

A schedule slip can be made up tomorrow; an injury cannot. Safety items go to a
separate channel, alone, and first — placed under a "today's minutes" heading
among other items they get skimmed past. Urgent items are sent one by one;
scheduled ones are batched, because making everything urgent removes the
meaning of urgent. The prompt says to err towards urgent: the cost of checking
is far below the cost of missing one.

### マーケティング — 承認待ちは、遅れとは別のもの

作業が遅れているなら担当者に確認すれば動きます。
しかし**承認待ちは、担当者に言っても動きません。** 待たせている側に
働きかける必要があります。

同じ催促の文面で送ると、**動かせない人を責めることになります。**
そのため承認待ちは分けて集計し、別の宛先・別の文面で出します。

公開日は動かせない前提で見ます。残り日数は集計して渡します
（テンプレートに計算の仕組みは無いので、渡さないとモデルが日付から
自分で数えることになります）。

Chasing an assignee moves late work but not an approval — that needs whoever is
holding it. Sent with the wording used for late work, it blames someone with no
means to act, so approvals are separated and addressed differently. The launch
date is treated as fixed, and the days remaining are counted before being
handed over.

### 製造 — 現場が悪いとは限らない

生産ラインの停止は、二つの軸で分けて扱う必要があります。

**安全に関わる停止は、単独で・即時に・専用チャンネルへ。** ロックアウト・
タグアウトのような案件を、進捗報告の見出しの下に他の停止と並べて書くと、
流し読みで飛ばされます。建設の安全指摘と同じ理由ですが、根拠は製造業に
固有です — 労働災害はロックアウト手順の省略から起きることが多く、
迷ったら安全側に倒すべき性質のものです。

**資材待ちで止まっているものは、現場ではなく調達へ向けます。** 現場の
担当者に確認しても、部品そのものは届きません。動かせるのは調達側だけです。
遅れている作業と同じ扱いで現場へ催促すると、**動かせない人を責めることに
なります。**「現場が悪い」とは限らないというのが、この業界に固有の点です。

停止時間はデータにある値をそのまま使います。テンプレートに計算の仕組みは
無いので、渡さなければモデルが自分で数えることになります。

Production-line stoppages split along two axes. Safety-related ones are sent
alone, immediately, to their own channel — same reasoning as construction's
safety line, but the grounding is manufacturing-specific: injuries there
typically trace back to a skipped lockout step, which is exactly the kind of
thing that should be over-reported rather than missed. Parts-and-supply waits
are addressed to procurement rather than the floor, since checking with the
operator cannot make a part arrive — chasing the floor with the same wording
used for ordinary delays would blame someone with no means to act, which is
the field-specific twist: the floor is not always at fault. Downtime hours are
used as given, not recomputed.

### 法務・コンプライアンス — 期限を過ぎると取り返しがつかない

この業界には、他の3つには無い制約がもう一つあります。**AI に法的な見解や
助言を述べさせてはいけません。** プロンプトでは事実（期限・状況）の報告に
限定するよう明示的に指示し、案件の是非や対応方針には触れさせません。
これは弁護士法上の要請であって、単なる好みではありません。

期限が迫っている案件は、他の業界の「安全」と同じ形で扱います —
単独・即時に、専用チャンネルへ。ただし理由は異なります。工程の遅れは
明日取り戻せますが、**法的な期限を過ぎると却下・失権・制裁のような、
やり直しのきかない効果が生じることがあります。**

相手方・裁判所・依頼者からの回答待ちの案件は、担当者への催促にしません。
担当者に確認しても、相手方や裁判所は動かせないためです
（マーケティングの承認待ちと同じ構造）。

**秘匿特権の対象案件だけに固有の扱いがもう一つあります。** 緊急度や
何を待っているかに関わらず、限定された人だけが見るチャンネルへ、
案件番号と残り日数だけを通知します。案件名も内容も出しません。
自動化された経路が秘匿特権の内容に触れる範囲は、狭いほど安全だからです。

This field carries one constraint the other three do not: **the model must
never state a legal opinion, give advice, or assess a matter's merits.** The
prompt explicitly limits it to reporting facts — a deadline, a status — not
whether the matter's position is sound or what to do about it. This is a
requirement of the legal profession's own rules, not a stylistic choice.

Deadline-critical matters get the same treatment as "safety" elsewhere — sent
alone, immediately, to their own channel — but for a different reason: a
schedule slip can be made up tomorrow, while missing a legal deadline can
carry consequences (dismissal, waived rights, sanctions) that cannot be
undone. Matters waiting on the other side, the court, or the client are never
turned into a chase aimed at the assigned attorney, since confirming with them
cannot move any of those three (the same shape as marketing's approval wait).

**Privileged matters carry one further rule unique to this field:** regardless
of urgency or what they're waiting on, they go only to a restricted channel,
identified by matter number and days remaining alone — no name, no substance.
The less an automated path touches privileged content, the safer it is.

---

## 自分の業界向けに作る / Building one for your field

**コードは要りません。** プロンプトとテンプレートの2ファイルです。

```
prompts/minutes_<業界>_ja.md          何を読み取るか
templates/industries/<業界>/*.yaml    読み取った結果をどう扱うか
```

手順:

1. 既存のものを写して始める。建設版は「拾い漏らしてはいけないものがある」
   業務、マーケティング版は「他者待ちが多い」業務の型です。近い方を選ぶ。
2. プロンプトの JSON の形を、その業界で必要な項目に変える。
3. テンプレートで、その項目をどこへ出すか決める。

**考えるべきこと:**

- **見落としてはいけないものは何か。** それは他と混ぜず、単独で送る。
- **催促して動くのは誰か。** 担当者とは限りません。
- **数えれば決まる値はどれか。** 集計はアダプタか `days_between` などの
  組み込み変換で行い、モデルには渡すだけにします。
  言語モデルは数を間違えますし、**間違えても正しそうに見えます。**
- **何も無いとき、送らないでよいか。** 毎朝「順調です」が届くチャンネルは、
  そのうち読まれなくなります。読まれなくなった通知は、危ないときにも
  読まれません。

What to think through: what must never be missed (send it alone), who can
actually unblock things (not always the assignee), which figures are countable
(compute them, do not let the model), and whether silence is the right output
when nothing is wrong.

---

## 使っている道具について / A note on the tools

これらのテンプレートは Teams・Jira・Slack を前提にしています。
**その業界の標準的な道具とは限りません。**

建設なら施工管理システム、マーケティングなら Asana や Monday.com を
使っている組織が多いはずです。その場合はアダプタの追加が要ります。
プロンプトとテンプレートだけでは届きません。

These templates assume Teams, Jira and Slack, which are **not necessarily what
the field actually uses** — construction has its own site-management systems,
marketing often runs on Asana or Monday.com. Reaching those needs an adapter;
a prompt and a template are not enough.

アダプタの書き方は `aipmo/adapters/base.py` と、既存の実装を参照してください。
署名から道具の定義が自動生成されるので、エージェントからも使えるようになります。
