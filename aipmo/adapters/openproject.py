"""OpenProject アダプタ / OpenProject adapter.

書き換えには `lockVersion`（楽観ロック）が要る。API 仕様上、更新のたびに
まず現在の Work Package を取得して版番号を読み、それを付けて PATCH
しなければならない。ここでは呼び出し側にその手順を意識させないよう、
update_issue の内部で自動的に行う。

Updates require `lockVersion` (optimistic concurrency): the API demands
fetching the current work package first to read its version number, then
sending that back with the PATCH. `update_issue` does this internally so
the caller never has to think about it.

冪等キーは説明文の先頭に `[aipmo:キー]` として埋め込み、次回はそれを
含む説明文を検索して重複を避ける。OpenProject の Work Package には
Jira のラベルに相当する汎用タグが無いための代替策。

The idempotency key is embedded as `[aipmo:KEY]` at the start of the
description, and searched for on the next run to avoid duplicates —
a workaround for OpenProject work packages having no generic tag/label
field like Jira's.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from base64 import b64encode
from typing import Any

from .base import Adapter, AdapterError, action

logger = logging.getLogger("aipmo.adapters.openproject")

RETRY_STATUSES = {429, 500, 502, 503, 504}


class OpenProjectAdapter(Adapter):
    name = "openproject"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        project_id: str | None = None,
        work_package_type_id: int | None = None,
        transport: Any = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.project_id = project_id
        self.work_package_type_id = work_package_type_id
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport

    # -- HTTP -----------------------------------------------------------

    def _auth_header(self) -> str:
        for value, name in ((self.base_url, "base_url"), (self.api_key, "api_key"),
                            (self.project_id, "project_id")):
            if not value:
                raise AdapterError(
                    f"openproject: {name} が設定されていません "
                    f"/ {name} is not configured"
                )
        pair = f"apikey:{self.api_key}".encode("utf-8")
        return "Basic " + b64encode(pair).decode("ascii")

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
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
                    logger.warning("openproject: %s, retrying in %.0fs", exc.code, wait)
                    time.sleep(min(wait, 60))
                    continue
                return exc.code, _decode(raw)
            except urllib.error.URLError as exc:
                if attempt == self.max_retries:
                    raise AdapterError(
                        f"openproject: 接続できません / cannot reach OpenProject: {exc}")
                time.sleep(2 ** attempt)

        raise AdapterError("openproject: 失敗しました / request failed")

    def _require(self, status: int, data: Any, what: str) -> Any:
        if status == 401:
            raise AdapterError(
                "openproject: 401 — API key を確認してください / check the API key."
            )
        if status == 403:
            raise AdapterError(
                "openproject: 403 — このユーザーに操作の権限がありません "
                "/ this account lacks permission for that operation."
            )
        if status == 409:
            raise AdapterError(
                "openproject: 409 — 版が古くなっています（誰かが先に更新しました）。"
                "再取得してやり直してください / stale lockVersion — someone else "
                "updated it first; re-fetch and retry."
            )
        if status >= 400:
            message = data.get("message") if isinstance(data, dict) else data
            raise AdapterError(f"openproject: {what} に失敗 ({status}): {message}")
        return data

    def health_check(self) -> bool:
        try:
            status, _ = self._request("GET", f"/api/v3/projects/{self.project_id}")
            return status < 400
        except Exception:
            return False

    # -- アクション / actions ---------------------------------------------

    @action()
    def search(self, filters: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Work Package を検索する / search work packages.

        `filters` は OpenProject の JSON フィルタ文字列。省略すると
        完了していない Work Package を全件返す。

        `filters` is OpenProject's JSON filter string. Omitting it returns
        every open work package.
        """
        query = filters or '[{"status":{"operator":"o","values":[]}}]'
        status, data = self._request(
            "GET", f"/api/v3/projects/{self.project_id}/work_packages"
                   f"?filters={query}&pageSize={min(limit, 100)}")
        data = self._require(status, data, "検索 / search")

        elements = (data.get("_embedded") or {}).get("elements") or []
        items = [_flatten(item) for item in elements]
        return {"items": items, "count": len(items)}

    @action()
    def find_overdue(self, as_of: str | None = None) -> dict[str, Any]:
        """期限を過ぎた未完了の Work Package / open work packages past their due date."""
        from datetime import date

        cutoff = as_of or date.today().isoformat()
        result = self.search(
            filters=f'[{{"status":{{"operator":"o","values":[]}}}},'
                    f'{{"dueDate":{{"operator":"<t-","values":["{cutoff}"]}}}}]')
        return result

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]],
                      work_package_type_id: int | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        """Work Package を作る / create work packages.

        同じ冪等キーの印を説明文に持つものが既にあれば、作り直さない。
        Nothing is recreated when a work package already carries the same
        idempotency marker in its description.
        """
        if not issues:
            return {"created": [], "count": 0, "skipped": "no issues supplied"}

        marker = f"[aipmo:{idempotency_key}]" if idempotency_key else None
        if marker:
            existing = self.search(
                filters=f'[{{"description":{{"operator":"~","values":["{marker}"]}}}}]',
                limit=100)
            if existing["count"]:
                logger.info("openproject: 作成済みのため省略 / already created for %s",
                            marker)
                return {
                    "created": [item["id"] for item in existing["items"]],
                    "count": existing["count"],
                    "skipped": "already created for this idempotency key",
                }

        type_id = work_package_type_id or self.work_package_type_id
        created: list[int] = []
        failed: list[dict[str, Any]] = []
        for issue in issues:
            description = issue.get("description") or ""
            if marker:
                description = f"{marker} {description}".strip()

            payload: dict[str, Any] = {
                "subject": (issue.get("summary") or issue.get("subject") or "")[:255],
                "description": {"format": "markdown", "raw": description},
            }
            if issue.get("due_date"):
                payload["dueDate"] = issue["due_date"]
            if type_id:
                payload["_links"] = {"type": {"href": f"/api/v3/types/{type_id}"}}

            status, data = self._request(
                "POST", f"/api/v3/projects/{self.project_id}/work_packages", payload)
            if status >= 400:
                failed.append({"issue": payload["subject"], "error": data})
                continue
            created.append(data.get("id"))

        result: dict[str, Any] = {"created": created, "count": len(created)}
        if failed:
            result["failed"] = failed
            result["failed_count"] = len(failed)
        return result

    @action(writes=True)
    def update_issue(self, work_package_id: int, subject: str | None = None,
                     description: str | None = None, due_date: str | None = None,
                     comment: str | None = None) -> dict[str, Any]:
        """Work Package を書き換える / change fields on an existing work package.

        渡された項目だけを送る。lockVersion は内部で取得して自動的に付ける。

        Only the given fields are sent; lockVersion is fetched and attached
        internally.
        """
        fields: dict[str, Any] = {}
        if subject is not None:
            fields["subject"] = subject[:255]
        if description is not None:
            fields["description"] = {"format": "markdown", "raw": description}
        if due_date is not None:
            fields["dueDate"] = due_date

        if fields:
            status, current = self._request(
                "GET", f"/api/v3/work_packages/{work_package_id}")
            current = self._require(status, current,
                                     f"{work_package_id} の取得 / fetching")
            fields["lockVersion"] = current.get("lockVersion")

            status, data = self._request(
                "PATCH", f"/api/v3/work_packages/{work_package_id}", fields)
            self._require(status, data, f"{work_package_id} の更新 / updating")

        if comment:
            self.add_comment(work_package_id, comment)

        changed = sorted(k for k in fields if k != "lockVersion")
        if not changed and not comment:
            return {"work_package_id": work_package_id, "changed": [],
                    "skipped": "nothing to change"}

        result: dict[str, Any] = {"work_package_id": work_package_id, "changed": changed}
        if comment:
            result["commented"] = True
        return result

    @action(writes=True)
    def add_comment(self, work_package_id: int, text: str) -> dict[str, Any]:
        status, data = self._request(
            "POST", f"/api/v3/work_packages/{work_package_id}/activities",
            {"comment": {"format": "markdown", "raw": text}})
        data = self._require(status, data, "コメントの追加 / adding a comment")
        return {"id": data.get("id"), "work_package_id": work_package_id}


def _flatten(item: dict[str, Any]) -> dict[str, Any]:
    links = item.get("_links") or {}
    status = links.get("status") or {}
    assignee = links.get("assignee") or {}
    description = item.get("description") or {}
    return {
        "id": item.get("id"),
        "subject": item.get("subject"),
        "status": status.get("title"),
        "assignee": assignee.get("title"),
        "due_date": item.get("dueDate"),
        "description": description.get("raw"),
        "lock_version": item.get("lockVersion"),
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
