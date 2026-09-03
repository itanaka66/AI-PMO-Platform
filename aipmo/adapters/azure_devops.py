"""Azure DevOps アダプタ / Azure DevOps adapter.

Work Item はプロセステンプレート（Agile / Scrum / CMMI / カスタム）ごとに
フィールド名が違う。締切日は既定で Agile の
`Microsoft.VSTS.Scheduling.DueDate` を使うが、他のテンプレートでは
存在しないことがあるため、`due_date_field` で上書きできるようにしてある。

Field names differ by process template (Agile / Scrum / CMMI / custom).
The default due-date field is Agile's
`Microsoft.VSTS.Scheduling.DueDate`; other templates may not have it, so
`due_date_field` overrides it.

冪等キーはタグ（System.Tags、カンマ区切り）に載せる。Work Item には
Jira のラベル検索に相当する `System.Tags CONTAINS` が WIQL にある。

The idempotency key rides on tags (System.Tags, comma-separated) — WIQL's
`System.Tags CONTAINS` plays the role Jira's label search does.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from typing import Any

from .base import Adapter, AdapterError, action

logger = logging.getLogger("aipmo.adapters.azure_devops")

RETRY_STATUSES = {429, 500, 502, 503, 504}
IDEMPOTENCY_PREFIX = "aipmo-"
API_VERSION = "7.1"


class AzureDevOpsAdapter(Adapter):
    name = "azure_devops"

    def __init__(
        self,
        organization: str | None = None,
        project: str | None = None,
        pat: str | None = None,
        work_item_type: str = "Task",
        due_date_field: str = "Microsoft.VSTS.Scheduling.DueDate",
        transport: Any = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.organization = organization
        self.project = project
        self.pat = pat
        self.work_item_type = work_item_type
        self.due_date_field = due_date_field
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport

    # -- HTTP -----------------------------------------------------------

    def _auth_header(self) -> str:
        for value, name in ((self.organization, "organization"),
                            (self.project, "project"), (self.pat, "pat")):
            if not value:
                raise AdapterError(
                    f"azure_devops: {name} が設定されていません "
                    f"/ {name} is not configured"
                )
        pair = f":{self.pat}".encode("utf-8")
        return "Basic " + b64encode(pair).decode("ascii")

    def _base(self) -> str:
        return f"https://dev.azure.com/{self.organization}"

    def _request(self, method: str, path: str, payload: Any = None,
                 content_type: str = "application/json") -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self._base()}{path}"
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": content_type,
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
                    logger.warning("azure_devops: %s, retrying in %.0fs",
                                   exc.code, wait)
                    time.sleep(min(wait, 60))
                    continue
                return exc.code, _decode(raw)
            except urllib.error.URLError as exc:
                if attempt == self.max_retries:
                    raise AdapterError(
                        f"azure_devops: 接続できません / cannot reach Azure DevOps: {exc}")
                time.sleep(2 ** attempt)

        raise AdapterError("azure_devops: 失敗しました / request failed")

    def _require(self, status: int, data: Any, what: str) -> Any:
        if status in (401, 203):
            raise AdapterError(
                "azure_devops: 401 — PAT を確認してください / check the PAT. "
                "期限切れや scope 不足のことがあります "
                "/ it may have expired or lack the needed scope."
            )
        if status == 403:
            raise AdapterError(
                "azure_devops: 403 — この操作の権限がありません "
                "/ this account lacks permission for that operation."
            )
        if status == 409:
            raise AdapterError(
                "azure_devops: 409 — 競合が発生しました（誰かが先に更新した"
                "可能性があります）。取得し直してやり直してください "
                "/ conflict — someone else may have updated it first; "
                "re-fetch and retry."
            )
        if status >= 400:
            message = data.get("message") if isinstance(data, dict) else data
            raise AdapterError(f"azure_devops: {what} に失敗 ({status}): {message}")
        return data

    def health_check(self) -> bool:
        try:
            status, _ = self._request(
                "GET", f"/{self.project}/_apis/wit/workitemtypes?api-version={API_VERSION}")
            return status < 400
        except Exception:
            return False

    # -- アクション / actions ---------------------------------------------

    @action()
    def search(self, wiql: str | None = None, limit: int = 50) -> dict[str, Any]:
        """WIQL で検索する / search with WIQL.

        `wiql` を省略すると `project` 内の未完了 Work Item を全件返す。
        WIQL は ID の一覧しか返さないため、詳細取得に2回目の呼び出しを行う。

        Omitting `wiql` returns every open work item in `project`. WIQL
        returns only a list of IDs, so a second call fetches the details.
        """
        query = wiql or (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.project}' "
            f"AND [System.State] <> 'Closed' AND [System.State] <> 'Done' "
            f"ORDER BY [System.ChangedDate] DESC"
        )
        status, data = self._request(
            "POST", f"/{self.project}/_apis/wit/wiql?api-version={API_VERSION}",
            {"query": query})
        data = self._require(status, data, "検索 / search")

        ids = [item["id"] for item in (data.get("workItems") or [])][:limit]
        if not ids:
            return {"items": [], "count": 0}

        fields = ("System.Title", "System.State", "System.AssignedTo",
                  "System.Tags", self.due_date_field)
        id_list = ",".join(str(i) for i in ids)
        status, detail = self._request(
            "GET",
            f"/_apis/wit/workitems?ids={id_list}&fields={','.join(fields)}"
            f"&api-version={API_VERSION}")
        detail = self._require(status, detail, "詳細の取得 / fetching details")

        items = [_flatten(item, self.due_date_field) for item in detail.get("value") or []]
        return {"items": items, "count": len(items)}

    @action()
    def find_overdue(self, as_of: str | None = None) -> dict[str, Any]:
        """期限を過ぎた未完了の Work Item / open work items past their due date."""
        cutoff = as_of or "@today"
        wiql = (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.project}' "
            f"AND [{self.due_date_field}] < {cutoff} "
            f"AND [System.State] <> 'Closed' AND [System.State] <> 'Done' "
            f"ORDER BY [{self.due_date_field}] ASC"
        )
        return self.search(wiql=wiql)

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]],
                      work_item_type: str | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        """Work Item を作る / create work items.

        同じ冪等キーのタグで既に作られていれば、作り直さない。
        Nothing is recreated for a key that already produced work items.
        """
        if not issues:
            return {"created": [], "count": 0, "skipped": "no issues supplied"}

        item_type = work_item_type or self.work_item_type
        tag = f"{IDEMPOTENCY_PREFIX}{idempotency_key}" if idempotency_key else None

        if tag:
            existing = self.search(
                wiql=f"SELECT [System.Id] FROM WorkItems "
                     f"WHERE [System.TeamProject] = '{self.project}' "
                     f"AND [System.Tags] CONTAINS '{tag}'")
            if existing["count"]:
                logger.info("azure_devops: 作成済みのため省略 / already created for %s", tag)
                return {
                    "created": [item["id"] for item in existing["items"]],
                    "count": existing["count"],
                    "skipped": "already created for this idempotency key",
                }

        created: list[int] = []
        failed: list[dict[str, Any]] = []
        for issue in issues:
            patch = [
                {"op": "add", "path": "/fields/System.Title",
                 "value": (issue.get("summary") or issue.get("title") or "")[:256]},
            ]
            if issue.get("description"):
                patch.append({"op": "add", "path": "/fields/System.Description",
                              "value": issue["description"]})
            if issue.get("assignee"):
                patch.append({"op": "add", "path": "/fields/System.AssignedTo",
                              "value": issue["assignee"]})
            if issue.get("due_date"):
                patch.append({"op": "add", "path": f"/fields/{self.due_date_field}",
                              "value": issue["due_date"]})
            tags = list(issue.get("tags") or [])
            if tag:
                tags.append(tag)
            if tags:
                patch.append({"op": "add", "path": "/fields/System.Tags",
                              "value": "; ".join(tags)})

            status, data = self._request(
                "POST",
                f"/{self.project}/_apis/wit/workitems/${item_type}"
                f"?api-version={API_VERSION}",
                patch, content_type="application/json-patch+json")
            if status >= 400:
                failed.append({"issue": patch[0]["value"], "error": data})
                continue
            created.append(data.get("id"))

        result: dict[str, Any] = {"created": created, "count": len(created)}
        if failed:
            result["failed"] = failed
            result["failed_count"] = len(failed)
        return result

    @action(writes=True)
    def update_issue(self, work_item_id: int, title: str | None = None,
                     description: str | None = None, state: str | None = None,
                     assignee: str | None = None, due_date: str | None = None,
                     comment: str | None = None) -> dict[str, Any]:
        """Work Item を書き換える / change fields on an existing work item.

        渡された項目だけを送る（Jira アダプタと同じ約束）。
        Only the given fields are sent (same promise as the Jira adapter).
        """
        patch = []
        if title is not None:
            patch.append({"op": "add", "path": "/fields/System.Title",
                          "value": title[:256]})
        if description is not None:
            patch.append({"op": "add", "path": "/fields/System.Description",
                          "value": description})
        if state is not None:
            patch.append({"op": "add", "path": "/fields/System.State", "value": state})
        if assignee is not None:
            patch.append({"op": "add", "path": "/fields/System.AssignedTo",
                          "value": assignee})
        if due_date is not None:
            patch.append({"op": "add", "path": f"/fields/{self.due_date_field}",
                          "value": due_date})
        if comment is not None:
            patch.append({"op": "add", "path": "/fields/System.History",
                          "value": comment})

        if not patch:
            return {"work_item_id": work_item_id, "changed": [],
                    "skipped": "nothing to change"}

        status, data = self._request(
            "PATCH",
            f"/_apis/wit/workitems/{work_item_id}?api-version={API_VERSION}",
            patch, content_type="application/json-patch+json")
        self._require(status, data, f"{work_item_id} の更新 / updating")

        changed = sorted(p["path"].rsplit("/", 1)[-1] for p in patch
                          if p["path"] != "/fields/System.History")
        result = {"work_item_id": work_item_id, "changed": changed}
        if comment is not None:
            result["commented"] = True
        return result

    @action(writes=True)
    def add_comment(self, work_item_id: int, text: str) -> dict[str, Any]:
        status, data = self._request(
            "POST",
            f"/_apis/wit/workitems/{work_item_id}/comments"
            f"?api-version={API_VERSION}-preview.4",
            {"text": text})
        data = self._require(status, data, "コメントの追加 / adding a comment")
        return {"id": data.get("id"), "work_item_id": work_item_id}


def _flatten(item: dict[str, Any], due_date_field: str) -> dict[str, Any]:
    fields = item.get("fields") or {}
    assigned_to = fields.get("System.AssignedTo") or {}
    tags_raw = fields.get("System.Tags") or ""
    return {
        "id": item.get("id"),
        "title": fields.get("System.Title"),
        "status": fields.get("System.State"),
        "assignee": assigned_to.get("displayName") if isinstance(assigned_to, dict)
                    else assigned_to,
        "due_date": fields.get(due_date_field),
        "tags": [t.strip() for t in tags_raw.split(";") if t.strip()],
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
