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

## Web の接続プール / The web connection pool

画面（`aipmo serve`）は、リクエストのたびに台帳を開いて閉じていました。PostgreSQL では、そのたびに接続を
張り直し、表の確認（DDL）を走らせ、同時のリクエストの数だけ接続が増えます（40 並列で +40 接続。
`max_connections` を使い切ると、画面だけでなく常駐の `aipmo schedule` まで接続できなくなります）。

```yaml
web:
  pool:
    size: 8                # 台帳へ張る接続の上限（既定 8。0 でプールを使わず、従来どおり毎回開閉）
    timeout_seconds: 10    # 全部使用中のとき、順番を待つ秒数（既定 10）。過ぎたら 503
    idle_seconds: 300      # 使われないまま持ち続ける秒数（既定 300）
```

- **上限を超えて接続しない。** 全部使用中なら `timeout_seconds` まで待ち、それでも空かなければ **503**
  （`Retry-After: 2` つき）で断る。待ち続けて固まることはない。
- **借りるたびに最新を読み直す**（使い回しても古い台帳を見せない。別プロセスの書き込みも見える）。ブリーフィングや
  判断ログだけを読むときは、台帳の読み直しを省く。
- **借りた間は自分だけのもの**（同時に 2 つのリクエストが同じ台帳を使わない）。返すとき、学習の補正など
  書き換えられる状態を空に戻す。
- **壊れたものは捨てる。** リクエストが例外で終わり、台帳を読み直せなければ（接続が切れた）捨てて、次は新しく
  作る。PostgreSQL の再起動やネットワーク断のあとも、画面は自然に復帰する。
- **使われないものは閉じる**（`idle_seconds`）。サーバーの停止時にも閉じる。
- 状況は `GET /api/health` の `ledger_pool`（`size` / `in_use` / `idle` / `waiting` / `created` / `reused` /
  `timeouts` / `discarded`）で見える。
- 実測（実 PostgreSQL 16、40 並列 × 10 リクエスト）: 台帳への接続の最大が **+40 → +8**、1 秒あたりの処理数は
  21.6 → 23.8、中央値 1.9 秒 → 1.6 秒。速さの改善は小さい（1 リクエストの処理が CPU 中心のため）。効くのは
  **接続数に上限がつくこと**。

## 限界 / Limits

- 判断ログは、データベースでもファイルでも**増え続けます**（自動では間引かない）。
- PostgreSQL → SQLite は、台帳が大きいと全件をメモリに載せて写します（数十万件を超える規模は未確認）。
- 画面の PostgreSQL アダプタ（WBS の変更提案・Digital Twin の読み取りに使う `adapters.postgres`）は、
  従来どおり 1 本の接続を使い回す（同時のリクエストは順番に処理される）。プールの対象は台帳だけ。
- 常駐のスケジューラ自身の状態（`scheduler-state.json`、最後に走った時刻）は、そのホストのローカルファイルのまま
  （ホストごとのもの）。
- 実際の運用規模（長期間・多数のテナント）での負荷は未確認です。

---

### 他言語の要約 / Summary in other languages

**中文**：说明台账旁边保存的文件（简报 `pmo-briefing.json`、跨周期状态 `pmo-core-state.json` 等）以及它们可以放在文件旁边或迁移到数据库中的方式。

**한국어**：원장 옆에 남는 파일들(브리핑 `pmo-briefing.json`, 주기를 넘기는 상태 `pmo-core-state.json` 등)과 이를 파일로 두거나 DB로 옮기는 방법을 설명합니다.

**Español**：Describe los archivos que viven junto al libro mayor (el briefing `pmo-briefing.json`, el estado entre ciclos `pmo-core-state.json`, etc.) y cómo pueden mantenerse como archivos o migrarse a una base de datos.

**Français**：Décrit les fichiers qui vivent à côté du registre (le briefing `pmo-briefing.json`, l'état inter-cycles `pmo-core-state.json`, etc.) et comment les garder en fichiers ou les migrer vers une base de données.

**Deutsch**：Beschreibt die Dateien, die neben dem Ledger liegen (das Briefing `pmo-briefing.json`, zyklenübergreifender Zustand `pmo-core-state.json` usw.) und wie sie als Dateien bleiben oder in eine Datenbank migriert werden können.

**Português**：Descreve os arquivos que ficam ao lado do livro-razão (o briefing `pmo-briefing.json`, o estado entre ciclos `pmo-core-state.json`, etc.) e como eles podem continuar como arquivos ou ser migrados para um banco de dados.
