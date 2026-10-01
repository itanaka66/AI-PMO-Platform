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

生成したタスクは**台帳だけ**にあり、課題管理ツールには作りません（外の世界を変えるのは、人が確定した
操作だけ、という方針のまま）。

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
- 担当を確定しても、書き戻す先が無いので台帳にだけ残る。
- 開いている間は期限切れで消えない。

## できないこと / Not (yet) done

- 承認した提案を、課題管理ツールにも起票すること（WBS の項 6.11）。いまは台帳の中で完結する。
- WBS の更新漏れ・証拠の欠けを、対応タスクの提案にすること（項 6.12）。
- 収集は課題管理ツールの**状態**を集めるもので、コミットや PR からの進捗の推定はしない。
