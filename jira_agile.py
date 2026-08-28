"""Jira Agile 連携 / Jira Agile integration.

スプリントとボードを扱う。課題そのものの操作は jira アダプタ側にある。

Sprints and boards. Operations on the issues themselves live in the jira
adapter.

別系統の API です / A separate API
-----------------------------------
アジャイル関連は `/rest/agile/1.0/` にあり、課題の API (`/rest/api/3/`) とは
別物です。認証は同じなので、HTTP の部分は jira アダプタを使い回します。

Agile lives under `/rest/agile/1.0/`, not `/rest/api/3/`. Authentication is the
same, so the HTTP layer is borrowed from the jira adapter.

ストーリーポイントの項目 ID は固定ではありません
-------------------------------------------------
`customfield_10016` のような ID は **Jira インスタンスごとに違います。**
他所のコードから持ってきた ID をそのまま書くと、値が取れずに全件が
「見積もり無し」になります。しかも**エラーにならない**ので気づきません。

ボード設定 (`/board/{id}/configuration`) が、そのボードで実際に使われている
項目 ID を返すので、そこから引き当てます。

A field id like `customfield_10016` **differs between Jira instances.** Copying
one from elsewhere yields no values at all, and every item silently reads as
unestimated — silently, because nothing errors. The board configuration
endpoint reports the field this board actually uses, so it is looked up.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .base import Adapter, AdapterError, action
from .jira import JiraAdapter

logger = logging.getLogger("aipmo.adapters.agile")

AGILE = "/rest/agile/1.0"

# スプリントへの課題移動は1回あたりの上限がある。
# Moving issues into a sprint is capped per request.
MOVE_BATCH = 50


class JiraAgileAdapter(Adapter):
    name = "agile"

    def __init__(self, board_id: int | None = None,
                 client: JiraAdapter | None = None, **config: Any) -> None:
        super().__init__(**config)
        self.board_id = board_id
        # HTTP と認証は jira アダプタと共通 / shares HTTP and auth with jira
        self.client = client or JiraAdapter(**config)
        self._estimation_field: str | None = None

    def health_check(self) -> bool:
        return self.client.health_check()

    # -- 内部 / internals --------------------------------------------------

    def _board(self, board_id: int | None) -> int:
        chosen = board_id or self.board_id
        if not chosen:
            raise AdapterError(
                "agile: board_id が必要です / board_id is required.\n"
                "  list_boards で調べられます / find it with list_boards."
            )
        return int(chosen)

    def _get(self, path: str, what: str) -> Any:
        status, data = self.client._request("GET", f"{AGILE}{path}")
        return self.client._require(status, data, what)

    def estimation_field(self, board_id: int | None = None) -> str | None:
        """そのボードが見積もりに使っている項目 ID。

        The field this board actually uses for estimation.
        """
        if self._estimation_field is not None:
            return self._estimation_field or None

        board = self._board(board_id)
        try:
            config = self._get(f"/board/{board}/configuration", "ボード設定 / board config")
        except AdapterError as exc:
            logger.warning("agile: ボード設定を取得できません / %s", exc)
            self._estimation_field = ""
            return None

        field = (((config.get("estimation") or {}).get("field") or {})
                 .get("fieldId"))
        self._estimation_field = field or ""
        return field or None

    # -- アクション / actions ----------------------------------------------

    @action()
    def list_boards(self, project: str | None = None) -> dict[str, Any]:
        query = f"?projectKeyOrId={project}" if project else ""
        data = self._get(f"/board{query}", "ボード一覧 / listing boards")

        items = [
            {"id": board.get("id"), "name": board.get("name"),
             "type": board.get("type")}
            for board in (data.get("values") or [])
        ]
        return {"items": items, "count": len(items)}

    @action()
    def active_sprint(self, board_id: int | None = None) -> dict[str, Any]:
        """進行中のスプリント / the sprint currently running."""
        board = self._board(board_id)
        try:
            data = self._get(f"/board/{board}/sprint?state=active",
                             "スプリント / sprints")
        except AdapterError as exc:
            # カンバンボードにスプリントはない。設定の事実であって障害ではない。
            # A kanban board has no sprints: a fact about the board, not a fault.
            if "400" in str(exc) or "does not support sprints" in str(exc):
                return {"active": False,
                        "reason": "このボードはスプリントを持ちません "
                                  "/ this board does not use sprints"}
            raise

        sprints = data.get("values") or []
        if not sprints:
            # スプリント間の期間。次が始まるまでは何も進行していない。
            # Between sprints: nothing is running until the next one starts.
            return {"active": False, "reason": "進行中のスプリントがありません "
                                               "/ no sprint is currently active"}

        # 同時に複数走っている場合は、最も早く終わるものを選ぶ。
        # 報告で見たいのは、いちばん近い締め切りだから。
        # Where several run at once, the one ending soonest is chosen: the
        # nearest deadline is what a status report is about.
        sprints.sort(key=lambda s: s.get("endDate") or "9999")
        sprint = sprints[0]

        return {
            "active": True,
            "id": sprint.get("id"),
            "name": sprint.get("name"),
            "goal": sprint.get("goal"),
            "start": sprint.get("startDate"),
            "end": sprint.get("endDate"),
            # 残日数はここで数える。テンプレートに計算の仕組みは無く、
            # 言語モデルに数えさせると間違える。数えれば決まる値は渡す側で決める。
            # Counted here: templates cannot do arithmetic and a language model
            # miscounts. A countable fact is settled before it is handed over.
            "days_remaining": _days_until(sprint.get("endDate")),
            "concurrent_sprints": len(sprints),
        }

    @action()
    def sprint_issues(self, sprint_id: int,
                      board_id: int | None = None) -> dict[str, Any]:
        """スプリントの課題と、そこから読み取れる状況。

        The issues in a sprint, and what they say about it.
        """
        field = self.estimation_field(board_id)
        wanted = ["summary", "status", "assignee", "duedate", "issuetype", "updated"]
        if field:
            wanted.append(field)

        data = self._get(
            f"/sprint/{sprint_id}/issue?fields={','.join(wanted)}&maxResults=200",
            "スプリントの課題 / sprint issues")

        items: list[dict[str, Any]] = []
        for issue in data.get("issues") or []:
            values = issue.get("fields") or {}
            category = ((values.get("status") or {}).get("statusCategory") or {})
            assignee = values.get("assignee") or {}

            items.append({
                "key": issue.get("key"),
                "summary": values.get("summary"),
                "status": (values.get("status") or {}).get("name"),
                "done": category.get("key") == "done",
                "in_progress": category.get("key") == "indeterminate",
                "assignee": assignee.get("displayName"),
                "points": values.get(field) if field else None,
                "updated": values.get("updated"),
                "type": (values.get("issuetype") or {}).get("name"),
            })

        done = [i for i in items if i["done"]]
        unestimated = [i for i in items if i["points"] is None]

        return {
            "items": items,
            "count": len(items),
            "done_count": len(done),
            "unassigned": [i["key"] for i in items
                           if not i["assignee"] and not i["done"]],
            # 見積もりの無い課題。項目 ID を引けなかった場合も全件がここに入るので、
            # 「全部見積もり無し」に見えたら設定を疑ってください。
            # Everything lands here when the field id could not be resolved, so
            # "nothing is estimated" is a reason to check the configuration.
            "unestimated": [i["key"] for i in unestimated],
            "estimation_field": field,
            "points_total": _sum_points(items),
            "points_done": _sum_points(done),
            # 進捗率もここで出す。割り算をモデルにさせない。
            # ポイントが使われていなければ件数で出す。分母が違うので
            # どちらで出したかも返す。
            #
            # The percentage is computed here too: the division is not the
            # model's to do. Where points are not in use it falls back to
            # counts, and says which basis it used, because the denominators
            # are not the same thing.
            "percent_done": _percent(items, done),
            "percent_basis": "points" if _sum_points(items) else "count",
        }

    @action()
    def backlog(self, board_id: int | None = None,
                limit: int = 50) -> dict[str, Any]:
        board = self._board(board_id)
        data = self._get(
            f"/board/{board}/backlog?maxResults={limit}&fields=summary,status,issuetype",
            "バックログ / backlog")

        items = [
            {"key": issue.get("key"),
             "summary": (issue.get("fields") or {}).get("summary"),
             "type": (((issue.get("fields") or {}).get("issuetype")) or {}).get("name")}
            for issue in (data.get("issues") or [])
        ]
        return {"items": items, "count": len(items)}

    @action(writes=True)
    def move_to_sprint(self, sprint_id: int,
                       issue_keys: list[str]) -> dict[str, Any]:
        """課題をスプリントに入れる / put issues into a sprint."""
        if not issue_keys:
            return {"moved": 0, "skipped": "no issues supplied"}

        moved = 0
        for start in range(0, len(issue_keys), MOVE_BATCH):
            batch = issue_keys[start:start + MOVE_BATCH]
            status, data = self.client._request(
                "POST", f"{AGILE}/sprint/{sprint_id}/issue", {"issues": batch})
            self.client._require(status, data, "スプリントへの移動 / moving to sprint")
            moved += len(batch)

        return {"moved": moved, "sprint_id": sprint_id}


def _sum_points(items: list[dict[str, Any]]) -> float | None:
    """見積もりの合計。1件も無ければ None。

    0.0 を返すと「全部終わっている」と読めてしまう。見積もりが無いことと、
    合計が 0 であることは違う。

    None rather than 0.0 when nothing is estimated: a zero would read as "all
    complete", and having no estimates is not the same as summing to nothing.
    """
    values = [i["points"] for i in items if isinstance(i["points"], (int, float))]
    return float(sum(values)) if values else None


def _percent(items: list[dict[str, Any]], done: list[dict[str, Any]]) -> int:
    """進捗率。割り算はここでする / progress as a percentage.

    _sum_points は未見積もりのとき None を返す（0 だと「全部終わった」に
    読めるため）。完了側が None なのは「見積もり済みの完了が無い」ことなので、
    0 として扱う。分母が無いときは件数に切り替える。

    _sum_points returns None when nothing is estimated, since a zero would read
    as "all complete". None on the completed side means no estimated work is
    done, which is zero. With no denominator it falls back to counts.
    """
    total_points = _sum_points(items)
    if total_points:
        return round((_sum_points(done) or 0.0) / total_points * 100)
    if items:
        return round(len(done) / len(items) * 100)
    return 0


def _days_until(stamp: str | None) -> int | None:
    """終了までの残日数 / whole days until the end.

    切り上げる。残り半日を「0日」と出すと、もう終わっているように読める。
    Rounded up: half a day reported as zero reads as though it is already over.
    """
    if not stamp:
        return None
    try:
        end = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None

    if end.tzinfo is None:
        # タイムゾーンの無い日付が返ることがある。UTC とみなす。
        # ここで例外にすると、日付の書式ひとつで報告全体が止まる。
        # Some dates come back without a zone; they are read as UTC. Raising
        # here would let one date format stop the entire report.
        end = end.replace(tzinfo=timezone.utc)

    delta = end - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return 0
    return int(delta.total_seconds() // 86400) + (
        1 if delta.total_seconds() % 86400 else 0)
