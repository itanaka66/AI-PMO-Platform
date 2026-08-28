# Jira と Slack / Jira and Slack

課題の起票と通知を実際に行うための設定です。

---

## Jira

### 準備 / Setup

1. **API トークンを作る** — https://id.atlassian.com/manage-profile/security/api-tokens
   「API トークンを作成」で発行します。**発行直後にしか値は見られません。**
   The value is shown only once, at creation.

2. **設定に書く / Configure**

```yaml
adapters:
  mode: real
  jira:
    site: https://yourcompany.atlassian.net
    email: ${JIRA_EMAIL}          # トークンを作った本人のアドレス
    api_token: ${JIRA_API_TOKEN}
    project: PROJ
    issue_type: Task
```

```bash
# .env に置く / put these in .env
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
```

`email` は**トークンを発行した本人のアドレス**です。ここが違うと 401 になります。
The email must be the one that issued the token, or you get a 401.

3. **確認 / Verify**

```bash
aipmo doctor
```

### 知っておくべき仕様 / What the API demands

**旧い検索エンドポイントは削除されました。**
`/rest/api/3/search` は 2025年に停止され、いまは 410 を返します。
このアダプタは `/rest/api/3/search/jql` を使い、ページ送りもトークン方式です。
古い記事のコードをそのまま持ってくると動きません。

`/rest/api/3/search` was shut down during 2025 and now answers 410. This
adapter uses `/rest/api/3/search/jql` with token pagination. Code copied from
older write-ups will not work.

**新しい検索は既定で id しか返しません。**
旧エンドポイントは全項目が既定でした。移行して「結果は返るのに中身が空」に
なるのはこれが原因です。このアダプタは項目を必ず明示します。

The new endpoint returns id alone by default, which is why a migrated query
comes back looking empty. This adapter always names the fields.

**説明文は ADF でなければ通りません。**
v3 の `description` は Atlassian Document Format です。素の文字列は 400 に
なります。変換はアダプタ側で行うので、テンプレートには普通の文章を書けます。

In v3, `description` must be Atlassian Document Format; a plain string is
rejected. The conversion happens in the adapter, so templates carry plain prose.

**担当者は accountId でしか指定できません。**
氏名やメールアドレスでは設定できないため、検索して引き当てます。

Assignee accepts only an accountId — not a name, not an email — so it is looked
up first.

> **引き当てられなかった場合は未割り当てで起票します。**
> ここで失敗させると、担当者が1人分からないだけで**課題が1件も作られません。**
> 未割り当ての課題は後から直せますが、作られなかった課題は取り戻せません。
> 誰が漏れたかは `unassigned` に入ります。
>
> An unresolved name leaves the issue unassigned rather than failing. Failing
> would mean one unrecognised person prevents every issue from being created;
> an unassigned issue can be fixed later, a missing one cannot. The names are
> returned in `unassigned`.

### 二重起票の防止 / Not filing twice

Jira に冪等キーの仕組みはありません。代わりに実行ごとのキーをラベルとして
残し、**作る前にそのラベルで検索します。** 同じ会議を2回処理しても課題は
二重になりません。

Jira has no idempotency mechanism. The run key is carried as a label and
searched for before creating, so reprocessing a meeting does not duplicate its
tasks.

一部だけ失敗した場合は `failed` に入ります。**成功として返しません。**
Partial failures appear in `failed` rather than being reported as success.

### 使えるアクション / Actions

| アクション | 書き込み |
|---|---|
| `search` | — |
| `find_overdue` | — |
| `create_issues` | ✓ |
| `add_comment` | ✓ |

---

## Slack

### 準備 / Setup

1. **アプリを作る** — https://api.slack.com/apps → Create New App → From scratch

2. **スコープを付ける** — OAuth & Permissions → Bot Token Scopes

| スコープ | 用途 |
|---|---|
| `chat:write` | メッセージの送信（必須） |
| `users:read.email` | メールから利用者を引く（メンションに使う場合） |
| `channels:read` | チャンネル一覧 |

3. **ワークスペースにインストール** — Install to Workspace。
   `xoxb-` で始まるボットトークンが発行されます。

4. **ボットをチャンネルに招待する**

```
/invite @yourbot
```

> **これを忘れると `not_in_channel` で失敗します。**
> スコープを付けただけでは、そのチャンネルには投稿できません。
> プライベートチャンネルでは特に忘れがちです。
>
> Granting the scope does not admit the bot to a channel. Without the invite
> every send fails with `not_in_channel` — easy to miss on private channels.

5. **設定に書く / Configure**

```yaml
adapters:
  slack:
    token: ${SLACK_BOT_TOKEN}
    default_channel: "#project-updates"
```

### 知っておくべき仕様 / What the API does

**Slack は失敗しても HTTP 200 を返します。**

```json
{"ok": false, "error": "channel_not_found"}
```

ステータスコードだけを見る実装は、**通知が1件も届いていないのに
「全部成功」と報告し続けます。** 何も壊れて見えないので、
気づくまでに時間がかかります。このアダプタは本文の `ok` を検査します。

Slack answers 200 even when the call failed; success lives in the body's `ok`
field. Code that checks only the status code reports every send as successful
while nothing arrives — and because nothing looks broken, it goes unnoticed.
This adapter checks the body.

**よくあるエラーには対処法を添えます。**

| エラー | 対処 |
|---|---|
| `not_in_channel` | `/invite @yourbot` |
| `channel_not_found` | 名前か ID を確認。プライベートは参加が必要 |
| `invalid_auth` | トークンを確認（`xoxb-` で始まります） |
| `missing_scope` | `chat:write` を付ける |
| `msg_too_long` | 要約するか分割する |

再送しても実らないものは、待たずにすぐ諦めます。
Errors that retrying cannot fix fail immediately rather than sleeping.

**送信制限があります。** 429 のときは `Retry-After` に従って待ちます。
無視して押し続けると絞りが長引くためです。

### 二重送信について / On sending twice

**Slack に冪等キーの仕組みはありません。** アダプタ側で二重送信を
防ぐことはできないので、テンプレート側で担保してください。

Slack has no idempotency mechanism, and one cannot be built in the adapter.
Guard against double sends in the template.

```yaml
- id: notify
  when: "{{ steps.register.output.count }} > 0"
```

### 使えるアクション / Actions

| アクション | 書き込み |
|---|---|
| `post_message` | ✓ |
| `reply_in_thread` | ✓ |
| `find_user` | — |
| `list_channels` | — |

---

## エージェントに渡すとき / Handing these to an agent

読み取りだけを渡せます。アダプタ名を書いただけでは、
**課題の起票も通知も行われません。**

Read-only access is the default: naming the adapter does not grant issue
creation or message sending.

```yaml
- id: investigate
  agent:
    tools: [jira]          # search と find_overdue のみ
    allow_writes: false
  prompt_inline: 遅延の状況を調べて報告してください

- id: notify               # 送るかどうかはテンプレートが決める
  adapter: slack
  action: post_message
  inputs:
    text: "{{ steps.investigate.output.answer }}"
```

詳しくは [AGENTS.md](AGENTS.md) を見てください。
