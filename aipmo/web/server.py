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

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..dsl import loader
from ..engine.context import RunContext
from ..engine.runner import Engine, StepFailure
from ..i18n import CATALOG, DEFAULT_LANG, detect, normalize
from ..pmo_core import Member, PmoCore, scope_briefing
from ..writeback import WritebackError, make_writer, tracker_of, writable_trackers
from ..ledger_store import LedgerConfigError, LedgerStore, LedgerTenantError
from ..task_engine import TaskEngine, side_path

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

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def create_app(
    engine: Engine,
    template_root: Path,
    token: str | None = None,
    viewer_token: str | None = None,
    tenant: str = "",
    lang: str | None = None,
    store: RunStore | None = None,
    cors_origins: list[str] | None = None,
    pmo_ledger: Path | None = None,
    viewer_projects: list[str] | None = None,
    ledger_store_factory: Callable[[], LedgerStore] | None = None,
    members: list[Member] | None = None,
):
    runs = store or RunStore()
    # 閲覧用トークンが見てよいプロジェクト。未設定（空）なら制限なし。
    # 空のリストを「何も見せない」と読むと、設定の書き忘れで画面が空に
    # なるだけで原因に気づけないので、制限なしとして扱う。
    # Projects the viewer token may see; unset or empty means no limit (an
    # empty list read as "show nothing" would only look like a broken screen).
    scoped_projects = {p.lower() for p in (viewer_projects or []) if p} or None
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

    # -- CORS -----------------------------------------------------------
    #
    # 既定では何も付けない。この画面はトークンをクエリ文字列やクッキーで
    # 運ぶため、任意のオリジンからの読み取りを許すと、認証だけでは防げない
    # 経路が生まれる。別オリジンの画面・アプリから叩く場合だけ、
    # 許可するオリジンを明示させる。
    #
    # No CORS headers by default. This screen carries its token in a query
    # string or cookie; allowing any origin to read responses would open a
    # path that authentication alone does not close. Only set up CORS when
    # an operator explicitly names the origins that should be allowed to
    # call this from elsewhere.
    if cors_origins:
        # ワイルドカードは資格情報つき（Cookie）の応答と両立しない
        # ——ブラウザ側が拒否する。ワイルドカードを渡された場合は
        # allow_credentials を落とし、クエリ文字列トークンでの利用に限る。
        #
        # A wildcard origin cannot be combined with credentialed (cookie)
        # responses — browsers refuse it. Given a wildcard, credentials are
        # dropped instead, limiting cross-origin use to the query-string
        # token.
        allow_credentials = "*" not in cors_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
            # トークンそのものはログに残さない。誤って有効な鍵に近い値を
            # 書き残さないため。
            # Never logs the supplied token itself, so a value close to a
            # real credential doesn't end up sitting in the logs.
            logger.warning(
                "auth failed: invalid token from %s (%s %s)",
                _client_ip(request), request.method, request.url.path,
            )
            raise HTTPException(status_code=401, detail="invalid token")
        return role

    def require_operator(request: Request) -> str:
        role = principal(request)
        if role != "operator":
            # 403 にする。認証は通っているが、権限が足りない。
            # 401 だと、利用者は「鍵が違う」と思って入れ直そうとする。
            # 403: the credential is valid, the permission is not. A 401 would
            # send the reader off to re-enter a key that was never the problem.
            logger.warning(
                "permission denied: %s token attempted an operator action "
                "from %s (%s %s)",
                role, _client_ip(request), request.method, request.url.path,
            )
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
            "writeback": sorted(writable_trackers(engine.adapters)),
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
                logger.info("run %s: template=%s started_by=%s status=%s",
                           record["id"], template.name, role, status)
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
        logger.info("run %s: template=%s started_by=%s status=%s",
                   record["id"], template.name, role, status)
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

        logger.info("webhook received: event=%s matched=%d role=%s from %s",
                   event_type, len(matched), role, _client_ip(request))

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

    # -- PMO Core の表示 / PMO Core view -----------------------------------
    #
    # 台帳（task-ledger.db）と、常駐側 (`aipmo schedule`) が周ごとに書く
    # ブリーフィング・判断ログを**読むだけ**。画面から周を回したり、通知や
    # テンプレート起動を起こしたりはしない。書けるのは担当の確定だけで、
    # operator のみ。
    #
    # Reads the ledger and what the resident `aipmo schedule` writes each
    # cycle (briefing, decision log); never runs a cycle, notifies or
    # launches templates from here. The one write is confirming an assignment,
    # and that needs operator.

    def _pmo_files() -> tuple[Path, Path, Path]:
        if pmo_ledger is None:
            raise HTTPException(status_code=404, detail="PMO Core is not configured")
        return (pmo_ledger, side_path(pmo_ledger, "pmo-briefing.json"),
                side_path(pmo_ledger, "pmo-decisions.jsonl"))

    def _ledger_present(ledger: Path) -> bool:
        # PostgreSQL の台帳は、ファイルの有無では分からない（接続できれば有る）。
        # A PostgreSQL ledger cannot be told by a file; if it connects, it is there.
        return ledger_store_factory is not None or TaskEngine.exists(ledger)

    def _open_store(ledger: Path) -> TaskEngine:
        try:
            return TaskEngine(ledger, tenant=tenant or None,
                              store=ledger_store_factory() if ledger_store_factory else None)
        except LedgerTenantError as exc:
            # 別テナントの台帳を指している。データには一切触れずに断る。
            # Pointed at another tenant's ledger: refuse without touching data.
            logger.error("ledger tenant mismatch: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except LedgerConfigError as exc:
            logger.error("ledger unavailable: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _allowed_projects(role: str, requested: str | None) -> set[str] | None:
        """この呼び出しが見てよいプロジェクト（小文字）。None は制限なし。

        閲覧用トークンに `web.viewer_projects` を設定していれば、閲覧者は
        そのプロジェクトだけを見られる。絞り込み（?project=）はその内側でのみ。
        範囲外のプロジェクトを名指しされたら 403 — 「無い」と答えて存在を
        教えるより、権限が無いと答える方が、利用者が原因に気づける。

        The projects this call may see (lower-case); None means no limit. With
        `web.viewer_projects` set, a viewer sees those projects only, and a
        ?project= filter narrows within them. Naming a project outside the
        scope is a 403.
        """
        confined = scoped_projects if role == "viewer" else None
        if requested:
            wanted = requested.lower()
            if confined is not None and wanted not in confined:
                raise HTTPException(
                    status_code=403,
                    detail="this token cannot see that project")
            return {wanted}
        return confined

    @app.get("/api/pmo", dependencies=[guard])
    def pmo_view(project: str | None = None, role: str = guard) -> dict[str, Any]:
        ledger, briefing_file, _ = _pmo_files()
        if not _ledger_present(ledger) and not briefing_file.exists():
            raise HTTPException(status_code=404, detail="no PMO data yet")
        allowed = _allowed_projects(role, project)
        confined = role == "viewer" and scoped_projects is not None

        briefing = None
        age = None
        try:
            briefing = json.loads(briefing_file.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(briefing["generated_at"])
            age = int((datetime.now(timezone.utc) - generated).total_seconds())
        except (OSError, ValueError, KeyError):
            briefing = None

        active: list[Any] = []
        names: list[str] = []
        if _ledger_present(ledger):
            store = _open_store(ledger)
            try:
                active = store.ranked(projects=allowed)
                names = [n for n in store.projects()
                         if not confined or scoped_projects is None
                         or n.lower() in scoped_projects]
            finally:
                store.close()

        if briefing is not None and allowed is not None:
            briefing = scope_briefing(briefing, active, allowed, redact_org=confined)

        tasks = [
            {"id": t.id, "key": t.key, "title": t.title, "score": t.score,
             "project": t.project, "tracker": tracker_of(t),
             "external_id": t.external_id or (t.key if tracker_of(t) == "jira" else None),
             "assignee": t.assignee, "suggested_assignee": t.suggested_assignee,
             "suggestion_reason": t.suggestion_reason, "due_date": t.due_date,
             "priority": t.priority, "status": t.status, "blocked": t.blocked,
             "reasons": t.reasons, "templates": t.templates}
            for t in active[:50]
        ]
        return {"briefing": briefing, "briefing_age_seconds": age, "tasks": tasks,
                "projects": names, "scoped": confined}

    @app.get("/api/pmo/decisions", dependencies=[guard])
    def pmo_decisions(limit: int = 50, project: str | None = None,
                      role: str = guard) -> dict[str, Any]:
        ledger, _, decisions = _pmo_files()
        limit = max(1, min(limit, 200))
        allowed = _allowed_projects(role, project)
        try:
            lines = decisions.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"items": []}

        visible: set[str] | None = None
        if allowed is not None:
            # 判断ログの行はタスクの id を持つ。範囲内のプロジェクトのタスクの
            # 行だけを返し、組織全体の判断（学習の更新など）は返さない。
            # A log line carries its task id. Only lines about tasks in the
            # allowed projects are returned; organisation-wide decisions
            # (a learned-model update, say) are not.
            visible = set()
            if _ledger_present(ledger):
                store = _open_store(ledger)
                try:
                    visible = {t.id for t in store.tasks.values()
                               if t.project.lower() in allowed}
                finally:
                    store.close()
            lines = lines[-2000:]
        else:
            lines = lines[-limit:]

        items = []
        for line in reversed(lines):        # 新しい順 / newest first
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if visible is not None and entry.get("task") not in visible:
                continue
            items.append(entry)
            if len(items) >= limit:
                break
        return {"items": items}

    @app.post("/api/pmo/assignments/accept")
    def pmo_accept(request: Request, payload: dict[str, Any],
                   role: str = operator_guard) -> dict[str, Any]:
        ledger, _, _ = _pmo_files()
        ref = str(payload.get("ref") or "")
        if not ref:
            raise HTTPException(status_code=422, detail="ref is required")

        # そのタスクのトラッカーにも書くか。`jira` は従来の名前（同じ意味）。
        # Also write to the task's own tracker; `jira` is the old name.
        write = (make_writer(engine.adapters, members or [])
                 if payload.get("writeback") or payload.get("jira") else None)

        store = _open_store(ledger)
        core = PmoCore(task_engine=store)
        try:
            task = core.accept_assignment(ref, write=write)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WritebackError as exc:
            # 設定・宛先の問題は 4xx/503、トラッカーが受け付けなかったのは 502。
            # Config and target problems are 4xx/503; the tracker refusing is 502.
            status = {"adapter": 503, "target": 422, "account": 422}.get(exc.kind, 502)
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            store.close()
        written = (core.last_writeback or {}).get("tracker")
        logger.info("pmo assignment accepted: %s -> %s by %s from %s (written to %s)",
                    task.id, task.assignee, role, _client_ip(request), written or "ledger only")
        return {"id": task.id, "assignee": task.assignee, "written_to": written,
                "jira_updated": written == "jira"}

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
        request: Request, proposal_id: str, status: str, role: str,
        note: str | None,
    ) -> dict[str, Any]:
        pg = _postgres_or_503()
        result = pg.execute("decide_wbs_proposal", {
            "tenant": tenant, "id": proposal_id, "status": status,
            "decided_by": role, "decision_note": note,
        })
        if not result["rows"]:
            # pending でなかった（既に決定済み・存在しない・staleになった）。
            # No such id, or it was not pending (already decided, or gone stale).
            logger.warning(
                "wbs proposal decision rejected: %s %s by %s from %s "
                "(not pending)",
                proposal_id, status, role, _client_ip(request),
            )
            raise HTTPException(
                status_code=409,
                detail="proposal is not pending (already decided, missing, or stale)",
            )
        # WBS の計画そのものを変える決定なので、DB の decided_by/decided_at/
        # decision_note とは別に、アプリのログにも残す。DB はテナント単位の
        # クエリでしか見えないが、ログは通常の監視・集約基盤にそのまま流れる。
        #
        # This changes the WBS plan itself, so it's logged here in addition to
        # the DB's own decided_by/decided_at/decision_note — the DB is only
        # visible through a tenant-scoped query, while the log reaches
        # whatever monitoring/aggregation pipeline is already watching this
        # process.
        logger.info(
            "wbs proposal decided: %s %s by %s from %s%s",
            proposal_id, status, role, _client_ip(request),
            f' note="{note}"' if note else "",
        )
        return result["rows"][0]

    @app.post("/api/wbs-proposals/{proposal_id}/approve")
    def approve_wbs_proposal(
        request: Request, proposal_id: str, payload: dict[str, Any] | None = None,
        role: str = operator_guard,
    ) -> dict[str, Any]:
        note = (payload or {}).get("note")
        return _decide_wbs_proposal(request, proposal_id, "approved", role, note)

    @app.post("/api/wbs-proposals/{proposal_id}/reject")
    def reject_wbs_proposal(
        request: Request, proposal_id: str, payload: dict[str, Any] | None = None,
        role: str = operator_guard,
    ) -> dict[str, Any]:
        note = (payload or {}).get("note")
        return _decide_wbs_proposal(request, proposal_id, "rejected", role, note)

    # -- Project Digital Twin（読み取り専用） -------------------------------
    #
    # 同期・診断そのものはテンプレート実行（既存の /api/runs）から行う。
    # ここは結果を見るためだけの、読み取り専用の2エンドポイント。
    #
    # Syncing and diagnosing happen through template runs (the existing
    # /api/runs), not here. These two routes only let you look at the result.

    @app.get("/api/v1/projects/{project_id}/state", dependencies=[guard])
    def get_project_state(project_id: str) -> dict[str, Any]:
        pg = _postgres_or_503()
        project = pg.query("dt_get_project", {"tenant": tenant, "project_id": project_id})
        if not project["rows"]:
            raise HTTPException(status_code=404, detail="no such project")
        params = {"tenant": tenant, "project_id": project_id}
        return {
            "project": project["rows"][0],
            "wbs_nodes": pg.query("dt_list_wbs_nodes", params)["rows"],
            "tasks": pg.query("dt_list_tasks", params)["rows"],
            "schedule_forecast": pg.query("dt_latest_schedule_forecast", params)["rows"][0],
            "resources": pg.query("dt_list_resources", params)["rows"],
            "risks": pg.query("dt_list_risks", params)["rows"],
            "issues": pg.query("dt_list_issues", params)["rows"],
            "dependencies": pg.query("dt_list_dependencies", params)["rows"],
            "budget": pg.query("dt_get_budget", params)["rows"][0],
            "decisions": pg.query("dt_list_decisions", params)["rows"],
            "documents": pg.query("dt_list_documents", params)["rows"],
        }

    @app.get("/api/v1/projects/{project_id}/diagnose", dependencies=[guard])
    def get_project_diagnosis(project_id: str) -> Any:
        pg = _postgres_or_503()
        result = pg.query(
            "dt_latest_health_diagnostic", {"tenant": tenant, "project_id": project_id},
        )
        if not result["rows"]:
            raise HTTPException(status_code=404, detail="no diagnosis recorded yet")
        return result["rows"][0]

    return app

def generate_token() -> str:
    return secrets.token_urlsafe(24)
