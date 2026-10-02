# 台帳と、その隣に置くもの / The ledger and what lives beside it

PMO Core は、タスクの台帳のほかに、次のものを残します。

| 名前 | 中身 |
|---|---|
| `pmo-briefing.json` | 直近のブリーフィング（画面・`aipmo pmo` が読む） |
| `pmo-core-state.json` | 警告の継続・通知の履歴・役割AIの件数など、周をまたぐ状態 |
| `pmo-learned.json` | 学習した補正 |
| `pmo-judgment-control.json` | 自律的な判断の一時停止・遮断器の解除（CLI が書き、常駐が読む） |
| `pmo-decisions.jsonl` | 判断ログ（追記だけ。レビュー件数・WBS 反映の記録もここから数える） |

これらは長いあいだ、台帳（SQLite）の隣の**ローカルファイル**でした。そのため、常駐の `aipmo schedule` と
画面の `aipmo serve` を**別のホストに分ける**と、serve はブリーフィングも判断ログも読めませんでした
（台帳は PostgreSQL で共有できても、隣のファイルは共有できない）。

## 置き場の選び方 / Choosing the home

```yaml
task_engine:
  backend: postgres        # sqlite（既定） | postgres
  dsn: postgresql://...
  side_storage: auto       # auto（既定） | file | database
```

| `side_storage` | 置き場 |
|---|---|
| `auto` | **PostgreSQL ならデータベース**、SQLite なら隣のファイル（従来どおり） |
| `file` | 台帳の隣のファイル（PostgreSQL でも。ホストごとに別々になる） |
| `database` | 台帳と同じデータベースの表。SQLite でも使える（台帳の `.db` ひとつに全部入る） |

データベースのときの表: SQLite は `side_docs` / `side_log`、PostgreSQL は `ledger_side_docs` / `ledger_side_log`
（**テナントごとに行で分かれる**）。起動時に自動で作られる。

- **schedule と serve を別ホストに分けられる。** 同じ PostgreSQL と `tenant` を指していれば、どちらのホストでも
  同じブリーフィング・状態・判断ログが見える。ホスト A の `aipmo schedule` が書いたものを、ホスト B の
  `aipmo serve` が出す。台帳の隣にファイルは作られない。
- **文書は丸ごと置き換え、ログは追記だけ。** 読む側は、書きかけの状態を見ない。ログは位置（カーソル）で
  続きだけを読める（レビュー件数の集計がこれを使う）。
- **ファイルでも直した点**: 判断ログへの同時の追記が、Windows で行を失っていた（4 スレッドで 40 行中 39 行）。
  OS の排他ロックを取ってから追記するようにした（複数プロセスでも確かめた）。
- 保存に失敗しても周は止めない（警告を出して続ける）。

## 既存のファイルを移す / Moving existing files

```bash
aipmo ledger migrate                 # SQLite → PostgreSQL。置き場が database なら、隣のファイルも一緒に
aipmo ledger side-import             # 隣のファイルだけをデータベースへ取り込む（--from-dir DIR で元の場所を指定）
aipmo ledger info                    # 台帳と、隣に置くものの置き場を表示
```

取り込みは、移行先に文書が**既にあれば上書きしません**（`--force` のときだけ上書き）。ログは移行先に 1 行でも
あれば**足しません**（二重に取り込まない。`--force` でも）。移行元のファイルは消しません。

## PostgreSQL から SQLite へ戻す / Moving back to SQLite

```bash
aipmo ledger migrate-to-sqlite                         # 移行先は設定の台帳ファイルの場所
aipmo ledger migrate-to-sqlite --to ./task-ledger.db   # 移行先を指定
aipmo ledger migrate-to-sqlite --side database         # 隣に置くものを SQLite の表へ（既定は隣のファイル）
aipmo ledger migrate-to-sqlite --force                 # 移行先に行があっても、同じ id を上書きする
```

設定の `task_engine.backend: postgres` の台帳（この `tenant` の行）が移行元です。

- **写したあと読み直して、移行元と一致するか確かめる。** タスクは 1 件ずつ中身を、完了実績は全件を比べる
  （PostgreSQL の `jsonb` は書式を整え直すので、書式の違いは無視して中身で比べる）。一致しなければ成功と
  言わない。
- **持ち主の刻印を守る。** 移行先の SQLite に `tenant` を刻む。別のテナントの SQLite ファイルへは書かない。
- **既存の行を壊さない。** 移行先にタスクがあれば止まる（`--force` で同じ id を上書き）。`--force` でやり直しても
  **完了実績は二重にならない**（すでにあるものは足さない）。移行先にだけある行は消さない。
- **隣に置くもの**（ブリーフィング・判断ログ・状態）がデータベースにあれば、移行先のファイル（`--side file`、既定）か
  SQLite の表（`--side database`）へ写す。移行先に既にある文書は上書きしない（`--force` のときだけ）。ログは
  移行先に 1 行でもあれば足さない。
- **移行元は消さない。** 使い始めるには `config.yaml` の `task_engine.backend` を `sqlite`（または削除）にし、
  確認してから PostgreSQL 側の行を片付ける。
- 往復（SQLite → PostgreSQL → SQLite）で元に戻ることを、実 PostgreSQL で確かめた。

## 限界 / Limits

- 判断ログは、データベースでもファイルでも**増え続けます**（自動では間引かない）。
- PostgreSQL → SQLite は、台帳が大きいと全件をメモリに載せて写します（数十万件を超える規模は未確認）。
- 常駐のスケジューラ自身の状態（`scheduler-state.json`、最後に走った時刻）は、そのホストのローカルファイルのまま
  （ホストごとのもの）。
- 実際の運用規模（長期間・多数のテナント）での負荷は未確認です。
