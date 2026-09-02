"""Jira Cloud アダプタ / Jira Cloud adapter.

認証は API トークンによる Basic 認証。無人で動かすので、
利用者のサインインには依存させない。

Authenticates with an API token over Basic auth: this runs unattended and
cannot depend on a person being signed in.

仕様変更でつまずく点 / Where the API bites
------------------------------------------
1. 検索の旧エンドポイントは削除済み
   `/rest/api/3/search` は 2025年に段階的に停止され、現在は 410 を返す。
   `/rest/api/3/search/jql` に移行し、ページ送りは startAt ではなく
   nextPageToken になった。

   `/rest/api/3/search` was shut down during 2025 and now answers 410. The
   replacement is `/rest/api/3/search/jql`, paginated by nextPageToken rather
   than startAt.

2. 新しい検索は既定で id しか返さない
   旧エンドポイントは *navigable が既定だった。移行して「なぜか中身が空」に
   なるのはこれが原因なので、fields は必ず明示する。

   The old endpoint defaulted to *navigable; the new one returns id alone.
   This is why a migrated query comes back looking empty, so fields are always
   named explicitly here.

3. 説明文は ADF でないと通らない
   v3 の description は Atlassian Document Format。素の文字列を渡すと 400。

   In v3, description must be Atlassian Document Format. A plain string is
   rejected with a 400.

4. 担当者は accountId でしか指定できない
   氏名やメールアドレスでは設定できない。検索して引き当てる必要がある。

   Assignee accepts only an accountId — not a name, not an email address — so
   it has to be looked up.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Adapter, AdapterError, action

logger = logging.getLogger("aipmo.adapters.jira")

RETRY_STATUSES = {429, 500, 502, 503, 504}

# 検索で明示的に要求する項目。既定は id のみなので、書かないと空に見える。
# Named explicitly: the endpoint returns id alone by default.
DEFAULT_FIELDS = ["summary", "status", "assignee", "duedate", "priority", "labels"]

# 冪等性のための印。Jira に冪等キーの仕組みは無いので、
# ラベルとして残し、作る前に検索して重複を避ける。
# Jira has no idempotency mechanism, so the key is carried as a label and
# looked up before creating.
IDEMPOTENCY_PREFIX = "aipmo-"

def to_adf(text: str) -> dict[str, Any]:
    """素の文字列を Atlassian Document Format にする。

    空段落は入れない。Jira 側で検証に落ちることがある。
    Empty paragraphs are omitted; Jira sometimes rejects them.
    """
    paragraphs = [line.strip() for line in (text or "").split("\n\n")]
    content = [
        {"type": "paragraph",
         "content": [{"type": "text", "text": paragraph}]}
        for paragraph in paragraphs if paragraph
    ]
    if not content:
        content = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": content}

class JiraAdapter(Adapter):
    name = "jira"

    def __init__(
        self,
        site: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        project: str | None = None,
        issue_type: str = "Task",
        transport: Any = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.site = (site or "").rstrip("/")
        self.email = email
        self.api_token = api_token
        self.project = project
        self.issue_type = issue_type
        self.max_retries = max_retries
        self.timeout = timeout
        self._transport = transport
        self._account_cache: dict[str, str | None] = {}

    # -- HTTP ---------------------------------------------------------------

    def _auth_header(self) -> str:
        for value, name in ((self.site, "site"), (self.email, "email"),
                            (self.api_token, "api_token")):
            if not value:
                raise AdapterError(
                    f"jira: {name} が設定されていません / {name} is not configured"
                )
        pair = f"{self.email}:{self.api_token}".encode("utf-8")
        return "Basic " + base64.b64encode(pair).decode("ascii")

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.site}{path}"
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
                    logger.warning("jira: %s, retrying in %.0fs", exc.code, wait)
                    time.sleep(min(wait, 60))
                    continue
                return exc.code, _decode(raw)
            except urllib.error.URLError as exc:
                if attempt == self.max_retries:
                    raise AdapterError(f"jira: 接続できません / cannot reach Jira: {exc}")
                time.sleep(2 ** attempt)

        raise AdapterError("jira: 失敗しました / request failed")

    def _require(self, status: int, data: Any, what: str) -> Any:
        if status == 410:
            # 削除済みエンドポイントを叩いている。原因が分かる形で言う。
            # A removed endpoint. Say so in terms that point at the cause.
            raise AdapterError(
                f"jira: {what} — 410。旧 API は削除されています "
                f"/ this endpoint has been removed. "
                f"/rest/api/3/search/jql を使ってください."
            )
        if status == 401:
            raise AdapterError(
                "jira: 401 — メールアドレスと API トークンを確認してください "
                "/ check the email address and API token. "
                "トークンは失効することがあります / tokens do expire."
            )
        if status == 403:
            raise AdapterError(
                "jira: 403 — このユーザーに操作の権限がありません "
                "/ this account lacks permission for that operation."
            )
        if status >= 400:
            messages = data.get("errorMessages") if isinstance(data, dict) else None
            errors = data.get("errors") if isinstance(data, dict) else None
            raise AdapterError(
                f"jira: {what} に失敗 ({status}): {messages or errors or data}"
            )
        return data

    def health_check(self) -> bool:
        try:
            status, _ = self._request("GET", "/rest/api/3/myself")
            return status < 400
        except Exception:
            return False

    # -- 担当者の解決 / resolving assignees ---------------------------------

    def _account_id(self, who: str | None) -> str | None:
        """氏名やメールから accountId を引く。

        引けなければ None を返して未割り当てにする。ここで例外にすると、
        担当者が1人分からないだけで課題が1件も作られない。
        未割り当ての課題は後から直せる。

        Returns None when the person cannot be resolved, leaving the issue
        unassigned. Raising would mean one unrecognised name prevents every
        issue from being created; an unassigned issue can be fixed later.
        """
        if not who:
            return None
        if who in self._account_cache:
            return self._account_cache[who]

        query = urllib.parse.quote(who)
        status, data = self._request(
            "GET", f"/rest/api/3/user/search?query={query}")

        account_id = None
        if status < 400 and isinstance(data, list) and data:
            account_id = data[0].get("accountId")
        else:
            logger.warning("jira: 担当者を解決できません / unresolved assignee: %s", who)

        self._account_cache[who] = account_id
        return account_id

    # -- アクション / actions -----------------------------------------------

    @action()
    def find_overdue(self, project: str | None = None,
                     as_of: str | None = None) -> dict[str, Any]:
        """期限を過ぎた未完了の課題 / open issues past their due date."""
        key = project or self.project
        if not key:
            raise AdapterError("jira: project が必要です / project is required")

        jql = (f'project = "{key}" AND duedate < '
               f'{"now()" if not as_of else f"{as_of}"} '
               f"AND statusCategory != Done ORDER BY duedate ASC")
        return self.search(jql=jql)

    @action()
    def search(self, jql: str, project: str | None = None, fields: list[str] | None = None,
               limit: int = 50) -> dict[str, Any]:
        """JQL で検索する / search with JQL.

        新しいエンドポイントを使い、fields を明示する。
        既定は id のみなので、書かないと中身が空に見える。

        安全のため、必ずプロジェクトで絞り込む。
        jql のみに依存すると、任意のプロジェクトの情報を引けてしまうため。

        Uses the current endpoint and names the fields.
        Enforces a project filter for safety, preventing queries from reading
        across the entire Jira instance.
        """
        key = project or self.project
        if not key:
            raise AdapterError("jira: project が必要です / project is required")

        # ORDER BY を分離して括弧の外に出す / Extract ORDER BY to keep it outside the parens
        match = re.search(r'(?i)\s+ORDER\s+BY\s+', jql)
        if match:
            condition = jql[:match.start()].strip()
            order_by = jql[match.start():].strip()
        else:
            condition = jql.strip()
            order_by = ""

        if condition:
            safe_jql = f'project = "{key}" AND ({condition})'
        else:
            safe_jql = f'project = "{key}"'

        if order_by:
            safe_jql += f" {order_by}"

        payload = {
            "jql": safe_jql,
            "fields": fields or DEFAULT_FIELDS,
            "maxResults": min(limit, 100),
        }
        status, data = self._request("POST", "/rest/api/3/search/jql", payload)
        data = self._require(status, data, "検索 / search")

        items = []
        for issue in data.get("issues") or []:
            values = issue.get("fields") or {}
            assignee = values.get("assignee") or {}
            status_field = values.get("status") or {}
            items.append({
                "key": issue.get("key"),
                "summary": values.get("summary"),
                "status": status_field.get("name"),
                "assignee": assignee.get("displayName"),
                "assignee_id": assignee.get("accountId"),
                "due_date": values.get("duedate"),
                "labels": values.get("labels") or [],
            })

        return {
            "items": items,
            "count": len(items),
            # 全件が要るときのために残す / kept for callers that need to page
            "next_page_token": data.get("nextPageToken"),
        }

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]],
                      project: str | None = None,
                      issue_type: str | None = None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
        """課題を作る / create issues.

        同じ冪等キーで既に作られていれば、作り直さない。
        会議を2回処理しても課題が二重にならないようにするため。

        Nothing is recreated for a key that already produced issues, so
        reprocessing a meeting does not duplicate its tasks.
        """
        key = project or self.project
        if not key:
            raise AdapterError("jira: project が必要です / project is required")
        if not issues:
            return {"created": [], "count": 0, "skipped": "no issues supplied"}

        label = f"{IDEMPOTENCY_PREFIX}{idempotency_key}" if idempotency_key else None

        if label:
            existing = self.search(jql=f'labels = "{label}"', limit=100)
            if existing["count"]:
                logger.info("jira: 作成済みのため省略 / already created for %s", label)
                return {
                    "created": [item["key"] for item in existing["items"]],
                    "count": existing["count"],
                    "skipped": "already created for this idempotency key",
                }

        prepared = []
        unresolved = []
        for issue in issues:
            fields: dict[str, Any] = {
                "project": {"key": key},
                "issuetype": {"name": issue.get("issue_type")
                              or issue_type or self.issue_type},
                "summary": (issue.get("summary") or "")[:255],
                "description": to_adf(issue.get("description") or ""),
            }

            who = issue.get("assignee")
            account_id = self._account_id(who)
            if account_id:
                fields["assignee"] = {"accountId": account_id}
            elif who:
                unresolved.append(who)

            if issue.get("due_date"):
                fields["duedate"] = issue["due_date"]
            if issue.get("priority"):
                fields["priority"] = {"name": issue["priority"]}

            labels = list(issue.get("labels") or [])
            if label:
                labels.append(label)
            if labels:
                fields["labels"] = labels

            prepared.append({"fields": fields})

        status, data = self._request(
            "POST", "/rest/api/3/issue/bulk", {"issueUpdates": prepared})
        data = self._require(status, data, "課題の作成 / issue creation")

        created = [item.get("key") for item in (data.get("issues") or [])]
        result: dict[str, Any] = {"created": created, "count": len(created)}

        # 一部だけ失敗することがある。黙って成功として返さない。
        # Partial failure happens; it is not reported as success.
        failures = data.get("errors") or []
        if failures:
            result["failed"] = failures
            result["failed_count"] = len(failures)
        if unresolved:
            result["unassigned"] = unresolved

        return result

    @action()
    def list_transitions(self, issue_key: str) -> dict[str, Any]:
        """その課題からいま進める先の一覧 / where this issue can move to now.

        Jira の状態は項目の書き換えでは変えられない。ワークフローに沿った
        「遷移」を通す必要があり、進める先は現在の状態によって変わる。

        Status is not a field you can write: Jira moves an issue through
        workflow transitions, and which ones exist depends on where it is now.
        """
        status, data = self._request(
            "GET", f"/rest/api/3/issue/{issue_key}/transitions")
        data = self._require(status, data, "遷移の取得 / listing transitions")

        items = [
            {"id": item.get("id"), "name": item.get("name"),
             "to": (item.get("to") or {}).get("name")}
            for item in (data.get("transitions") or [])
        ]
        return {"items": items, "count": len(items)}

    @action(writes=True)
    def transition_issue(self, issue_key: str, to_status: str,
                         comment: str | None = None) -> dict[str, Any]:
        """状態を進める / move an issue to another status.

        遷移の名前でも、行き先の状態名でも指定できるようにしてある。
        ワークフローによって「完了」「Done」「クローズ」と呼び名が違うため、
        利用者がどちらで書いても通るようにする。

        Accepts either the transition's name or the destination status: what
        one workflow calls "Done" another calls "Closed", and the person
        writing the template should not have to know which.
        """
        available = self.list_transitions(issue_key)["items"]
        wanted = to_status.strip().casefold()

        match = next(
            (item for item in available
             if (item["name"] or "").casefold() == wanted
             or (item["to"] or "").casefold() == wanted),
            None,
        )
        if match is None:
            # いまの状態からは進めない先。ワークフロー上の理由があるので、
            # 何が可能かを示して止まる。
            # Not reachable from where the issue is. There is a workflow reason
            # for that, so this stops and says what is possible instead.
            options = ", ".join(f"{i['name']} → {i['to']}" for i in available)
            raise AdapterError(
                f"jira: {issue_key} を '{to_status}' に進められません "
                f"/ cannot move {issue_key} to '{to_status}'.\n"
                f"  いまの状態から可能な遷移 / available from here: {options or 'なし / none'}"
            )

        payload: dict[str, Any] = {"transition": {"id": match["id"]}}
        if comment:
            payload["update"] = {"comment": [{"add": {"body": to_adf(comment)}}]}

        status, data = self._request(
            "POST", f"/rest/api/3/issue/{issue_key}/transitions", payload)
        self._require(status, data, f"{issue_key} の遷移 / transitioning")
        return {"issue_key": issue_key, "to": match["to"],
                "transition": match["name"]}

    @action(writes=True)
    def update_issue(self, issue_key: str, summary: str | None = None,
                     description: str | None = None, assignee: str | None = None,
                     due_date: str | None = None, priority: str | None = None,
                     add_labels: list[str] | None = None,
                     comment: str | None = None) -> dict[str, Any]:
        """課題を書き換える / change fields on an existing issue.

        **渡された項目だけを送ります。** 指定しなかった項目には触れません。

        更新は新規作成より危ない操作です。作成の誤りは余計な課題が1件増える
        だけですが、更新の誤りは**すでに正しかった値を消します。** 期限や
        担当者は、誰かが考えて入れたものです。空を送って消すことはしません。

        **Only the fields given are sent**; anything omitted is left alone.

        Updating is more dangerous than creating. A mistaken create adds one
        unwanted issue; a mistaken update **destroys a value that was right** —
        a due date or an assignee someone thought about and entered. Nothing is
        cleared by omission.
        """
        fields: dict[str, Any] = {}

        if summary is not None:
            fields["summary"] = summary[:255]
        if description is not None:
            fields["description"] = to_adf(description)
        if due_date is not None:
            fields["duedate"] = due_date
        if priority is not None:
            fields["priority"] = {"name": priority}

        unresolved = None
        if assignee is not None:
            account_id = self._account_id(assignee)
            if account_id:
                fields["assignee"] = {"accountId": account_id}
            else:
                # 引き当てられない担当者で、既存の担当を消さない。
                # Never unassign someone because a name could not be resolved.
                unresolved = assignee

        payload: dict[str, Any] = {}
        if fields:
            payload["fields"] = fields
        if add_labels:
            payload["update"] = {"labels": [{"add": label} for label in add_labels]}
        if comment:
            payload.setdefault("update", {})["comment"] = [
                {"add": {"body": to_adf(comment)}}]

        if not payload:
            # 何も送らない場合でも、担当者を引き当てられなかった事実は返す。
            # 黙って「変更なし」とだけ返すと、名前の綴り違いに気づけない。
            # Even with nothing to send, an unresolved name is reported: a bare
            # "no change" would hide a misspelling from the caller.
            result: dict[str, Any] = {"issue_key": issue_key, "changed": [],
                                      "skipped": "nothing to change"}
            if unresolved:
                result["unresolved_assignee"] = unresolved
            return result

        status, data = self._request(
            "PUT", f"/rest/api/3/issue/{issue_key}", payload)
        self._require(status, data, f"{issue_key} の更新 / updating")

        result = {"issue_key": issue_key, "changed": sorted(fields)}
        if add_labels:
            result["labels_added"] = add_labels
        if unresolved:
            result["unresolved_assignee"] = unresolved
        return result

    @action(writes=True)
    def add_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        status, data = self._request(
            "POST", f"/rest/api/3/issue/{issue_key}/comment", {"body": to_adf(text)})
        data = self._require(status, data, "コメントの追加 / adding a comment")
        return {"id": data.get("id"), "issue_key": issue_key}

def _decode(payload: bytes | str | None) -> Any:
    if payload is None:
        return {}
    if isinstance(payload, str):
        text = payload
    else:
        text = payload.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:500]}
