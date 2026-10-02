# デモの手順 / Demo guide

AI-PMO Platform を、**外部サービス無しで**触って確かめるためのサンプルデータと手順です。
`demo/` の設定とサンプルデータを台帳（DB）に入れると、「3 プロジェクト・22 件のタスクを、しばらく運用してきた」
状態ができます。期限切れ・長期ブロック・過負荷・担当未定・役割AIの成果・WBS のずれが仕込んであり、
PMO Core が**本物の判定で**警告や提案を出します。

A demo with sample data and no external service. One command loads a lived-in ledger (3 projects, 22
tasks, 20 past completions); every warning and proposal you then see is produced by the real rules.

> 以降のコマンドは、**リポジトリのルート**で実行します。`aipmo` は `python -m aipmo.cli` でも同じです。

---

## 1. 何が入るか

| 入るもの | 内容 |
|---|---|
| タスク 22 件 | web-renewal 10 件・mobile-app 6 件・infra 6 件。[demo/data/tasks.yaml](../demo/data/tasks.yaml) |
| 完了実績 20 件 | 過去に終わった仕事の記録（学習の材料）。[demo/data/outcomes.yaml](../demo/data/outcomes.yaml) |
| メンバー | 人 4 名（佐藤・鈴木・田中・高橋）と、役割AI 1 つ（開発AI）。[demo/config.yaml](../demo/config.yaml) |
| WBS | デモ製品のリリース計画。**わざと 2 か所ずれている**。[demo/wbs-demo.yaml](../demo/wbs-demo.yaml) |
| 定期タスク | 「週次レビュー」（設定に書いたので、承認なしで作られる） |

入れたあとの状況（実行した日が基準。日付は毎回ずれます）:

| 仕込んだ状況 | PMO Core が出すもの |
|---|---|
| 佐藤が 6 件を抱える（学習で補正した上限は 2 件） | 過負荷の警告、自律的な判断の**提案** |
| MOB-201・WEB-101・INF-330 が期限切れ | 警告。続けば「対応を決める」タスクの**提案**（承認待ち） |
| INF-301 が 6〜7 日ブロック | 長期ブロックの警告と、対応タスクの提案 |
| 担当未定が 4 件 | 担当の**提案**（スキルが合う人から。確定は人） |
| 開発AI に 3 件 | 役割AIの成果。1 件は認め済み・1 件は**差し戻し済み**（警告になる）・1 件は**レビュー待ち** |
| WBS の 1.2 は証拠が揃っているのに未完了、2.1 は完了なのに証拠が無い | WBS の更新漏れの**提案** 2 件 |
| 過去の完了実績 20 件 | 学習：bug ラベルへの加点、佐藤の上限を下げ・鈴木を上げる補正、1 ポイントあたりの日数 |

**どれも作り物の行を書き込んだものではありません。** 過去の時刻でタスクを入れ、時計を進めながら PMO Core の周を
回して（3 日前・2 日前・1 日前・いま）、警告が「続いた」状態を本物の判定で作っています
（[aipmo/demo.py](../aipmo/demo.py)）。

---

## 2. 入れる（SQLite・すぐ試せる）

```bash
pip install -e ".[web]"                          # まだなら。Web 画面（serve）に必要
aipmo --config demo/config.yaml demo load        # サンプルデータを入れる
```

出力の例:

```
デモのデータを入れました / demo data loaded  [sqlite:.../demo/task-ledger.db]
  タスク 31 件（未完了 23）・完了実績 20 件（学習 20 件）
  警告 15 件（全体のレベル: critical）
  承認待ちの提案 4 件（警告からの対応 2・WBS のずれ 2）・担当の提案 4 件
  自律的な判断 4 件（承認待ち 4）
  役割AIの成果のレビュー待ち 1 件・起票待ち 1 件
```

（タスク 31 件 = サンプル 22 件 + 週次レビュー + 提案 8。件数は日付で多少変わります。）

台帳は `demo/task-ledger.db`（設定ファイルの隣）に作られ、ブリーフィング・判断ログ・状態も隣のファイルに入ります。
**途中で何度でもやり直せます**（→ 第 7 章）。

> 安全のため、`demo` のコマンドは **`tenant: demo` の設定でだけ**動きます。別のテナントの台帳には、読み込みも
> 消去もしません。台帳にタスクが既にあるときは読み込まず、`--reset` を求めます。

---

## 3. 画面で見る

```bash
AIPMO_WEB_TOKEN=demo-operator AIPMO_VIEWER_TOKEN=demo-viewer aipmo --config demo/config.yaml serve
```

（Windows の PowerShell は `$env:AIPMO_WEB_TOKEN="demo-operator"; $env:AIPMO_VIEWER_TOKEN="demo-viewer"; aipmo --config demo/config.yaml serve`）

ブラウザで開く:

| 役割 | URL | できること |
|---|---|---|
| 操作する人（operator） | <http://127.0.0.1:8765/?token=demo-operator> | 見る＋承認・確定・レビュー・起票のボタン |
| 見るだけ（viewer） | <http://127.0.0.1:8765/?token=demo-viewer> | 見るだけ（ボタンは出ない） |

**PMO Core** 欄を上から見ていきます。

| 欄 | 見るところ |
|---|---|
| 警告（Alerts） | 全体が critical。期限切れ・長期ブロック・担当未定・役割AIの差し戻し |
| 自律的な判断 | 診断（過負荷・プロジェクトのリスク）と、人の承認を待つ判断。**承認するまで何も実行されない** |
| 役割AIの成果（人のレビュー待ち） | 開発AI の成果 1 件。**「認める」「差し戻す」**（差し戻しは理由が必須） |
| 起票待ち | 週次レビュー。**「起票」**で課題管理ツール（ここでは mock の Jira）に作る |
| 提案されたタスク | 「対応を決める…」。**承認／却下**。承認するまで順位にも入らない |
| 担当の提案 | **「担当を確定」**。スキルが合う人の理由つき |
| 優先順位 | 点数と内訳。タスクを開くと理由・役割AIの成果・レビューが見える |
| メンバーの負荷 | 佐藤が上限超過 |

ボタンを押すと、その場で一覧から消えて結果が反映されます。viewer のトークンで開くと、同じ画面でボタンが
出ないことも確かめられます。

---

## 4. CLI で順に触る（所要 15 分）

### 4-1. 全体像

```bash
aipmo --config demo/config.yaml pmo
```

全体のレベル・上位のタスク・警告・担当の提案・承認待ち・起票待ち・学習した補正が 1 画面に出ます。

### 4-2. 順位と、その理由

```bash
aipmo --config demo/config.yaml tasks --why --limit 5
```

点数の**内訳**が出ます（優先度・期限超過・学習による補正「過去実績で遅れやすいラベル」・見積り×ペース）。
点数は決定論的な計算で、LLM は使っていません。

### 4-3. 担当を決める

```bash
aipmo --config demo/config.yaml assign                     # 提案の一覧（理由つき）
aipmo --config demo/config.yaml assign INF-315 --apply     # INF-315 を提案どおり確定
```

担当未定の 4 件に、スキルが合う人が提案されています（INF-315・WEB-120 → 鈴木、MOB-210・WEB-125 → 高橋）。
確定は人の操作です。

### 4-4. 警告から出た「対応タスク」を決める

```bash
aipmo --config demo/config.yaml generated                  # 承認待ちの提案（id が出る）
aipmo --config demo/config.yaml generated approve <ID>     # 承認 → 普通のタスクになる
aipmo --config demo/config.yaml generated reject <ID>      # 却下 → 記録は残り、同じ警告では出し直さない
```

`<ID>` は `generated` の出力からコピーします（`PMO:fu:...` が警告からの対応、`PMO:wb:...` が WBS のずれ、
`PMO:jd:...` が自律的な判断）。**承認するまで順位にも担当提案にも入りません。**

### 4-5. 役割AIの成果を、人が確かめる

```bash
aipmo --config demo/config.yaml agents                     # 役割AIの状況と、レビューの件数
aipmo --config demo/config.yaml agents review              # レビュー待ちの成果（WEB-130）
aipmo --config demo/config.yaml agents review WEB-130 --accept --by あなた --note "問題なし"
aipmo --config demo/config.yaml agents review WEB-130 --reject --by あなた --note "確認項目が足りない"
```

- 差し戻しには**理由が必須**。差し戻すと警告（`agent_attention`）になり、人が引き取るか、もう一度任せます。
- **役割AI自身はレビューできません**（`--by 開発AI` は断られる）。
- デモの役割AIは、タスクの題名を埋め込んだ**固定の文**を返します（実際の AI は呼びません）。本物の役割AIは
  [docs/ROLES.md](ROLES.md)。

### 4-6. 自律的な判断（提案を承認 → 常駐が実行）

```bash
aipmo --config demo/config.yaml judgment                   # 診断・自律度・承認待ちの判断
aipmo --config demo/config.yaml generated approve <判断のID>   # 例: PMO:jd:overload:佐藤:followup:...
aipmo --config demo/config.yaml schedule --interval 3      # 常駐を動かす（数周したら Ctrl+C）
aipmo --config demo/config.yaml judgment                   # 承認した判断が executed になる
```

- 自律度の既定は保守的（通知と読み取りだけ自動）。それ以外は**提案どまり**で、人が承認するまで実行されません。
- 実行するのは**常駐（`schedule`）だけ**です。承認しただけでは動きません。
- `aipmo --config demo/config.yaml judgment pause` で一時停止、`resume` で再開、`reset` で遮断器を戻します。
  詳しくは [docs/JUDGMENT.md](JUDGMENT.md)。

### 4-7. 課題管理ツールへ起票する（承認つき）

```bash
aipmo --config demo/config.yaml file                       # 起票待ち（承認済み・未起票のタスク）
aipmo --config demo/config.yaml file --all --apply         # 起票する（ここでは mock の Jira。DEMO-1 ができる）
```

4-4 で承認した「対応タスク」も起票待ちに加わります。**mock なので、実際の Jira には何も作られません。**
本物の課題管理ツールに繋ぐ方法は [docs/TASK-GENERATION.md](TASK-GENERATION.md)。

### 4-8. WBS の更新漏れ

```bash
aipmo wbs check wbs-demo.yaml --root demo                  # error 1 件・warning 1 件（意図したずれ。終了コードは 1）
aipmo wbs notify wbs-demo.yaml --root demo --changed evidence/screens.md   # PR に出るコメントの本文（書かない）
```

- 2.1「ログイン機能」は完了なのに証拠（`evidence/login.py`）が無い、1.2「画面設計」は証拠が揃っているのに未完了。
- PMO Core はこれを「WBS を確かめる」提案にしています（4-4 の `PMO:wb:...`）。
- `evidence/login.py` を作って（`echo "def login(): ..." > demo/evidence/login.py`）`wbs check` を流し直すと、error が消えます。

### 4-9. 学習

`aipmo --config demo/config.yaml pmo` の末尾に、完了実績 20 件から学習した補正が出ます。

- 佐藤は遅れがちなので上限を下げ（約 ×0.7）、鈴木は期限どおりなので上げる（約 ×1.2）。
- 「bug」「dev」ラベルは遅れやすいので、順位に加点。
- 見積りと実日数から、1 ポイントあたりの日数（ペース）。

---

## 5. 常駐（schedule）を動かしてみる

```bash
aipmo --config demo/config.yaml schedule --interval 5
```

`demo/templates/tick.yaml`（何もしない定時テンプレート）が動くたびに PMO Core の周が回ります。周ごとに、
警告の継続・提案・自律的な判断・役割AIへの依頼が更新されます。別の端末で `serve` を開いておくと、画面が
更新されていくのが見えます。

---

## 6. PostgreSQL で試す

```bash
docker run -d --name aipmo-demo-pg -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=aipmo -p 5432:5432 postgres:16
export AIPMO_DEMO_DSN=postgresql://postgres:demo@localhost:5432/aipmo      # PowerShell: $env:AIPMO_DEMO_DSN="..."
aipmo --config demo/config.postgres.yaml demo load
aipmo --config demo/config.postgres.yaml ledger info
```

- 設定は [demo/config.postgres.yaml](../demo/config.postgres.yaml)（`config.yaml` と台帳の保存先だけが違う）。
- テーブルは**初回に自動で作られます**（`sql/schema.sql` を流す必要はありません。→ [docs/LEDGER-STORAGE.md](LEDGER-STORAGE.md)）。
- PostgreSQL では、ブリーフィング・判断ログ・状態も**同じデータベースに入る**ので、`schedule` と `serve` を別の
  ホストに分けても、同じ画面が出ます。`ledger info` の最後の行（`database tables of postgres`）で確かめられます。
- `tenant: demo` の行だけを使い、消すのもその行だけです（同じデータベースの別のテナントには触れません）。
- 後始末: `aipmo --config demo/config.postgres.yaml demo reset` のあと `docker rm -f aipmo-demo-pg`。

SQLite の台帳を PostgreSQL に移す練習をするなら、`aipmo --config demo/config.postgres.yaml ledger migrate
--from-sqlite demo/task-ledger.db`（逆向きは `ledger migrate-to-sqlite`）。

---

## 7. やり直す・片付ける

```bash
aipmo --config demo/config.yaml demo status          # いまの状況（周を 1 回回して数え直す）
aipmo --config demo/config.yaml demo load --reset    # デモのテナントの行を消して、入れ直す
aipmo --config demo/config.yaml demo reset           # 消すだけ
```

SQLite では台帳のファイルと、隣のブリーフィング・判断ログ・状態のファイルを消します（`demo/` の中だけ）。
手順を何度試しても、`--reset` で元の状態に戻ります。

---

## 8. サンプルデータを変える

- タスク: [demo/data/tasks.yaml](../demo/data/tasks.yaml)。日付は「今日からの日数」で書くので、いつ入れても
  期限切れ・期限間近の状況が再現されます（`due`・`age`・`started_days`・`blocked_days`。先頭のコメントを参照）。
- 完了実績: [demo/data/outcomes.yaml](../demo/data/outcomes.yaml)（5 件以上で学習が始まる）。
- メンバー・自律度・起票先: [demo/config.yaml](../demo/config.yaml)。
- WBS: [demo/wbs-demo.yaml](../demo/wbs-demo.yaml) と `demo/evidence/`。

変えたら `demo load --reset` で入れ直します。入力の誤り（key の重複・title が無い・`due` が整数でない）は、
読み込み前にはっきり止まります。

---

## 9. デモと本番の違い・限界

- **外部サービスにはつながない。** アダプタは mock（起票は画面上のダミー）、LLM は echo、役割AIの成果は固定文。
- **時計を進めて作った履歴です。** 警告が「続いた」状態は、3 日前から 1 日ごとに周を回して作っています
  （実際に 3 日待ったわけではありません）。判断ログや警告の時刻は、その模擬の時刻です。
- 自動で動かすのは `schedule` のときだけで、デモでも同じです（`pmo`・`tasks` などの表示コマンドは台帳の
  周を 1 回回しますが、通知や実行はしません）。
- 日付に依存する id（`PMO:fu:...:2026-09-29T0149` のようなもの）は読み込むたびに変わります。コマンドの `<ID>` は、
  `generated` の出力からコピーしてください。

## 10. うまくいかないとき

| 症状 | 見るところ |
|---|---|
| `デモは tenant: demo の設定でだけ` | 別の設定ファイルを指している。`--config demo/config.yaml` |
| `台帳にはすでに N 件のタスクがあります` | 入れ直すなら `demo load --reset` |
| `テンプレートが見つかりません` | `web.templates_dir` は設定ファイルのあるディレクトリから見る（`demo/templates`） |
| PostgreSQL に接続できない | `echo $AIPMO_DEMO_DSN`、`docker ps`。`aipmo --config demo/config.postgres.yaml ledger info` で確かめる |
| 画面が開かない | `serve` の出力のポート（既定 8765）。`pip install -e ".[web]"` を入れたか |
| Windows で `demo reset` が消せない | `serve` や `schedule` が台帳を開いたまま。止めてから |
