# 実サービス（Jira・Plane・OpenProject）につなぐ確認 / Verifying against real trackers

この環境（開発・CI）には実サービスの接続先も認証情報も無いので、実サービスに対する確認は**手元で行う**。
そのための道具を 3 つ用意した。**どれも、書き込みは明示したときだけ**。

## 1. 接続の診断（読み取りだけ）

```bash
aipmo --config config.yaml integrations            # 設定したアダプタ全部
aipmo --config config.yaml integrations plane      # 1 つだけ
```

次の順に確かめ、止まった所と原因を出す（終了コードは、1 つでも失敗すれば 1）。

| 段階 | 内容 | 失敗のときに疑うところ |
|---|---|---|
| `health` | アダプタの疎通確認 | URL・認証・プロジェクト／ワークスペースの指定 |
| `read` | 課題を 1 件だけ検索する | 読み取りの権限、JQL などの検索の書き方 |
| `people` | 担当候補の一覧（Plane・OpenProject） | メンバー一覧を読む権限（担当者の引き当てに必要） |

原因は `auth`（401/403）・`not_found`（404）・`timeout`・`network`・`rate_limit`・`other` に分類する。
エラー文からは、設定のトークン・API キーを伏せる。**書き込みのアクションは呼ばない。**

同じ診断は、Web 画面の「連携」の **接続を確かめる** ボタン（operator のみ、5 秒に 1 回まで。`POST /api/integrations/check`）でもできる。

## 2. 担当者の引き当ての確認（読み取りだけ）

```bash
aipmo --config config.yaml members --tracker plane
```

メンバーがトラッカーのどのユーザーに当たるかを見る。曖昧・該当なしは、そのまま書き戻すと**書かずに止まる**
（[TICKET-TRACKERS.md](TICKET-TRACKERS.md)）。

## 3. 実サービスに対する自動確認（手元でだけ動く）

[tests/test_live_trackers.py](../tests/test_live_trackers.py) は、環境変数が無ければ飛ばす。

```bash
# 読み取りだけ: 全アダプタの診断と、担当者の引き当て
AIPMO_LIVE_CONFIG=/path/to/config.yaml pytest tests/test_live_trackers.py

# 担当の書き戻しまで（実際の課題の担当を変える。専用のテスト課題で）
AIPMO_LIVE_CONFIG=/path/to/config.yaml AIPMO_LIVE_WRITE=1 AIPMO_LIVE_ISSUES="jira=PROJ-1;plane=<課題のid>;openproject=12" AIPMO_LIVE_ASSIGNEE="佐藤" pytest tests/test_live_trackers.py
```

書き戻しは「担当を書く → 読み直して付いたことを確かめる → 元の担当に戻す」。Jira は担当者の id、Plane はユーザー id で
元に戻せる。**OpenProject は読み戻した名前しか分からないので元に戻せない**（警告を出す）。未割り当てだった課題も戻せない。
そのため、必ず専用のテスト課題を使うこと。

## 確認の状況（正直に）

- 作り物の HTTP（Jira・Plane・OpenProject の応答の形）では、診断・書き戻し・エラーの分類・秘密の伏せ字・
  「読み取りで書き込みを出さない」を確かめた（`tests/test_probe.py`、`tests/test_plane.py` ほか）。
- **実サービスに対しては、まだ一度も動かしていない。** 実際の応答の細部（権限の範囲、検索の癖、Jira の
  `accountId` の引き当て、OpenProject の `lockVersion` の競合、Plane の API キーの種類）は、上の確認を実環境で
  動かして初めて分かる。動かして出た差は、このファイルの表に足していく。
