# 定時実行 / Scheduling

テンプレートに書いた `trigger: "schedule:..."` を実際に起動させます。
**これが動いていないと、定時起動のテンプレートは書けても何も起きません。**

Runs templates that declare `trigger: "schedule:..."`. Without the scheduler
running, such a template can be written but never fires.

---

## 使い方 / Usage

```bash
aipmo schedule --list     # 次回時刻を確認する / check the next times
aipmo schedule --once     # いま実行すべきものだけ実行 / run what is due
aipmo schedule            # 常駐する / stay resident
```

```
overdue_triage               2026-08-31 09:00 JST   0 9 * * MON-FRI
```

ルートの `docker-compose.yml` では、サービス名は `aipmo` で、中で `schedule`
が常駐します。Oracle 構成だけが画面 (`aipmo`) と定時実行 (`scheduler`) を分けています。

The root `docker-compose.yml` runs `schedule` inside the `aipmo` service.
Only the Oracle layout splits the interface (`aipmo`) from the scheduler
(`scheduler`).

```bash
# ローカル / local (root compose)
docker compose up -d aipmo
docker compose logs -f aipmo

# Oracle (deploy/oracle)
docker compose up -d scheduler
docker compose logs -f scheduler
```

---

## 書き方 / Writing a schedule

```yaml
trigger: "schedule:0 9 * * MON-FRI"     # 平日の朝9時
```

```yaml
trigger:
  type: schedule
  cron: "0 9 * * MON-FRI"
  timezone: Asia/Tokyo                   # 既定 / the default
```

| 書式 | 意味 |
|---|---|
| `0 9 * * *` | 毎日 9:00 |
| `0 9 * * MON-FRI` | 平日の 9:00 |
| `*/15 * * * *` | 15分ごと |
| `0 9-17/2 * * *` | 9:00 から 17:00 まで2時間ごと |
| `0 0 1 * *` | 毎月1日の 0:00 |

**日付と曜日を両方指定すると、どちらかに当たれば起動します。**
cron の歴史的な挙動で、直感には反しますが揃えてあります。
「毎月1日と、毎週月曜」を1行で書けるのはこの規則のためです。

**Specifying both a day-of-month and a weekday fires on either.** This is
cron's historical behaviour — counter-intuitive, but matched deliberately: it
is what lets "the 1st of the month, and every Monday" be a single line.

```yaml
trigger: "schedule:0 9 1 * MON"    # 毎月1日 かつ/または 毎週月曜
```

書き間違えても cron 式は動いてしまいます。`--list` で目視してください。
A mistyped expression still runs, just at the wrong time. Check it with
`--list`.

---

## 止まっていた間の扱い / Missed runs

機械が止まる。ノート PC が閉じられる。コンテナが再起動する。
そのとき、逃した実行をあとから流すか。

**流しません。**

毎朝9時の報告を、正午に5日分まとめて送っても意味がありません。
それは通知の洪水であって、報告ではない。逃したことは記録し、
次の予定から再開します。

**Missed runs are not replayed.** Five days of a 9am report delivered at noon
is a flood of notifications, not a report. The misses are recorded and the
schedule resumes at its next occurrence.

逃した回数は状態ファイルの `missed` に残ります。
The count is kept in the state file under `missed`.

> 遡って処理したい場合は、手で実行してください。
> To process a specific past occurrence, run it by hand:
> ```bash
> aipmo run templates/examples/overdue_triage.yaml
> ```

---

## 同時実行 / Overlap

前回がまだ終わっていないうちに次の時刻が来たとき、**重ねて走らせません。**
会議記録の処理が遅れているだけなのに、同じ課題が二重に起票されては困ります。

If the previous run has not finished when the next time arrives, it is not
started again: a transcript that is merely slow should not produce the same
Jira issues twice.

> **ただし、これはプロセス内での保護です。**
> 画面から手動で実行したものと、スケジューラの実行は別プロセスなので、
> 重なることがあります。Jira は冪等キーで二重起票を防ぎますが、
> **Slack への通知は二重に届きます。**
>
> This guard is per-process. A manual run from the interface and a scheduled
> run are separate processes and can overlap. Jira's idempotency key prevents
> duplicate issues, but **a Slack notification will arrive twice.**

---

## 同じ回に複数本が並ぶとき / Several jobs sharing one pass

同じ時刻に予定された複数のテンプレートは、**それぞれ別スレッドで並行に
走ります。** 1本が Slack 承認待ち（既定で最大300秒）のように長く塞がって
いても、他のテンプレートはそれを待たずに走ります。

Templates scheduled for the same moment **run concurrently, each in its own
thread.** If one is stuck behind a long wait — a Slack approval can take up to
300 seconds by default — the others do not wait for it.

これは1回の tick の内側だけの並行です。次の回（前述「同時実行」）は
これまで通り `job.running` で二重実行を防ぎます。

This concurrency is scoped to a single tick. The next pass (see "Overlap"
above) still prevents the *same* job from running twice via `job.running`,
unchanged.

Postgres・Qdrant・pgvector・Chroma・Milvus・Weaviate の各アダプタは、
1つの接続やクライアントを複数スレッドから同時に触らないよう、アダプタ
自身が直列化しています。複数のテンプレートが同じアダプタ・インスタンスを
共有していても壊れません。

The Postgres and vector-store adapters (Qdrant, pgvector, Chroma, Milvus,
Weaviate) each serialize access to their single connection or client
internally, so several templates sharing one adapter instance cannot corrupt
it.

---

## 失敗したとき / Failures

**1つのテンプレートの失敗で、スケジューラ全体は止まりません。**
止まると、他のテンプレートも全部動かなくなるためです。
失敗は記録し、次の予定で再試行します。

One template's failure does not stop the scheduler — that would take every
other template down with it. Failures are logged and retried at the next
occurrence.

連続失敗の回数は記録されるので、ログで気づけます。
ローカルは `docker compose logs aipmo`、Oracle は `docker compose logs scheduler`。

---

## 再起動と記録 / Restarts and state

最後に走った時刻を状態ファイルに残します。持たないと、**コンテナが
再起動するたびに直近の予定をもう一度走らせて**しまいます。

The last run time is persisted. Without it, every container restart would run
the most recent occurrence again.

```yaml
state_file: /app/state/scheduler-state.json
```

書き途中で落ちても壊れないよう、置き換えで保存します。
読めなくなっていた場合も起動は止めず、次の予定から再開します。

Written by replacement so a crash cannot corrupt it. An unreadable file does
not prevent startup; the schedule simply resumes from its next occurrence.

---

## 停止 / Stopping

SIGTERM を受けると、**実行中のテンプレートを最後まで走らせてから**止まります。
途中で切ると、課題は起票されたのに通知されていない、といった半端が残ります。

On SIGTERM the current run finishes before the process exits. Cutting it short
would leave half-states — issues filed but nobody told.

Docker では `stop_grace_period: 120s` を設定しています。
これより短いと、途中で強制終了されます。

---

## 時刻の扱い / Time

**夏時間で飛ばされた時刻は、その日は起動しません。** 存在しない時刻を
無理に実行しても、意図した時刻ではないためです。

A wall-clock time skipped by a spring-forward does not fire that day: forcing a
run at a time that did not exist is not the time that was asked for.

秋の巻き戻しで同じ時刻が二度来た場合は、実行の記録によって一度に抑えられます。

An autumn fallback repeats an hour; the record of what has already run
suppresses the second.

同じ時刻に複数のテンプレートが並ぶ場合、少しずらして実行します。
9:00 に5本あると、同じ瞬間に外部 API へ集中して絞られるためです。

Templates sharing a time are staggered: five at 9:00 would hit the same API in
the same instant and be throttled.

---

## うまくいかないとき / When nothing runs

| 症状 | 見るところ |
|---|---|
| 何も起動しない | `aipmo schedule --list` に出ているか。出ていなければ `trigger` の書式 |
| 一覧に出てこない | `trigger` が `schedule:` で始まっているか。`--list` は理由も表示します |
| 時刻がずれる | `timezone` の指定。既定は `Asia/Tokyo` |
| 再起動のたびに走る | `state_file` が書ける場所か。Docker では volume が要ります |
| 通知が二重に届く | 画面からの手動実行と重なっていないか（上記「同時実行」） |
