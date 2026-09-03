"""Plane アダプタ / Plane adapter.

冪等キーは Plane が課題そのものに持つ `external_id` / `external_source`
に載せる。ラベルを作って探す（Jira・GitHub のやり方）よりこちらが自然
——Plane の連携用 API はもとよりこの2項目を「外部システムとの重複防止」
のために用意している。

The idempotency key rides on Plane's own `external_id` / `external_source`
fields on the issue, rather than a manufactured label (the Jira/GitHub
approach) — Plane's integration-facing API already provides these two
fields specifically to prevent duplicates from an external system.

締切は `target_date`（YYYY-MM-DD）。優先度・状態は Plane 側の
ワークスペース設定でラベルが変わるため、ここでは値をそのまま渡す
（正規化はしない）。

The due date is `target_date` (YYYY-MM-DD). Priority and state labels vary
per workspace configuration, so their values are passed through as given,
not normalized.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from .base import Adapter, AdapterError, action

logger = logging.getLogger("aipmo.adapters.plane")

RETRY_STATUSES = {429, 500, 502, 503, 504}
EXTERNAL_SOURCE = "aipmo"


class PlaneAdapter(Adapter):
    name = "plane"

    def __init__(
        self,
        api_key: str | None = None,
        workspace_slug: str | None = None,
        project_id: str | None = None,
        base_url: str = "https://api.plane.so",
        transport: Any = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.api_key = api_key
        self.workspace_slug = workspace_slug
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport

    # -- HTTP -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        for value, name in ((self.api_key, "api_key"),
                            (self.workspace_slug, "workspace_slug"),
                            (self.project_id, "project_id")):
            if not value:
                raise AdapterError(
                    f"plane: {name} が設定されていません / {name} is not configured"
                )
        return {
            "X-API-Key": str(self.api_key),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _issues_path(self, suffix: str = "") -> str:
        return (f"/api/v1/workspaces/{self.workspace_slug}"
                f"/projects/{self.project_id}/issues/{suffix}")

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = self._headers()
        body = json.dumps(payload).encode("utf-8") if payload is not None else None

        if self._transport is not None:
            status, _, raw = self._transport.request(method, url, headers, body,
                                                     self.timeout)
            return status, _decode(raw)

        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(url, data=body, headers=headers,
                                             method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, _decode(response.read())
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code in RETRY_STATUSES and attempt < self.max_retries:
                    wait = float(exc.headers.get("Retry-After") or 2 ** attempt)
                    logger.warning("plane: %s, retrying in %.0fs", exc.code, wait)
                    time.sleep(min(wait, 60))
                    continue
                return exc.code, _decode(raw)
            except urllib.error.URLError as exc:
                if attempt == self.max_retries:
                    raise AdapterError(f"plane: 接続できません / cannot reach Plane: {exc}")
                time.sleep(2 ** attempt)

        raise AdapterError("plane: 失敗しました / request failed")

    def _require(self, status: int, data: Any, what: str) -> Any:
        if status == 401:
            raise AdapterError(
                "plane: 401 — API key を確認してください / check the API key."
            )
        if status == 403:
            raise AdapterError(
                "plane: 403 — このワークスペースでの操作権限がありません "
                "/ this key lacks permission in this workspace."
            )
        if status == 404:
            raise AdapterError(
                f"plane: {what} — 404。workspace_slug / project_id / issue id "
                f"を確認してください / check workspace_slug, project_id, and the issue id."
            )
        if status >= 400:
            raise AdapterError(f"plane: {what} に失敗 ({status}): {data}")
        return data

    def health_check(self) -> bool:
        try:
            status, _ = self._request(
                "GET", f"/api/v1/workspaces/{self.workspace_slug}/projects/"
                       f"{self.project_id}/")
            return status < 400
        except Exception:
            return False

    # -- アクション / actions ---------------------------------------------

    @action()
    def search(self, limit: int = 50) -> dict[str, Any]:
        """未完了の課題を一覧する / list issues.

        Plane の REST API は自由記述のクエリ言語を持たないため、
        絞り込みはページ単位で取得したものを呼び出し側が行う想定。

        Plane's REST API has no free-form query language, so filtering
        beyond pagination is left to the caller.
        """
        status, data = self._request(
            "GET", self._issues_path() + f"?per_page={min(limit, 100)}")
        data = self._require(status, data, "検索 / search")

        raw_items = data.get("results") if isinstance(data, dict) else data
        items = [_flatten(issue) for issue in (raw_items or [])]
        return {"items": items, "count": len(items)}

    @action()
    def find_overdue(self, as_of: str | None = None) -> dict[str, Any]:
        """期限を過ぎた未完了の課題 / open issues past their target date."""
        from datetime import date

        cutoff = as_of or date.today().isoformat()
        result = self.search(limit=100)
        overdue = [
            item for item in result["items"]
            if item.get("target_date") and item["target_date"] < cutoff
            and not item.get("completed")
        ]
        return {"items": overdue, "count": len(overdue)}

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]],
                      idempotency_key: str | None = None) -> dict[str, Any]:
        """課題を作る / create issues.

        `external_id` に冪等キーを渡すと、Plane 側が重複を防ぐ
        （同じ external_source + external_id の課題は作り直されない）。

        Passing `external_id` lets Plane itself prevent duplicates — an
        issue with the same external_source + external_id is not recreated.
        """
        if not issues:
            return {"created": [], "count": 0, "skipped": "no issues supplied"}

        created: list[str] = []
        failed: list[dict[str, Any]] = []
        for index, issue in enumerate(issues):
            payload: dict[str, Any] = {
                "name": (issue.get("summary") or issue.get("name") or "")[:255],
                "description_html": f"<p>{issue.get('description') or ''}</p>",
            }
            if issue.get("due_date"):
                payload["target_date"] = issue["due_date"]
            if idempotency_key:
                payload["external_id"] = f"{idempotency_key}:{index}"
                payload["external_source"] = EXTERNAL_SOURCE

            status, data = self._request("POST", self._issues_path(), payload)
            if status >= 400:
                failed.append({"issue": payload["name"], "error": data})
                continue
            created.append(data.get("id"))

        result: dict[str, Any] = {"created": created, "count": len(created)}
        if failed:
            result["failed"] = failed
            result["failed_count"] = len(failed)
        return result

    @action(writes=True)
    def update_issue(self, issue_id: str, name: str | None = None,
                     description: str | None = None, state: str | None = None,
                     due_date: str | None = None,
                     comment: str | None = None) -> dict[str, Any]:
        """課題を書き換える / change fields on an existing issue.

        渡された項目だけを送る（Jira アダプタと同じ約束）。
        Only the given fields are sent (same promise as the Jira adapter).
        """
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name[:255]
        if description is not None:
            fields["description_html"] = f"<p>{description}</p>"
        if state is not None:
            fields["state"] = state
        if due_date is not None:
            fields["target_date"] = due_date

        if fields:
            status, data = self._request(
                "PATCH", self._issues_path(f"{issue_id}/"), fields)
            self._require(status, data, f"{issue_id} の更新 / updating")

        if comment:
            self.add_comment(issue_id, comment)

        if not fields and not comment:
            return {"issue_id": issue_id, "changed": [], "skipped": "nothing to change"}

        result: dict[str, Any] = {"issue_id": issue_id, "changed": sorted(fields)}
        if comment:
            result["commented"] = True
        return result

    @action(writes=True)
    def add_comment(self, issue_id: str, text: str) -> dict[str, Any]:
        status, data = self._request(
            "POST", self._issues_path(f"{issue_id}/comments/"),
            {"comment_html": f"<p>{text}</p>"})
        data = self._require(status, data, "コメントの追加 / adding a comment")
        return {"id": data.get("id"), "issue_id": issue_id}


def _flatten(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": issue.get("id"),
        "name": issue.get("name"),
        "state": issue.get("state"),
        "target_date": issue.get("target_date"),
        "completed": bool(issue.get("completed_at")),
        "priority": issue.get("priority"),
    }


def _decode(payload: bytes | str | None) -> Any:
    if payload is None:
        return {}
    text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:500]}
