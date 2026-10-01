# PMO Core の自律的な判断 / Autonomous judgment

PMO Core が、自分で状況を診断し、対処を選び、運用者が許した範囲でだけ実行する仕組みです。
**LLM は使いません**。診断も選択も、数えて比べるだけの決定論的な処理で、同じ入力なら同じ結論になります。

## 何をするか

1. **診断**（毎周）：ブリーフィングと台帳から、次の問題を見つけます。
   `overload`（過負荷）・`project_risk`（プロジェクトの危険）・`agent_failure`（役割AIの失敗）・
   `collection_failing`（進捗収集の不調）・`capacity_shortage`（担い手不足）・`estimate_risk`（見積りの危うさ）。
2. **対処の選択**：診断ごとに候補の「はしご」があり、効用 = 基礎点 − 危険の代償 + 40 × (成功率 − 0.5) で並べます。
   成功率は過去の結果から学習します（Laplace 平滑化。実績が無ければ 50%）。
   対処は `notify`（人へ通知）・`recollect`（進捗を再収集）・`retry_agent`（役割AIの再実行）・
   `launch`（テンプレートの起動）・`followup`（対応タスクの作成）。
3. **自律度**：対処ごとに `off` / `propose` / `auto`。**既定は `notify` と `recollect` だけが `auto`**
   （通知と読み取りだけ）。ほかは `propose`：台帳に「判断」の記録（承認待ち）を作るだけで、人が承認するまで何も起きません。
4. **実行**：常駐の `aipmo schedule` だけが実行します。`aipmo judgment` や Web は表示・承認だけで、実行しません。
5. **効いたかの確認**：実行後、診断が消えれば成功を学習に加え、残れば「効かなかった」として次の段へ進みます。

## 設定

```yaml
pmo_core:
  judgment:                    # 書かなければ、この機能は無効
    autonomy:                  # 既定: notify=auto, recollect=auto, 他=propose
      launch: auto
    min_severity: 40
    limits:
      cooldown_hours: 24       # 同じ診断で次の対処を試すまでの待ち
      recheck_hours: 24        # 効いたかを見るまでの待ち
      renotify_hours: 72
      max_actions_per_cycle: 3
      max_attempts: 3          # 失敗した同じ対処を、1 つの診断の中で試す上限
      max_auto_per_day: 10
    circuit_breaker: {failures: 3, hours: 24}
    launch:                    # 起動してよいテンプレートの許可リスト（これ以外は決して起動しない）
      - id: replan
        template: wbs_replan
        addresses: [project_risk]
        params: {scope: project}
        max_per_day: 1
```

## 安全のしくみ

- **遮断器**：自動実行の失敗が `failures` 回（`hours` 時間内）続くと、`auto` は `propose` に落ちます（通知は除く）。
  `aipmo judgment reset` で戻せます。戻すと、それまでの失敗は数えずにやり直せます。時間がたてば自動でも戻ります。
- **失敗の再試行**：失敗した対処は、`max_attempts` 回まで同じ診断の中で再び試します（記録は「n 回目」として別に残ります）。尽きたら次の段へ。
- **一時停止**：`aipmo judgment pause` の間は、診断だけを行い、何も作らず、承認済みのものも実行しません。`resume` で再開。
- **1 日の上限**：`max_auto_per_day`、テンプレートごとの `max_per_day`。
- **判断の記録は仕事ではない**：順位に入らず、完了もできません。却下した判断は同じ診断の間は再提案されません。
- 外の世界への書き込みは、今までどおり人が確定した操作だけです。

## コマンド

```
aipmo judgment            # 診断・自律度・承認待ち・直近の判断（表示専用）
aipmo judgment pause      # 一時停止
aipmo judgment resume     # 再開
aipmo judgment reset      # 遮断器を戻す
aipmo generated           # 判断の記録（承認待ち）を見る・承認する
```

## 限界

- 実サービス（Jira・GitHub など）に対しては未検証です。偽のサーバーを使った実プロセスの通し確認と単体テストのみです。
- 対処は上の 5 種類だけです。診断の追加には `aipmo/judgment.py` の変更が要ります。
- 学習は成功・失敗の回数で、診断の種類ごとの粗いものです。
- 他言語のガイドには、まだ書いていません。

---

PMO Core diagnoses problems deterministically (no LLM), picks a remedy from a per-diagnosis ladder by learned
utility, and acts only inside the operator-granted envelope (default: notify and read-only re-collect are `auto`;
everything else is a proposal needing human approval). Only the resident `aipmo schedule` executes. A circuit
breaker demotes `auto` to `propose` after repeated failures; failed remedies are retried up to `max_attempts`;
`aipmo judgment pause|resume|reset` controls it. Not verified against real services.
