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

## 限界 / Limits

- 判断ログは、データベースでもファイルでも**増え続けます**（自動では間引かない）。
- 常駐のスケジューラ自身の状態（`scheduler-state.json`、最後に走った時刻）は、そのホストのローカルファイルのまま
  （ホストごとのもの）。
- 実際の運用規模（長期間・多数のテナント）での負荷は未確認です。
