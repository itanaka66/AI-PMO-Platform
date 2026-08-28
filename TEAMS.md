# Teams 連携 / Teams integration

Teams の会議記録から議事録と TODO を作ります。
**設定は Azure 側の作業が中心で、そこで止まる人がほとんどです。**
順番どおりに進めてください。

Most of the work is on the Azure side, and that is where setups stall. Follow
the order below.

---

## 先に知っておくこと / Read this first

### アプリ権限だけでは足りません

`OnlineMeetingTranscript.Read.All` に管理者同意を与えても、**それだけでは
403 が返り続けます。** Teams 側で「アプリケーションアクセスポリシー」を作り、
対象ユーザーに割り当てる必要があります。

Granting admin consent to the permission is **not sufficient** — requests keep
returning 403. Teams additionally requires an application access policy,
created and assigned to the users whose meetings you read.

403 の本文にはその理由が書かれていません。ここを知らないと、権限設定を
何度も見直して時間を溶かします。このアダプタは 403 を受けたとき、
この点を指摘するメッセージに置き換えます。

Graph's 403 body does not say this. Without knowing it you re-check the
permissions repeatedly and lose a day. The adapter replaces that 403 with a
message naming the likely cause.

### Transcript は会議終了と同時には出ません

数分かかります。会議終了イベントで即座に取りに行くと、たいてい空です。
`wait_seconds` を指定して待たせてください。

A transcript is not ready when the meeting ends; it can take minutes. Fetching
on the meeting-ended event usually returns nothing, so pass `wait_seconds`.

### 文字起こしが有効だった会議にしか存在しません

無効だった会議からは何も取れません。**これは障害ではなく設定の事実**なので、
アダプタは例外を投げず、空として返します。テンプレート側で分岐してください。

Meetings held without transcription yield nothing. **That is a configuration
fact, not a failure**, so the adapter returns empty rather than raising, and
the template branches on it.

```yaml
- id: minutes
  when: "{{ steps.transcript.output.utterance_count }} > 0"
```

この分岐が無いと、空の記録から議事録を捏造することになります。
Without this guard, minutes get invented from an empty transcript.

---

## 手順 / Steps

### 1. アプリを登録する / Register an application

Azure Portal → Microsoft Entra ID → アプリの登録 → 新規登録

- 名前: `AI-PMO`
- サポートされているアカウントの種類: この組織ディレクトリのみ

登録後、次の3つを控えます。

- **アプリケーション (クライアント) ID**
- **ディレクトリ (テナント) ID**
- **クライアントシークレット** — 証明書とシークレット → 新しいクライアント
  シークレット。**発行直後にしか値は見られません。**

The secret value is shown only once, at creation.

### 2. 権限を付与する / Grant permissions

API のアクセス許可 → アクセス許可の追加 → Microsoft Graph →
**アプリケーションの許可**（委任ではありません）

| 権限 | 用途 |
|---|---|
| `OnlineMeetingTranscript.Read.All` | Transcript の取得 |
| `OnlineMeetings.Read.All` | 参加 URL から会議を引く |
| `Calendars.Read` | 予定表（`upcoming_meetings` を使う場合） |

追加後、**「管理者の同意を与えます」を必ず押してください。**
押し忘れると、権限は一覧に出ているのに動きません。

Then press **Grant admin consent**. Skipping it leaves the permissions listed
but inert.

### 3. アプリケーションアクセスポリシー（ここが本番）

テナント管理者が Teams PowerShell で実行します。**手順2だけでは動きません。**

Run as a tenant administrator in Teams PowerShell. **Step 2 alone does not
work.**

```powershell
Connect-MicrosoftTeams

New-CsApplicationAccessPolicy `
    -Identity aipmo-read `
    -AppIds "<アプリケーション ID>" `
    -Description "AI-PMO transcript access"

# 特定のユーザーに割り当てる
Grant-CsApplicationAccessPolicy `
    -PolicyName aipmo-read `
    -Identity "tanaka@example.com"

# または組織全体に
Grant-CsApplicationAccessPolicy -PolicyName aipmo-read -Global
```

> **反映に時間がかかります。** 数十分かかることがあります。
> 直後に試して 403 が返っても、設定が誤っているとは限りません。
> しばらく置いてから再試行してください。
>
> **Propagation takes time** — sometimes tens of minutes. A 403 immediately
> afterwards does not necessarily mean the configuration is wrong.

### 4. 設定を書く / Configure

```yaml
adapters:
  mode: real
  teams:
    tenant_id: ${AZURE_TENANT_ID}
    client_id: ${AZURE_CLIENT_ID}
    client_secret: ${AZURE_CLIENT_SECRET}
    organiser_id: tanaka@example.com
```

```bash
# .env に置く。config.yaml には資格情報を書かない。
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
```

**`organiser_id` が要る理由** — アプリ専用認証にはサインインした利用者が
いないので、「誰の会議か」を指定しないと Graph は会議を特定できません。

App-only auth has no signed-in user, so Graph cannot infer whose meetings to
look at; the organiser must be named.

### 5. 確認する / Verify

```bash
aipmo doctor
```

`✓ teams` が出れば、認証は通っています。
アクセスポリシーの確認は、実際に Transcript を取るまで分かりません。

A `✓` confirms authentication. Whether the access policy is in place only
shows when a transcript is actually fetched.

---

## 使えるアクション / Available actions

| アクション | 内容 |
|---|---|
| `find_meeting` | 参加 URL → 会議 ID |
| `list_transcripts` | 会議に紐づく Transcript の一覧 |
| `get_transcript` | 本文を取得し、発話者ごとに整形 |
| `upcoming_meetings` | 予定表（参加 URL を含む） |

いずれも読み取りのみです。エージェントに渡しても、書き込みは発生しません。

All are read-only, so handing them to an agent introduces no write path.

### 参加 URL と会議 ID は別物です

予定表や通知が持っているのは**参加 URL** で、会議 ID ではありません。
`find_meeting` で変換してから `get_transcript` に渡します。

A calendar entry carries the **join URL**, not the meeting id. Convert with
`find_meeting` first.

---

## Transcript の整形 / What happens to the transcript

Graph が返すのは WebVTT です。そのまま LLM に渡していません。

1時間の会議は数百の細切れの字幕になります。**1つの発話が複数に割れる**ので、
素のまま渡すと同じ発話者の行が延々と並び、モデルは「誰が何を言ったか」を
再構成することに文脈を使ってしまいます。

連結してから渡します。ただし**間が空いたら別の発話**として扱います。
無条件に繋ぐと、会議の前半と後半の発言が1つになって時系列が壊れるためです。

An hour of meeting becomes hundreds of fragments, with single utterances split
across several. Passed through raw, the model spends its context reassembling
who said what. Fragments are merged first — but a long silence starts a new
utterance, since merging unconditionally would fuse remarks from opposite ends
of the meeting.

時刻は既定で落とします。議事録に必要なのは順序であって秒数ではなく、
数百行分の `00:12:34` はトークンを食うだけです。

Timestamps are dropped by default: minutes need the order, not the seconds.

---

## 動く例 / A working example

```bash
aipmo validate templates/examples/meeting_to_tasks.yaml
```

会議 → 議事録 → TODO → Jira 登録 → Slack 通知 の一本道です。
文字起こしが無効だった会議では、途中で止まります。

Meeting to minutes to tasks to Jira to Slack, stopping partway when there was
no transcription.

---

## うまくいかないとき / When it does not work

| 症状 | 見るところ |
|---|---|
| 403 が返る | アクセスポリシー（手順3）。反映待ちの可能性もあります |
| 401 が返る | シークレットの期限切れ。既定は2年ですが短く設定されることも |
| 会議が見つからない | `organiser_id` が本当に主催者か。出席者では引けません |
| Transcript が空 | 会議中に文字起こしが有効だったか。`wait_seconds` を伸ばす |
| 発話者が「不明」 | ゲスト参加者は名前が付かないことがあります |
