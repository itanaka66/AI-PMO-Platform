"""スマホ向け Web サーバー / mobile web server.

Web サーバーも AI サーバーも、どこで動かすかは利用者が決める。
ここが提供するのは待ち受け側だけで、URL・ポート・公開範囲は config で指定する。

Both the web server and the AI server are the operator's choice. This module
provides only the listener; host, port and exposure are configured.

セキュリティ / Security
-----------------------
ネットワークに開く以上、認証は必須。以下は既定で有効:

  - 既定の待ち受けは 127.0.0.1。スマホから使うには明示的に host を変える。
    誤って社内 LAN 全体に開くのを、既定では起こらないようにする。
  - トークン必須。未設定なら起動時に生成し、URL を表示する。
  - 比較は定数時間。トークンを総当たりで絞り込めないようにする。
  - 0.0.0.0 に開くときは警告を出す。TLS は前段のリバースプロキシで用意する前提。

  Opening a listener on a network makes authentication mandatory:
  - Binds to 127.0.0.1 by default; reaching it from a phone requires an
    explicit change, so exposing it to the whole office LAN cannot happen
    by accident.
  - A token is always required; one is generated and printed if unset.
  - Comparison is constant-time, so the token cannot be narrowed by timing.
  - Two roles, two separate tokens. A viewer token can read run history and
    templates but cannot start anything.

権限 / Roles
------------
  viewer    実行履歴とテンプレートを見られる。実行はできない。
            Reads run history and templates. Cannot start anything.
  operator  実行できる。
            Can start runs.

PMO の現場では「メンバーは進捗を見るだけ、担当者だけが実行」という分け方が
自然になる。トークンが1本しかないと、進捗を見せたいだけの相手に実行権限まで
渡すことになる。

In practice the split is that members watch progress while one person runs
things. With a single token, showing someone the progress means handing them
the ability to file issues and send messages.

**画面で実行ボタンを隠すのは権限管理ではありません。** サーバー側で拒否します。
**Hiding the run button is not access control.** The endpoint refuses.
  - Binding 0.0.0.0 emits a warning. TLS is expected from a reverse proxy.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Any

# FastAPI は注釈をモジュールの名前空間で解決する。
# `from __future__ import annotations` があるため注釈は文字列になり、
# 関数内 import では Request が解決できずクエリ引数と誤認される。
# だからここはモジュール先頭で import する。
#
# FastAPI resolves annotations against module globals. With
# `from __future__ import annotations` they are strings, so a function-local
# import leaves Request unresolvable and it gets treated as a query parameter.
# Hence these imports live at module level.
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..dsl import loader
from ..engine.context import RunContext
from ..engine.runner import Engine, StepFailure
from ..i18n import CATALOG, DEFAULT_LANG, detect, normalize

logger = logging.getLogger("aipmo.web")

STATIC_DIR = Path(__file__).parent / "static"

# 実行履歴の保持件数。Postgres 連携が入るまではメモリ上のみ。
# In-memory run history until the Postgres wiring lands.
HISTORY_LIMIT = 50


class RateLimiter:
    """簡易インメモリ・レートリミッター / Simple in-memory rate limiter."""

    def __init__(self, limit: int = 10, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        timestamps = self._requests.get(key, [])
        # 期限内のリクエストのみ残す / Keep only requests within the window
        timestamps = [t for t in timestamps if now - t < self.window_seconds]

        if len(timestamps) >= self.limit:
            self._requests[key] = timestamps
            return False

        timestamps.append(now)
        self._requests[key] = timestamps
        return True


class RunStore:
    """実行履歴を新しい順に保持する / keeps run records, newest first."""

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self._runs: list[dict[str, Any]] = []
        self._limit = limit

    def add(self, record: dict[str, Any]) -> None:
        self._runs.insert(0, record)
        del self._runs[self._limit:]

    def list(self) -> list[dict[str, Any]]:
        return list(self._runs)

    def get(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in self._runs if r["id"] == run_id), None)

def discover_templates(root: Path) -> list[dict[str, Any]]:
    """テンプレートを読み、壊れているものも一覧に残す。

    List templates, keeping broken ones visible. Hiding a template that fails
    to parse is the worst outcome: the user sees nothing and has no idea why.
    """
    found: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
        try:
            template = loader.load_file(path)
        except loader.TemplateError as exc:
            found.append({
                "path": str(path), "name": path.stem, "valid": False,
                "error": str(exc), "industry": None, "steps": [],
                "trigger": None, "description": "",
            })
            continue
        found.append({
            "path": str(path),
            "name": template.name,
            "valid": True,
            "error": None,
            "industry": template.industry,
            "description": template.description.strip(),
            "trigger": template.trigger.type,
            "steps": template.step_ids(),
        })
    return found

def create_app(
    engine: Engine,
    template_root: Path,
    token: str | None = None,
    viewer_token: str | None = None,
    tenant: str = "",
    lang: str | None = None,
    store: RunStore | None = None,
):
    runs = store or RunStore()
    ui_lang = normalize(lang) if lang else detect()
    rate_limiter = RateLimiter(limit=10, window_seconds=60.0)

    # Webhook 用のテンプレートキャッシュ
    # Webhooks cache templates so they don't hit the disk on every event.
    _template_cache: list[Any] = []
    _template_cache_loaded = False

    def _get_cached_templates() -> list[Any]:
        nonlocal _template_cache_loaded
        if not _template_cache_loaded:
            root = template_root.resolve()
            for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
                try:
                    _template_cache.append(loader.load_file(path))
                except loader.TemplateError:
                    continue
            _template_cache_loaded = True
        return _template_cache

    if not token:
        raise ValueError("web: 実行用トークンが必要です / an operator token is required")
    if viewer_token and secrets.compare_digest(viewer_token, token):
        # 同じ値だと、閲覧用を配った相手が実行もできてしまう。
        # 分離したつもりで分離できていない、が一番危ない。
        # Identical values would let everyone given the viewer token run
        # things: believing you have separated the roles when you have not is
        # the worst of the outcomes.
        raise ValueError(
            "web: 閲覧用と実行用のトークンは別の値にしてください "
            "/ the viewer and operator tokens must differ"
        )

    roles = {"operator": token, "viewer": viewer_token}

    app = FastAPI(title="AI-PMO", docs_url=None, redoc_url=None,
                  openapi_url=None)

    # -- 認証 / authentication --------------------------------------------

    def role_for(supplied: str) -> str | None:
        """トークンから権限を引く / resolve a token to its role.

        どのトークンとも一致しなかった場合に、どれに近かったかを
        漏らさないよう、全部を比較してから結果を見る。

        Every token is compared before the result is inspected, so a failure
        cannot reveal which one it came closest to.
        """
        matched: str | None = None
        for name, value in roles.items():
            if value and secrets.compare_digest(supplied, value):
                matched = name
        return matched

    def principal(request: Request) -> str:
        supplied = (
            request.headers.get("x-aipmo-token")
            or request.cookies.get("aipmo_token")
            or request.query_params.get("token")
            or ""
        )
        role = role_for(supplied)
        if role is None:
            raise HTTPException(status_code=401, detail="invalid token")
        return role

    def require_operator(request: Request) -> str:
        role = principal(request)
        if role != "operator":
            # 403 にする。認証は通っているが、権限が足りない。
            # 401 だと、利用者は「鍵が違う」と思って入れ直そうとする。
            # 403: the credential is valid, the permission is not. A 401 would
            # send the reader off to re-enter a key that was never the problem.
            raise HTTPException(
                status_code=403,
                detail="this token can view but not run",
            )
        return role

    guard = Depends(principal)
    operator_guard = Depends(require_operator)

    # -- 画面 / screens ----------------------------------------------------

    @app.get("/")
    def index(request: Request) -> Response:
        supplied = (
            request.cookies.get("aipmo_token")
            or request.query_params.get("token")
            or ""
        )
        role = role_for(supplied)
        if role is None:
            return FileResponse(STATIC_DIR / "locked.html", status_code=401)

        response = FileResponse(STATIC_DIR / "index.html")
        # クエリのトークンを Cookie に移す。以後 URL にキーが残らないので、
        # 共有・スクリーンショット・履歴からの漏洩を減らせる。
        # Move the token from the query string into a cookie so it stops
        # appearing in the address bar, screenshots and browser history.
        # TLS or X-Forwarded-Proto implies it should be a secure cookie.
        is_secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
        response.set_cookie(
            "aipmo_token", supplied, httponly=True, samesite="strict",
            secure=is_secure,
            max_age=60 * 60 * 24 * 30,
        )
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- API ---------------------------------------------------------------

    @app.get("/api/session")
    def session(role: str = guard) -> dict[str, Any]:
        return {
            "role": role,
            "can_run": role == "operator",
            "tenant": tenant,
            "lang": ui_lang,
            "strings": {**CATALOG[DEFAULT_LANG], **CATALOG[ui_lang]},
            "adapters": {
                name: sorted(engine.adapters.get(name).actions())
                for name in engine.adapters.names()
            },
        }

    @app.get("/api/templates", dependencies=[guard])
    def templates() -> dict[str, Any]:
        return {"items": discover_templates(template_root)}

    @app.get("/api/runs", dependencies=[guard])
    def run_list() -> dict[str, Any]:
        return {"items": runs.list()}

    @app.get("/api/runs/{run_id}", dependencies=[guard])
    def run_detail(run_id: str) -> Any:
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such run")
        return record

    def _do_run(template: Any, params: dict[str, Any], trigger: dict[str, Any],
                role: str) -> dict[str, Any]:
        """テンプレートを実際に走らせ、実行履歴の1件として記録する。

        `/api/runs`（同期）と `/api/webhook`（バックグラウンド実行）の
        両方から呼ばれる共通の実行本体。

        Actually runs a template and records it as one run-history entry.
        Shared by both `/api/runs` (synchronous) and `/api/webhook`
        (backgrounded).
        """
        ctx: RunContext | None
        try:
            ctx = engine.run(template, params=params, trigger=trigger)
            status = "success"
            error = None
        except StepFailure as exc:
            ctx = exc.context
            status = "failed"
            error = str(exc)
            if ctx is None:
                record: dict[str, Any] = {
                    "id": secrets.token_hex(6), "template": template.name,
                    "started_by": role,
                    "status": status, "error": error, "steps": [],
                    "started_at": None,
                }
                runs.add(record)
                return record

        record = {
            "id": ctx.run_id,
            "template": template.name,
            "started_by": role,
            "status": status,
            "error": error,
            "started_at": ctx.started_at.isoformat(),
            "steps": [
                {
                    "id": step_id,
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                    "attempts": result.attempts,
                    "error": result.error,
                }
                for step_id, result in ctx.results.items()
            ],
        }
        runs.add(record)
        return record

    @app.post("/api/runs")
    def start_run(request: Request, payload: dict[str, Any], role: str = operator_guard) -> Any:
        client_ip = request.client.host if request.client else "unknown"
        if not rate_limiter.is_allowed(client_ip):
            raise HTTPException(status_code=429, detail="Too Many Requests")

        raw_path = str(payload.get("path", ""))
        root = template_root.resolve()
        supplied = Path(raw_path)
        target = (supplied if supplied.is_absolute() else root / supplied).resolve()

        # テンプレート置き場の外を実行させない。
        # resolve() で正規化してから包含を確認するので、.. もシンボリックリンクも
        # 抜けられない。サブディレクトリは通す（一覧に出る以上、実行できないと筋が通らない）。
        # Never execute outside the template directory. Normalising with
        # resolve() before the containment check closes both `..` traversal and
        # symlinks. Subdirectories are allowed: listing a template the user
        # cannot then run would be incoherent.
        if not target.is_file() or not target.is_relative_to(root):
            raise HTTPException(status_code=400, detail="template not found")

        try:
            template = loader.load_file(target)
        except loader.TemplateError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

        record = _do_run(
            template,
            payload.get("params") or {},
            payload.get("trigger") or {},
            role
        )
        # 既存の API との互換性のため、failed 時に一部だけ 200 JSONResponse で返す挙動を維持する
        # (MVP としては _do_run 側にまとめず、呼び出し側でラップするのが無難)
        if record.get("status") == "failed" and not record.get("started_at"):
            return JSONResponse(status_code=200, content=record)

        return record

    @app.post("/api/webhook")
    def webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        payload: dict[str, Any] | None = None,
        role: str = operator_guard
    ) -> Any:
        payload = payload or {}
        event_type = (
            request.headers.get("x-github-event")
            or request.headers.get("x-gitlab-event")
            or request.headers.get("x-event-type")
            or request.query_params.get("event")
            or payload.get("event")
            or payload.get("action")
        )
        if not event_type:
            raise HTTPException(status_code=400, detail="event type not specified")

        matched = []
        for template in _get_cached_templates():
            if template.trigger.type == "event" and template.trigger.event == event_type:
                matched.append(template)

        if not matched:
            return JSONResponse(status_code=200, content={"detail": "no matching templates", "matched": 0})

        for template in matched:
            background_tasks.add_task(
                _do_run,
                template,
                {"payload": payload},
                {"type": "event", "event": event_type},
                f"{role} (webhook)"
            )

        return JSONResponse(
            status_code=200,
            content={
                "detail": "scheduled",
                "matched": len(matched),
                "templates": [t.name for t in matched]
            }
        )

    @app.get("/api/health", dependencies=[guard])
    def health() -> dict[str, Any]:
        return {
            "adapters": {
                name: engine.adapters.get(name).health_check()
                for name in engine.adapters.names()
            }
        }

    # -- WBS 再計画の承認 / WBS replan approval ----------------------------
    #
    # WBS再計画AIが提案した差分は、ここでしか人が見て決められない。
    # 承認・却下ともに operator のみ。閲覧は viewer にも許す
    # （進捗を見せたいだけの相手に決定権まで渡さない、という既存の役割分離
    # をそのまま流用する）。
    #
    # This is the only place a human sees and decides on a diff the
    # WBS-replanning AI proposed. Both approving and rejecting require
    # operator; viewing does not, reusing the same role split that already
    # keeps someone shown progress from also gaining the power to decide.

    def _postgres_or_503() -> Any:
        if not engine.adapters.has("postgres"):
            raise HTTPException(
                status_code=503,
                detail="postgres adapter is not configured / "
                       "postgres アダプタが設定されていません",
            )
        return engine.adapters.get("postgres")

    @app.get("/api/wbs-proposals", dependencies=[guard])
    def list_wbs_proposals() -> dict[str, Any]:
        pg = _postgres_or_503()
        result = pg.query("pending_wbs_proposals", {"tenant": tenant})
        return {"items": result["rows"]}

    @app.get("/api/wbs-proposals/{proposal_id}", dependencies=[guard])
    def get_wbs_proposal(proposal_id: str) -> Any:
        pg = _postgres_or_503()
        result = pg.query("get_wbs_proposal", {"tenant": tenant, "id": proposal_id})
        if not result["rows"]:
            raise HTTPException(status_code=404, detail="no such proposal")
        return result["rows"][0]

    def _decide_wbs_proposal(
        proposal_id: str, status: str, role: str, note: str | None,
    ) -> dict[str, Any]:
        pg = _postgres_or_503()
        result = pg.execute("decide_wbs_proposal", {
            "tenant": tenant, "id": proposal_id, "status": status,
            "decided_by": role, "decision_note": note,
        })
        if not result["rows"]:
            # pending でなかった（既に決定済み・存在しない・staleになった）。
            # No such id, or it was not pending (already decided, or gone stale).
            raise HTTPException(
                status_code=409,
                detail="proposal is not pending (already decided, missing, or stale)",
            )
        return result["rows"][0]

    @app.post("/api/wbs-proposals/{proposal_id}/approve")
    def approve_wbs_proposal(
        proposal_id: str, payload: dict[str, Any] | None = None,
        role: str = operator_guard,
    ) -> dict[str, Any]:
        note = (payload or {}).get("note")
        return _decide_wbs_proposal(proposal_id, "approved", role, note)

    @app.post("/api/wbs-proposals/{proposal_id}/reject")
    def reject_wbs_proposal(
        proposal_id: str, payload: dict[str, Any] | None = None,
        role: str = operator_guard,
    ) -> dict[str, Any]:
        note = (payload or {}).get("note")
        return _decide_wbs_proposal(proposal_id, "rejected", role, note)

    return app

def generate_token() -> str:
    return secrets.token_urlsafe(24)
