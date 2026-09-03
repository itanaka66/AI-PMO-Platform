# 課題管理ツール（GitHub Projects・Plane・OpenProject・Azure DevOps）
Ticket trackers (GitHub Projects, Plane, OpenProject, Azure DevOps)

[Jira](JIRA-SLACK.md) 以外の課題管理ツールを使うための設定です。どれも
`search` / `find_overdue`（GitHub Projects を除く）/ `create_issues` /
`update_issue` / `add_comment` を同じ形で公開しており、テンプレートは
Jira 向けに書いたものとほぼ同じ発想で書けます。ただし各ツールのデータ
モデルの違いはそのまま残るため、フィールド名は完全には揃えていません
——揃えるためにツール固有の意味を消してしまうより、違いを残す方を
選んでいます。

Configuration for ticket trackers other than [Jira](JIRA-SLACK.md). Every
one of them exposes `search` / `find_overdue` (except GitHub Projects) /
`create_issues` / `update_issue` / `add_comment` in the same shape, so a
template written for Jira translates almost directly. Field names are not
fully unified across them, though — flattening away each tool's own data
model in the name of consistency would have cost more than the
inconsistency does.

---

## GitHub Projects

Projects v2 のボード自体（カスタムフィールド・ステータス列）は操作
しません。ボードのアイテムは通常リポジトリの Issue そのものなので、
Issues REST API を操作します。ボード固有のフィールドを書き換えるには
GraphQL でボードごとのフィールド ID を事前に調べる必要があり、
リポジトリ横断で汎用には書けないため、意図的に対象外にしています。

This does not manipulate the Projects v2 board itself (its custom fields,
status columns). A board's items are ordinary repository issues, so this
operates on the Issues REST API instead. Writing a board-specific field
needs that board's own GraphQL field IDs looked up ahead of time, which
cannot be written generically across repositories — so it is deliberately
out of scope.

### 準備 / Setup

1. **Personal Access Token を作る** —
   https://github.com/settings/tokens?type=beta （Fine-grained）で
   対象リポジトリの **Issues: Read and write** を付与します。

2. **設定に書く / Configure**

```yaml
adapters:
  mode: real
  github_projects:
    token: ${GITHUB_TOKEN}
    owner: your-org
    repo: your-repo
```

```bash
GITHUB_TOKEN=github_pat_...
```

3. **確認 / Verify**

```bash
aipmo doctor
```

### 知っておくべき仕様 / What the API demands

**締切日を持つ標準の項目がありません。** そのため `find_overdue` は
このアダプタにはありません。Projects v2 の日付カスタムフィールドは
ボードごとに ID が違い、汎用には書けません。

There is no built-in due-date field on issues, so `find_overdue` does not
exist here. Projects v2 date custom fields exist, but their IDs differ per
board and cannot be addressed generically.

### 二重起票の防止 / Not filing twice

Jira と同じ形。実行ごとのキーをラベル（`aipmo-<キー>`）として残し、
作る前にそのラベルで検索します。

Same shape as Jira: the run key is carried as a label (`aipmo-<key>`) and
searched for before creating.

### 使えるアクション / Actions

| アクション | 書き込み |
|---|---|
| `search` | — |
| `create_issues` | ✓ |
| `update_issue` | ✓ |
| `add_comment` | ✓ |

---

## Plane

### 準備 / Setup

1. **API key を作る** — ワークスペース設定 → API Tokens

2. **設定に書く / Configure**

```yaml
adapters:
  mode: real
  plane:
    api_key: ${PLANE_API_KEY}
    workspace_slug: your-workspace
    project_id: your-project-uuid
    base_url: https://api.plane.so   # セルフホストならそのURL / self-hosted: your own URL
```

```bash
PLANE_API_KEY=plane_api_...
```

3. **確認 / Verify**

```bash
aipmo doctor
```

### 知っておくべき仕様 / What the API demands

**冪等キーは `external_id` / `external_source` に載せます。** ラベルを
作って探す（Jira・GitHub のやり方）代わりに、Plane が課題そのものに
持つこの2項目を使います——Plane の連携用 API はもとよりこれを
「外部システムとの重複防止」のために用意しています。

The idempotency key rides on Plane's own `external_id` / `external_source`
fields, rather than a manufactured label — Plane's integration-facing API
already provides these two fields specifically to prevent duplicates from
an external system.

**優先度・状態はワークスペースごとに違います。** ラベルの正規化は
行わず、値をそのまま渡します。

Priority and state labels vary per workspace configuration; values are
passed through as given, not normalized.

**`find_overdue` はクライアント側での絞り込みです。** Plane の REST API
に自由記述のクエリ言語が無いため、一覧を取得してから `target_date` で
判定しています。

`find_overdue` filters client-side: Plane's REST API has no free-form
query language, so this fetches a page of issues and checks `target_date`
itself.

### 使えるアクション / Actions

| アクション | 書き込み |
|---|---|
| `search` | — |
| `find_overdue` | — |
| `create_issues` | ✓ |
| `update_issue` | ✓ |
| `add_comment` | ✓ |

---

## OpenProject

### 準備 / Setup

1. **API key を作る** — マイアカウント → Access tokens → API

2. **設定に書く / Configure**

```yaml
adapters:
  mode: real
  openproject:
    base_url: https://your-instance.openproject.com
    api_key: ${OPENPROJECT_API_KEY}
    project_id: your-project-identifier
    work_package_type_id: 1   # 任意 / optional — 省略時はサーバー既定 / omit for the server default
```

```bash
OPENPROJECT_API_KEY=...
```

3. **確認 / Verify**

```bash
aipmo doctor
```

### 知っておくべき仕様 / What the API demands

**更新には `lockVersion`（楽観ロック）が要ります。** API 仕様上、
更新のたびにまず現在の Work Package を取得して版番号を読み、それを
付けて PATCH しなければなりません。呼び出し側にその手順を意識させない
よう、`update_issue` の内部で自動的に行います。版が古いまま送ると
409 になります——それは誰かが先に更新した合図なので、取得し直して
やり直してください。

Updates require `lockVersion` (optimistic concurrency): the API demands
fetching the current work package first to read its version number, then
sending that back with the PATCH. `update_issue` does this internally.
Sending a stale version answers 409 — a sign someone else updated it
first; re-fetch and retry.

**汎用のラベル/タグがありません。** そのため冪等キーは説明文の先頭に
`[aipmo:キー]` として埋め込み、次回はそれを含む説明文を検索します。

There is no generic tag/label field, so the idempotency key is embedded
as `[aipmo:KEY]` at the start of the description, and searched for on the
next run.

### 使えるアクション / Actions

| アクション | 書き込み |
|---|---|
| `search` | — |
| `find_overdue` | — |
| `create_issues` | ✓ |
| `update_issue` | ✓ |
| `add_comment` | ✓ |

---

## Azure DevOps

### 準備 / Setup

1. **Personal Access Token を作る** — User settings → Personal access
   tokens → Work Items: Read & Write

2. **設定に書く / Configure**

```yaml
adapters:
  mode: real
  azure_devops:
    organization: your-org
    project: YourProject
    pat: ${AZURE_DEVOPS_PAT}
    work_item_type: Task
    due_date_field: Microsoft.VSTS.Scheduling.DueDate   # 任意 / optional
```

```bash
AZURE_DEVOPS_PAT=...
```

3. **確認 / Verify**

```bash
aipmo doctor
```

### 知っておくべき仕様 / What the API demands

**フィールド名はプロセステンプレートごとに違います。** Agile / Scrum /
CMMI / カスタムのどれを使っているかで、締切日のフィールド名が変わり
ます。既定は Agile の `Microsoft.VSTS.Scheduling.DueDate` ですが、
存在しないテンプレートでは `due_date_field` で上書きしてください。

Field names differ by process template (Agile / Scrum / CMMI / custom).
The default due-date field is Agile's
`Microsoft.VSTS.Scheduling.DueDate`; override it with `due_date_field`
when your template does not have that field.

**検索は2段階です。** WIQL は ID の一覧しか返さないため、`search` は
内部で詳細取得をもう一度呼びます。

Search is two steps: WIQL returns only a list of IDs, so `search`
internally makes a second call for the details.

**冪等キーはタグ（System.Tags）に載せます。** Jira のラベル検索に
相当する `System.Tags CONTAINS` が WIQL にあります。

The idempotency key rides on tags (System.Tags) — WIQL's
`System.Tags CONTAINS` plays the role Jira's label search does.

### 使えるアクション / Actions

| アクション | 書き込み |
|---|---|
| `search` | — |
| `find_overdue` | — |
| `create_issues` | ✓ |
| `update_issue` | ✓ |
| `add_comment` | ✓ |
