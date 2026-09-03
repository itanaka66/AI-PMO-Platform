"""GitHub Projects アダプタ / GitHub Projects adapter.

「GitHub Projects」のボード自体（Projects v2 のカスタムフィールド・
ステータス列）は操作しない。ボードのアイテムは通常リポジトリの Issue
そのものなので、ここでは Issues REST API を Jira アダプタと同じ形
（search / create_issues / update_issue / add_comment）で操作する。
ボード固有のフィールド（優先度列など）を書き換えるには、GraphQL で
ボードごとのフィールド ID を事前に調べる必要があり、それは
リポジトリ横断で汎用に書けないため、意図的にここでは行わない。

This does not manipulate the Projects v2 board itself (its custom fields,
status columns). A board's items are ordinary repository issues, so this
operates on the Issues REST API instead, in the same shape as the Jira
adapter (search / create_issues / update_issue / add_comment). Writing a
board-specific field (e.g. a priority column) needs that board's own
GraphQL field IDs looked up ahead of time, which cannot be written
generically across repositories — so it is deliberately left out.

Issue に締切日を持たせる標準の項目が無いため、find_overdue は無い。
Projects v2 の日付カスタムフィールドはボードごとに ID が違い、汎用には
書けない。

There is no `find_overdue`: Issues have no built-in due-date field.
Projects v2 date custom fields exist, but their IDs differ per board and
cannot be addressed generically.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Adapter, AdapterError, action

logger = logging.getLogger("aipmo.adapters.github_projects")

RETRY_STATUSES = {429, 500, 502, 503, 504}
IDEMPOTENCY_PREFIX = "aipmo-"


class GitHubProjectsAdapter(Adapter):
    name = "github_projects"

    def __init__(
        self,
        token: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        base_url: str = "https://api.github.com",
        transport: Any = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport

    # -- HTTP -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        for value, name in ((self.token, "token"), (self.owner, "owner"),
                            (self.repo, "repo")):
            if not value:
                raise AdapterError(
                    f"github_projects: {name} が設定されていません "
                    f"/ {name} is not configured"
                )
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

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
                    logger.warning("github_projects: %s, retrying in %.0fs",
                                   exc.code, wait)
                    time.sleep(min(wait, 60))
                    continue
                return exc.code, _decode(raw)
            except urllib.error.URLError as exc:
                if attempt == self.max_retries:
                    raise AdapterError(
                        f"github_projects: 接続できません / cannot reach GitHub: {exc}")
                time.sleep(2 ** attempt)

        raise AdapterError("github_projects: 失敗しました / request failed")

    def _require(self, status: int, data: Any, what: str) -> Any:
        if status == 401:
            raise AdapterError(
                "github_projects: 401 — token を確認してください "
                "/ check the token. 期限切れや scope 不足のことがあります "
                "/ it may have expired or lack the needed scope."
            )
        if status == 403:
            raise AdapterError(
                "github_projects: 403 — 権限が無いか、レート制限です "
                "/ forbidden, or rate-limited."
            )
        if status == 404:
            raise AdapterError(
                f"github_projects: {what} — 404。owner/repo か Issue 番号を"
                f"確認してください / check owner/repo or the issue number."
            )
        if status >= 400:
            message = data.get("message") if isinstance(data, dict) else data
            raise AdapterError(f"github_projects: {what} に失敗 ({status}): {message}")
        return data

    def health_check(self) -> bool:
        try:
            status, _ = self._request("GET", f"/repos/{self.owner}/{self.repo}")
            return status < 400
        except Exception:
            return False

    # -- アクション / actions ---------------------------------------------

    @action()
    def search(self, query: str = "", limit: int = 50) -> dict[str, Any]:
        """Issue を検索する / search issues.

        `query` は GitHub の検索クオリファイア（`is:open label:bug` など）。
        リポジトリの指定は自動で付く。

        `query` is GitHub search qualifiers (e.g. `is:open label:bug`); the
        repository qualifier is added automatically.
        """
        self._headers()  # 設定確認 / validate config early
        q = f"repo:{self.owner}/{self.repo} is:issue {query}".strip()
        status, data = self._request(
            "GET", f"/search/issues?q={urllib.parse.quote(q)}"
                   f"&per_page={min(limit, 100)}")
        data = self._require(status, data, "検索 / search")

        items = [_flatten(issue) for issue in data.get("items") or []]
        return {"items": items, "count": len(items),
                "total_count": data.get("total_count", len(items))}

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]],
                      idempotency_key: str | None = None) -> dict[str, Any]:
        """Issue を作る / create issues.

        同じ冪等キーのラベルで既に作られていれば、作り直さない。
        Nothing is recreated for a key that already produced issues.
        """
        if not issues:
            return {"created": [], "count": 0, "skipped": "no issues supplied"}

        label = f"{IDEMPOTENCY_PREFIX}{idempotency_key}" if idempotency_key else None
        if label:
            existing = self.search(query=f'label:"{label}"', limit=100)
            if existing["count"]:
                logger.info("github_projects: 作成済みのため省略 / already created for %s",
                            label)
                return {
                    "created": [item["number"] for item in existing["items"]],
                    "count": existing["count"],
                    "skipped": "already created for this idempotency key",
                }

        created: list[int] = []
        failed: list[dict[str, Any]] = []
        for issue in issues:
            labels = list(issue.get("labels") or [])
            if label:
                labels.append(label)
            payload: dict[str, Any] = {
                "title": (issue.get("summary") or issue.get("title") or "")[:256],
                "body": issue.get("description") or "",
            }
            if labels:
                payload["labels"] = labels
            if issue.get("assignee"):
                payload["assignees"] = [issue["assignee"]]

            status, data = self._request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues", payload)
            if status >= 400:
                failed.append({"issue": payload["title"], "error": data})
                continue
            created.append(data.get("number"))

        result: dict[str, Any] = {"created": created, "count": len(created)}
        if failed:
            result["failed"] = failed
            result["failed_count"] = len(failed)
        return result

    @action(writes=True)
    def update_issue(self, issue_number: int, title: str | None = None,
                     description: str | None = None, state: str | None = None,
                     assignee: str | None = None,
                     add_labels: list[str] | None = None,
                     comment: str | None = None) -> dict[str, Any]:
        """Issue を書き換える / change fields on an existing issue.

        渡された項目だけを送る。指定しなかった項目には触れない
        （Jira アダプタの update_issue と同じ約束）。

        Only the given fields are sent; anything omitted is left alone
        (the same promise as the Jira adapter's update_issue).
        """
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title[:256]
        if description is not None:
            fields["body"] = description
        if state is not None:
            fields["state"] = state
        if assignee is not None:
            fields["assignees"] = [assignee]

        changed = sorted(fields)
        if fields:
            status, data = self._request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                fields)
            self._require(status, data, f"#{issue_number} の更新 / updating")

        if add_labels:
            status, data = self._request(
                "POST",
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels",
                {"labels": add_labels})
            self._require(status, data, f"#{issue_number} のラベル追加 / adding labels")

        if comment:
            self.add_comment(issue_number, comment)

        if not fields and not add_labels and not comment:
            return {"issue_number": issue_number, "changed": [],
                    "skipped": "nothing to change"}

        result: dict[str, Any] = {"issue_number": issue_number, "changed": changed}
        if add_labels:
            result["labels_added"] = add_labels
        if comment:
            result["commented"] = True
        return result

    @action(writes=True)
    def add_comment(self, issue_number: int, text: str) -> dict[str, Any]:
        status, data = self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
            {"body": text})
        data = self._require(status, data, "コメントの追加 / adding a comment")
        return {"id": data.get("id"), "issue_number": issue_number}


def _flatten(issue: dict[str, Any]) -> dict[str, Any]:
    assignee = issue.get("assignee") or {}
    labels = [label.get("name") for label in (issue.get("labels") or [])
              if isinstance(label, dict)]
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "status": issue.get("state"),
        "assignee": assignee.get("login"),
        "labels": labels,
        "url": issue.get("html_url"),
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
