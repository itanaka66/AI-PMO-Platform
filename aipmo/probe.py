"""課題管理ツールとの接続を、読み取りだけで診断する。

実サービス（Jira・Plane・OpenProject など）につなぐとき、「疎通する／しない」だけでは原因が分からない。
ここは、アダプタに**読むだけ**の呼び出しを順に行い、どこで・なぜ止まるかを返す：

  1. `health`  … アダプタの疎通確認（URL・認証・プロジェクトの指定が合っているか）
  2. `read`    … 課題を 1 件だけ検索する（読み取りの権限と、検索の書き方が合っているか）
  3. `people`  … 担当候補の一覧を読む（`list_assignees` があるトラッカーだけ。担当者の引き当てに必要）

書き込みは**絶対にしない**：`writes=True` のアクションは呼ばない（門番）。エラー文からは、アダプタの設定に
ある秘密（トークン・パスワードなど）を伏せる。原因は `hint` に分類する：
`auth`（401/403）・`not_found`（404）・`timeout`・`network`・`rate_limit`・`other`。

Diagnoses a tracker connection with read-only calls and says where and why it stops. Never calls an action
that writes, and scrubs configured secrets from error text.
"""
from __future__ import annotations

import re
import time
from typing import Any

SECRET_WORDS = ("token", "key", "password", "secret", "authorization", "credential")

# 読み取りの確認に使う検索。トラッカーごとに引数の名前が違う。
READ_ARGS: dict[str, dict[str, Any]] = {
    "jira": {"jql": "ORDER BY created DESC", "fields": ["summary"], "limit": 1},
    "plane": {"limit": 1},
    "openproject": {"limit": 1},
    "github_projects": {"limit": 1},
    "azure_devops": {"limit": 1},
}


def _secrets(adapter: Any) -> list[str]:
    found = []
    for name, value in vars(adapter).items():
        if isinstance(value, str) and len(value) >= 6 and any(w in name.lower() for w in SECRET_WORDS):
            found.append(value)
    return found


def scrub(text: str, adapter: Any) -> str:
    """エラー文から秘密を伏せ、長さを抑える / hide secrets, cap the length."""
    for secret in _secrets(adapter):
        text = text.replace(secret, "***")
    text = re.sub(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 ***", text)
    return text[:240]


def classify(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(401|403)\b|unauthori[sz]ed|forbidden|認証|権限", low):
        return "auth"
    if re.search(r"\b404\b|not found|見つかりません", low):
        return "not_found"
    if re.search(r"\b429\b|rate limit|too many", low):
        return "rate_limit"
    if "timed out" in low or "timeout" in low or "タイムアウト" in low:
        return "timeout"
    if re.search(r"urlopen|connection|refused|resolve|name or service|getaddrinfo|ssl|certificate|network", low):
        return "network"
    return "other"


def _step(step_id: str, run) -> dict[str, Any]:
    started = time.monotonic()
    try:
        detail = run()
        return {"id": step_id, "ok": True, "ms": round((time.monotonic() - started) * 1000),
                "detail": detail, "hint": None}
    except Exception as exc:                                    # noqa: BLE001 — 診断は落ちずに理由を返す
        return {"id": step_id, "ok": False, "ms": round((time.monotonic() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}", "hint": None}


def probe_adapter(name: str, adapter: Any) -> dict[str, Any]:
    """1 つのアダプタを診断する。書き込みは呼ばない。"""
    steps: list[dict[str, Any]] = []

    def health() -> str:
        if not adapter.health_check():
            raise RuntimeError("health_check が偽でした（URL・認証・プロジェクトの指定を確かめてください）")
        return "ok"

    steps.append(_step("health", health))

    actions = adapter.actions()
    search = "search" if "search" in actions else None
    if name in READ_ARGS and search and not adapter.writes(search):
        args = dict(READ_ARGS.get(name, {}))

        def read() -> str:
            result = adapter.invoke(search, args)
            items = result.get("items") if isinstance(result, dict) else result
            return f"{len(items or [])} item(s)"

        steps.append(_step("read", read))

    if name in READ_ARGS and "list_assignees" in actions and not adapter.writes("list_assignees"):
        def people() -> str:
            result = adapter.invoke("list_assignees", {})
            items = result.get("items") if isinstance(result, dict) else result
            return f"{len(items or [])} people"

        steps.append(_step("people", people))

    for step in steps:
        if not step["ok"]:
            step["detail"] = scrub(step["detail"], adapter)
            step["hint"] = classify(step["detail"])
    return {"name": name, "ok": all(s["ok"] for s in steps), "steps": steps,
            "writes_back": bool(set(actions) & {"update_issue"}),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def probe_all(adapters: Any, only: str | None = None) -> list[dict[str, Any]]:
    """登録された課題管理ツール（と他のアダプタ）を診断する。`only` で 1 つだけ。"""
    names = [n for n in sorted(adapters.names()) if only in (None, n)]
    return [probe_adapter(n, adapters.get(n)) for n in names]


__all__ = ["classify", "probe_adapter", "probe_all", "scrub"]
