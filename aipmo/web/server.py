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

import hashlib
import json
import logging
import queue as queue_module
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
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

from ..adapters.base import AdapterError
from ..dsl import loader
from ..engine.context import RunContext
from ..engine.runner import Engine, StepFailure
from ..i18n import CATALOG, DEFAULT_LANG, detect, normalize
from ..judgment import AUTONOMY, RANK, REMEDIES, JudgmentConfig, read_control, write_control
from ..wbs_edit import WbsEditError, plan_changes, write_plan
from ..probe import probe_all
from ..messages import (is_japanese, localize_briefing, suggestion_reason, task_title,
                        translate)
from ..messages import localize_alert as localize_alert_text
from ..pmo_core import Member, PmoCore, scope_briefing
from ..inbox import build_inbox
from ..filing import FilingConfig, FilingError, eligible, filing_state, make_filer
from ..wbs import WbsError, load_wbs
from ..wbs import view as wbs_analysis
from ..wbs_proposals import ProposalError, applied_before, approve_and_apply
from ..wbs_proposals import plan_for as plan_wbs_proposal
from ..wbs_proposals import Target as WbsTarget
from ..writeback import WritebackError, make_writer, tracker_of, writable_trackers
from ..ledger_store import LedgerConfigError, LedgerStore, LedgerTenantError
from ..side_store import BRIEFING, DECISIONS, LEARNED, FileSide
from ..task_engine import TaskEngine, score_breakdown
from .pool import LedgerPool, PoolExhausted

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
    """実行履歴を新しい順に保持する / keeps run records, newest first.

    `add` は同じ id の行があれば置き換える（実行中の進捗を、同じ場所で
    更新していくため）。バックグラウンドの進捗更新と、画面からの一覧・
    詳細取得が別スレッドから同時に起こるので、ロックで守る。

    `add` replaces a record sharing its id in place (so progress on a
    running entry updates where it already sits). Background progress
    writes and the UI's list/detail reads happen from different threads, so
    a lock guards all three methods.
    """

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self._runs: list[dict[str, Any]] = []
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, record: dict[str, Any]) -> None:
        with self._lock:
            for index, existing in enumerate(self._runs):
                if existing["id"] == record["id"]:
                    self._runs[index] = record
                    return
            self._runs.insert(0, record)
            del self._runs[self._limit:]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._runs)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((r for r in self._runs if r["id"] == run_id), None)


class RunQueue:
    """テンプレートの実行を直列化する待ち行列 / a serial queue for template runs.

    ワーカーは1本だけ。LLM を呼ぶ工程が複数の実行から同時に走ることはなく、
    台帳にも同時書き込みは来ない。既に重いアダプタ呼び出しを直列化している
    箇所（Postgres・ベクトルストアの `_lock`）と同じ考え方——ここでは
    「待っている件数」そのものを画面に見せたいので、素の `BackgroundTasks`
    （並行に何本でも走る）ではなく、この待ち行列を自分で持つ。

    Exactly one worker thread. LLM-calling steps from different runs never
    overlap, and the ledger never sees concurrent writes from two runs. The
    same idea as the locking already used around heavy adapter calls
    (Postgres, the vector-store adapters' own `_lock`) — but here the queue
    itself needs to be visible on screen, so this keeps its own ordered list
    rather than firing jobs through FastAPI's `BackgroundTasks` (which would
    run them concurrently with no queue to show).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: list[str] = []
        self._running: str | None = None
        self._jobs: queue_module.Queue[tuple[str, Callable[[], None]] | None] = queue_module.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def enqueue(self, run_id: str, job: Callable[[], None]) -> None:
        with self._lock:
            self._waiting.append(run_id)
        self._jobs.put((run_id, job))

    def position(self, run_id: str) -> int | None:
        """1始まりの待ち順。待っていなければ None（実行中・完了済みも None）。"""
        with self._lock:
            if run_id not in self._waiting:
                return None
            return self._waiting.index(run_id) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._running, "waiting": list(self._waiting)}

    def _work(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                return
            run_id, job = item
            with self._lock:
                if run_id in self._waiting:
                    self._waiting.remove(run_id)
                self._running = run_id
            try:
                job()
            except Exception:
                logger.exception("run queue: job for %s crashed", run_id)
            finally:
                with self._lock:
                    self._running = None


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

_PRIORITY_ORDER = {"highest": 5, "critical": 5, "high": 4, "medium": 3, "low": 2, "lowest": 1}


def _priority_rank(priority: str | None) -> int:
    return _PRIORITY_ORDER.get((priority or "").strip().lower(), 0)


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
    filing: FilingConfig | None = None,
    lookup_assignees: bool = True,
    wbs_target: WbsTarget | None = None,
    wbs_view: tuple[Path, Path] | None = None,
    judgment: JudgmentConfig | None = None,
    side_storage: str = "auto",
    pool_size: int = 8,
    pool_timeout: float = 10.0,
    pool_idle: float = 300.0,
):
    runs = store or RunStore()
    run_queue = RunQueue()
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

    def _lang_for(request: Request) -> str:
        """この利用者の画面表示言語。ログイン画面で選んで Cookie に残したものが
        あればそれ、無ければサーバーの既定（設定の `lang` か、サーバー環境からの
        推定）。アプリの中には変える手段を置いていない——変えたいときは
        ログアウトしてログイン画面からやり直す、という決め事を、ここでは単に
        「アプリ内に別の変更経路を作らない」ことで保っている。

        The viewer's display language: the one chosen at login and kept in a
        cookie, if any; otherwise the server's own default (config `lang`, or
        inferred from the server's environment). Nothing inside the app itself
        offers another way to change it — the "only at login" rule is kept
        simply by never adding a second path.
        """
        cookie_lang = request.cookies.get("aipmo_lang")
        return normalize(cookie_lang) if cookie_lang else ui_lang

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
        # ログイン画面で選んだ表示言語を Cookie に残す。クエリに無ければ
        # （ブックマーク済みの URL を token だけで開き直した場合など）、
        # 前回までの選択をそのまま使う——上書きしない。
        # Persists the display language chosen on the login screen. Without a
        # `lang` query param (e.g. revisiting a bookmarked token-only URL),
        # whatever was chosen before stays — never silently overwritten.
        chosen_lang = request.query_params.get("lang")
        if chosen_lang and chosen_lang in CATALOG:
            response.set_cookie(
                "aipmo_lang", chosen_lang, httponly=False, samesite="strict",
                secure=is_secure,
                max_age=60 * 60 * 24 * 30,
            )
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- API ---------------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request, role: str = guard) -> dict[str, Any]:
        lang = _lang_for(request)
        return {
            "role": role,
            "can_run": role == "operator",
            "tenant": tenant,
            "lang": lang,
            "strings": {**CATALOG[DEFAULT_LANG], **CATALOG[lang]},
            "adapters": {
                name: sorted(engine.adapters.get(name).actions())
                for name in engine.adapters.names()
            },
            "writeback": sorted(writable_trackers(engine.adapters)),
        }

    @app.get("/api/templates", dependencies=[guard])
    def templates() -> dict[str, Any]:
        return {"items": discover_templates(template_root)}

    def _with_queue_position(record: dict[str, Any]) -> dict[str, Any]:
        """`status: queued` の行に、今の待ち順をその場で足す（保存はしない）。

        Adds the current wait position to a `status: queued` record without
        storing it — the queue moves, so this is only ever read fresh.
        """
        if record.get("status") != "queued":
            return record
        return {**record, "queue_position": run_queue.position(record["id"])}

    @app.get("/api/runs", dependencies=[guard])
    def run_list() -> dict[str, Any]:
        return {"items": [_with_queue_position(r) for r in runs.list()]}

    @app.get("/api/runs/{run_id}", dependencies=[guard])
    def run_detail(run_id: str) -> Any:
        record = runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such run")
        return _with_queue_position(record)

    @app.get("/api/queue", dependencies=[guard])
    def queue_status() -> dict[str, Any]:
        """今 LLM/テンプレートの実行を使っているものと、待っているものの列。

        The template run currently using the LLM/engine, and the queue of
        runs waiting their turn.
        """
        snap = run_queue.snapshot()

        def describe(run_id: str) -> dict[str, Any]:
            record = runs.get(run_id)
            return {"id": run_id, "template": record.get("template") if record else None}

        return {
            "running": describe(snap["running"]) if snap["running"] else None,
            "waiting": [describe(run_id) for run_id in snap["waiting"]],
        }

    def _do_run(template: Any, params: dict[str, Any], trigger: dict[str, Any],
                role: str, run_id: str | None = None) -> dict[str, Any]:
        """テンプレートを実際に走らせ、実行履歴の1件として記録する。

        `/api/runs`（バックグラウンド実行、`run_id` を先に渡す）と
        `/api/webhook`（バックグラウンド実行、`run_id` は渡さない）の
        両方から呼ばれる共通の実行本体。`run_id` があるときだけ、
        実行中の進捗（今どのステップか）を `runs` に書き続ける——
        画面がその id を `GET /api/runs/{id}` で読みに来る前提のときだけの負担。

        Actually runs a template and records it as one run-history entry.
        Shared by both `/api/runs` (backgrounded, `run_id` given upfront) and
        `/api/webhook` (backgrounded, no `run_id`). Progress is only written
        while running when `run_id` is given — the cost of keeping it live
        only applies when something is actually polling for it.
        """
        started_at = datetime.now(timezone.utc)

        def on_progress(index: int, total: int, step_id: str) -> None:
            runs.add({
                "id": run_id, "template": template.name, "started_by": role,
                "status": "running", "error": None,
                "started_at": started_at.isoformat(), "steps": [],
                "progress": {"index": index, "total": total, "step_id": step_id},
                "total": total,
            })

        ctx: RunContext | None
        try:
            ctx = engine.run(template, params=params, trigger=trigger, run_id=run_id,
                             on_progress=on_progress if run_id else None)
            status = "success"
            error = None
        except StepFailure as exc:
            ctx = exc.context
            status = "failed"
            error = str(exc)
            if ctx is None:
                record: dict[str, Any] = {
                    "id": run_id or secrets.token_hex(6), "template": template.name,
                    "started_by": role,
                    "status": status, "error": error, "steps": [],
                    "started_at": None,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "total": len(template.steps),
                }
                runs.add(record)
                logger.info("run %s: template=%s started_by=%s status=%s",
                           record["id"], template.name, role, status)
                return record
        except Exception as exc:
            # 進捗を追わせている（run_id がある）実行だけ、ここで食い止める。
            # さもなければ画面が「実行中」のまま固まる——何かが起きたと
            # 本物の例外で止める方が、黙って呑むより安全な範囲はここだけ。
            # run_id が無い（webhook）経路は、今までどおり外へ投げる。
            #
            # Only swallowed when something is polling for progress
            # (run_id given) — otherwise the UI would be stuck showing
            # "running" forever. The webhook path (no run_id) still
            # propagates, unchanged.
            if run_id is None:
                raise
            record = {
                "id": run_id, "template": template.name, "started_by": role,
                "status": "failed", "error": str(exc), "steps": [],
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "total": len(template.steps),
            }
            runs.add(record)
            logger.exception("run %s: template=%s started_by=%s crashed",
                            run_id, template.name, role)
            return record

        record = {
            "id": ctx.run_id,
            "template": template.name,
            "started_by": role,
            "status": status,
            "error": error,
            "started_at": ctx.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "total": len(template.steps),
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

        # すぐに応答を返し、実際の実行はワーカー1本の待ち行列に乗せる——
        # LLM を呼ぶ工程が重なって同時に走ることはなく、画面は run_id で
        # `GET /api/runs/{id}` を読みに来て、待ち順→今のステップの順に追える。
        # `GET /api/queue` では、列全体（今動いているものと待っているもの）
        # が見える。
        # Responds immediately and places the run on a single-worker queue:
        # LLM-calling steps from different runs never overlap, and the UI
        # polls `GET /api/runs/{id}` by this run_id to follow it from queue
        # position through to the current step. `GET /api/queue` shows the
        # whole line (what's running now, what's waiting).
        run_id = uuid.uuid4().hex[:12]
        total = len(template.steps)
        queued_record: dict[str, Any] = {
            "id": run_id, "template": template.name, "started_by": role,
            "status": "queued", "error": None,
            "started_at": None, "steps": [], "progress": None, "total": total,
        }
        runs.add(queued_record)

        def job() -> None:
            runs.add({
                "id": run_id, "template": template.name, "started_by": role,
                "status": "running", "error": None,
                "started_at": datetime.now(timezone.utc).isoformat(), "steps": [],
                "progress": {"index": 0, "total": total, "step_id": None}, "total": total,
            })
            _do_run(template, payload.get("params") or {}, payload.get("trigger") or {},
                    role, run_id)

        run_queue.enqueue(run_id, job)
        # 応答そのものを、台帳に置いた「待っている」行と同じにする——
        # 呼び出し側は、ここで status / template / started_by を既に読める。
        # The response body is the same "queued" record just stored, so a
        # caller already has status / template / started_by without a
        # follow-up GET.
        return JSONResponse(status_code=202, content=_with_queue_position(queued_record))

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
        result: dict[str, Any] = {
            "adapters": {
                name: engine.adapters.get(name).health_check()
                for name in engine.adapters.names()
            }
        }
        if _pools:
            # 台帳の接続プールの状況（使用中・待機・待ち・拒否した数）。
            result["ledger_pool"] = [pool.stats() for pool in list(_pools.values())]
        return result

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

    def _pmo_ledger() -> Path:
        if pmo_ledger is None:
            raise HTTPException(status_code=404, detail="PMO Core is not configured")
        return pmo_ledger

    def _side_doc(ledger: Path, name: str) -> str | None:
        """台帳の隣に置かれた文書（ブリーフィングなど）を読む。

        台帳の設定どおりの置き場から読む：PostgreSQL なら、schedule が別のホストで書いた
        ものも同じデータベースにある。台帳がまだ無い（schedule が一度も動いていない）ときは、
        隣のファイルだけを見る（台帳を作らない）。
        """
        if _ledger_present(ledger):
            store = _open_store(ledger, sync=False)
            try:
                return store.side.read_doc(name)
            finally:
                _release(store)
        return FileSide(ledger).read_doc(name)

    def _side_tail(ledger: Path, name: str, limit: int) -> list[str]:
        if _ledger_present(ledger):
            store = _open_store(ledger, sync=False)
            try:
                return store.side.tail(name, limit)
            finally:
                _release(store)
        return FileSide(ledger).tail(name, limit)

    def _ledger_present(ledger: Path) -> bool:
        # PostgreSQL の台帳は、ファイルの有無では分からない（接続できれば有る）。
        # A PostgreSQL ledger cannot be told by a file; if it connects, it is there.
        return ledger_store_factory is not None or TaskEngine.exists(ledger)

    # 台帳の接続プール（aipmo/web/pool.py）。多数が同時にアクセスしても、台帳への接続は
    # `pool_size` を超えない。ledger ごとに 1 つ。
    # Ledger connection pools, one per ledger: never more than `pool_size` connections.
    _pools: dict[str, LedgerPool] = {}
    _pools_lock = threading.Lock()

    def _pool_for(ledger: Path) -> LedgerPool:
        key = str(ledger)
        with _pools_lock:
            pool = _pools.get(key)
            if pool is None:
                pool = _pools[key] = LedgerPool(
                    lambda: TaskEngine(
                        ledger, tenant=tenant or None,
                        store=ledger_store_factory() if ledger_store_factory else None,
                        side_storage=side_storage),
                    size=pool_size, acquire_timeout=pool_timeout, max_idle=pool_idle)
            return pool

    def _release(store: TaskEngine) -> None:
        """借りた台帳を返す。例外の最中なら、まだ使えるか確かめてから戻す。"""
        pool = getattr(store, "_aipmo_pool", None)
        if pool is None:
            store.close()
            return
        pool.release(store, failed=sys.exc_info()[0] is not None)

    def _close_pools() -> None:
        for pool in list(_pools.values()):
            pool.close()

    app.router.on_shutdown.append(_close_pools)

    def _give_learning(store: TaskEngine) -> None:
        """常駐が学習した補正（pmo-learned）を、借りた台帳の採点に渡す。

        画面の台帳は学習を知らない。渡さないと、画面で提案を承認するなど「再採点を伴う書き込み」のたびに、
        学習した補正が抜けた点数で全タスクが採点し直され、次の周まで順位が崩れる。
        The ledger a request borrows knows nothing of what the resident process learned. Without it any
        write that rescores (approving a proposal) re-scores every task without the learned corrections.
        """
        try:
            doc = json.loads(store.side.read_doc(LEARNED) or "{}")
        except (OSError, ValueError):
            return
        if isinstance(doc, dict) and doc:
            store.label_bonus = dict(doc.get("label_bonus") or {})
            store.priority_delta = dict(doc.get("priority_delta") or {})
            store.pace = dict(doc.get("pace") or {})

    def _open_store(ledger: Path, sync: bool = True) -> TaskEngine:
        """台帳を借りる。`sync=False` は、ブリーフィングや判断ログだけを読むとき（台帳の読み直しを省く）。"""
        pool = _pool_for(ledger)
        try:
            store = pool.acquire(sync=sync)
            store._aipmo_pool = pool                    # type: ignore[attr-defined]
            _give_learning(store)
            return store
        except PoolExhausted as exc:
            # 全部使用中で、待っても空かなかった。固まらずに断り、少し後の再試行を促す。
            # Everything is in use and nothing freed up: refuse promptly and ask for a retry.
            logger.warning("ledger pool exhausted: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc),
                                headers={"Retry-After": "2"}) from exc
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
    def pmo_view(request: Request, project: str | None = None, role: str = guard) -> dict[str, Any]:
        lang = _lang_for(request)
        ledger = _pmo_ledger()
        present = _ledger_present(ledger)
        briefing_text: str | None = None if present else FileSide(ledger).read_doc(BRIEFING)
        if not present and briefing_text is None:
            raise HTTPException(status_code=404, detail="no PMO data yet")
        allowed = _allowed_projects(role, project)
        confined = role == "viewer" and scoped_projects is not None

        briefing = None
        age = None

        active: list[Any] = []
        pending: list[Any] = []
        names: list[str] = []
        filing_view: dict[str, Any] | None = None
        review_view: list[dict[str, Any]] | None = None
        reasons_by_task: dict[str, list[str]] | None = None
        if present:
            store = _open_store(ledger)                 # 1 回の借り出しで、ブリーフィングも台帳も読む
            try:
                briefing_text = store.side.read_doc(BRIEFING)
                if not confined and any(m.is_agent for m in (members or [])):
                    # 役割AIの成果のレビュー待ち。台帳から今の状態で出す。
                    # Results awaiting a human's review, live from the ledger.
                    review_view = [
                        r for r in PmoCore(task_engine=store, members=members or []
                                           ).reviews_pending(store.ranked(projects=allowed))
                        if allowed is None or (r.get("project") or "").lower() in allowed]
                if filing is not None and not confined:
                    # 起票待ちは台帳から今の状態で出す(押した直後に消えるように)。
                    # Live from the ledger, so a filed task leaves the list at once.
                    waiting = [t for t in store.tasks.values() if eligible(t, filing)
                               and (allowed is None or t.project.lower() in allowed)]
                    filing_view = {
                        "tracker": filing.tracker, "auto": list(filing.auto),
                        "can_file": engine.adapters.has(filing.tracker),
                        "pending": [{"id": t.id, "title": t.title, "project": t.project,
                                     "origin": t.origin, "priority": t.priority,
                                     "due_date": t.due_date,
                                     "state": filing_state(t).get("state") or "pending",
                                     "error": filing_state(t).get("error")}
                                    for t in sorted(waiting, key=lambda t: t.first_seen)]}
                active = store.ranked(projects=allowed)
                if not is_japanese(lang):             # 点数の内訳は、構造（parts）から選んだ言語で
                    learned = _learned_of(store)
                    reasons_by_task = {
                        t.id: [f"{p['text']} {p['points']:+d}" for p in _parts_of(store, t, learned, lang)]
                        for t in active[:50]}
                pending = [t for t in store.proposals()
                           if allowed is None or t.project.lower() in allowed]
                names = [n for n in store.projects()
                         if not confined or scoped_projects is None
                         or n.lower() in scoped_projects]
            finally:
                _release(store)

        try:
            briefing = json.loads(briefing_text or "")
            generated = datetime.fromisoformat(briefing["generated_at"])
            age = int((datetime.now(timezone.utc) - generated).total_seconds())
        except (OSError, ValueError, KeyError):
            briefing = None

        if briefing is not None and allowed is not None:
            briefing = scope_briefing(briefing, active, allowed, redact_org=confined)
        if briefing is not None:
            briefing = localize_briefing(lang, briefing, reasons_by_task)

        tasks = [
            {"id": t.id, "key": t.key, "title": task_title(lang, t.title, t.payload), "score": t.score,
             "project": t.project, "tracker": tracker_of(t), "origin": t.origin,
             "dispatches": t.dispatches[-3:],
             "external_id": t.external_id or (t.key if tracker_of(t) == "jira" else None),
             "assignee": t.assignee, "suggested_assignee": t.suggested_assignee,
             "suggestion_reason": suggestion_reason(lang, t.suggestion_reason, t.payload),
             "due_date": t.due_date, "priority": t.priority, "status": t.status, "blocked": t.blocked,
             "reasons": (reasons_by_task or {}).get(t.id, t.reasons), "templates": t.templates}
            for t in active[:50]
        ]
        proposals = [{"id": t.id, "title": t.title, "project": t.project,
                      "priority": t.priority, "due_date": t.due_date,
                      "generated_from": t.generated_from} for t in pending]
        return {"briefing": briefing, "briefing_age_seconds": age, "tasks": tasks,
                "proposals": proposals, "projects": names, "scoped": confined,
                "filing": filing_view, "agent_review": review_view}

    @app.get("/api/inbox", dependencies=[guard])
    def inbox_view(request: Request, project: str | None = None, role: str = guard) -> dict[str, Any]:
        """人の判断を待っているものを、種類をまたいで 1 つの一覧にする（読むだけ）。

        決める操作は、各項目の `actions` が指す既存の API（権限・確認はこれまでどおり）。
        viewer には actions を返さない。プロジェクトを限定された viewer には、組織全体の項目を出さない。
        """
        ledger = _pmo_ledger()
        allowed = _allowed_projects(role, project)
        confined = role == "viewer" and scoped_projects is not None
        empty = {"items": [], "total": 0, "by_kind": {}, "can_act": role != "viewer",
                 "generated_at": datetime.now(timezone.utc).isoformat()}
        if not _ledger_present(ledger):
            return empty
        replans: list[dict[str, Any]] = []
        if allowed is None and not confined and engine.adapters.has("postgres"):
            try:
                rows = engine.adapters.get("postgres").query(  # type: ignore[attr-defined]
                    "pending_wbs_proposals", {"tenant": tenant})["rows"]
                replans = [dict(r) for r in rows]
            except Exception:                            # noqa: BLE001 — 再計画案が読めなくても、他は出す
                logger.warning("inbox: WBS 再計画案を読めません / cannot read replan proposals",
                               exc_info=True)
        store = _open_store(ledger)
        try:
            return build_inbox(
                store, members=members or [], filing=filing, can_act=role != "viewer",
                allowed=allowed, confined=confined, writable=writable_trackers(engine.adapters),
                can_file=bool(filing is not None and engine.adapters.has(filing.tracker)),
                replans=replans, lang=_lang_for(request))
        finally:
            _release(store)

    @app.get("/api/wbs", dependencies=[guard])
    def wbs_screen(request: Request, role: str = guard) -> dict[str, Any]:
        """WBS の木・進捗・予測・ずれ・次に着手できる作業（読むだけ）。

        WBS ファイルは組織全体のものなので、プロジェクトを限定された viewer には出さない。
        The WBS tree, progress, forecast, drift and what can start next. Read-only; a viewer confined
        to some projects does not see it (the file is org-wide).
        """
        if wbs_view is None:
            raise HTTPException(status_code=404, detail="WBS is not configured")
        if role == "viewer" and scoped_projects is not None:
            raise HTTPException(status_code=403, detail="WBS is not available to a scoped viewer")
        file, root = wbs_view
        try:
            loaded, problems = load_wbs(file)
        except WbsError as exc:
            raise HTTPException(status_code=500, detail=f"cannot read the WBS: {exc}") from exc
        result = wbs_analysis(loaded, root, problems=problems, lang=_lang_for(request))
        for key in ("tasks", "items", "summary_text"):       # 画面に要らない（重い）もの
            result.pop(key, None)
        result["can_edit"] = role != "viewer"
        return result

    @app.post("/api/wbs/edit")
    def wbs_edit(request: Request, payload: dict[str, Any], role: str = operator_guard) -> dict[str, Any]:
        """WBS ファイルを画面から直す（operator のみ）。固定の形の変更だけを受け付ける。

        `apply` が偽（既定）なら、反映後の差分を返すだけで何も書かない。真なら、同じ検証のうえで書く。
        検証・書式の保持・読んだ後に書き換えられていたら書かない、は再計画案の承認と同じ仕組み
        （aipmo/wbs_edit.py）。反映は判断ログに残る。
        Edits the WBS file (operator only), accepting only the fixed change shapes. Without `apply`
        it returns the diff and writes nothing; with it, the same validation then the write. It is the
        replan-approval machinery, so formatting survives and a changed file is never overwritten.
        """
        if wbs_view is None:
            raise HTTPException(status_code=404, detail="WBS is not configured")
        file, root = wbs_view
        try:
            plan = plan_changes(file, root, payload.get("changes"))
        except WbsEditError as exc:
            raise HTTPException(status_code=422, detail={"message": str(exc), "problems": exc.problems}) from exc
        out: dict[str, Any] = {"applied": False, "changed": plan.changed, "diff": plan.diff,
                               "report": plan.report}
        if not payload.get("apply"):
            return out
        try:
            write_plan(plan)
        except WbsEditError as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "problems": exc.problems}) from exc
        out["applied"] = True
        ledger = pmo_ledger
        if ledger is not None and _ledger_present(ledger):
            store = _open_store(ledger, sync=False)
            try:
                store.side.append(DECISIONS, json.dumps(
                    {"at": datetime.now(timezone.utc).isoformat(), "kind": "wbs_edited",
                     "file": str(file), "by": role, "changes": plan.report}, ensure_ascii=False))
            finally:
                _release(store)
        logger.info("wbs edited by %s from %s (%d change(s))", role, _client_ip(request), len(plan.report))
        return out

    @app.get("/api/pulse", dependencies=[guard])
    def pulse() -> dict[str, Any]:
        """画面が「変わったか」を安く確かめるための値（読むだけ）。ブリーフィングの時刻と、判断ログの長さ。

        A cheap change marker for the page's visible-tab refresh: the briefing time and the decision-log size.
        """
        ledger = pmo_ledger
        if ledger is None or not _ledger_present(ledger):
            return {"briefing_at": None, "log": ""}
        store = _open_store(ledger, sync=False)
        try:
            text = store.side.read_doc(BRIEFING)
            last = store.side.tail(DECISIONS, 1)
            log = hashlib.sha1((last[-1] if last else "").encode("utf-8")).hexdigest()[:12]
        finally:
            _release(store)
        try:
            at = json.loads(text or "{}").get("generated_at")
        except ValueError:
            at = None
        return {"briefing_at": at, "log": log}

    @app.get("/api/pmo/decisions", dependencies=[guard])
    def pmo_decisions(limit: int = 50, project: str | None = None,
                      role: str = guard) -> dict[str, Any]:
        ledger = _pmo_ledger()
        limit = max(1, min(limit, 200))
        allowed = _allowed_projects(role, project)
        try:
            lines = _side_tail(ledger, DECISIONS, 2000 if allowed is not None else limit)
        except HTTPException:
            raise                                        # 台帳が使えない・混んでいる(503)は、そのまま伝える
        except Exception:                                # noqa: BLE001
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
                    _release(store)
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

    def _learned_of(store: Any) -> dict[str, Any]:
        """常駐側が学習した補正（pmo-learned）。画面の台帳には載っていないので、ここで読む。"""
        try:
            doc = json.loads(store.side.read_doc(LEARNED) or "{}")
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _parts_of(store: Any, task: Any, learned: dict[str, Any], lang: str) -> list[dict[str, Any]]:
        """点数の内訳（項目ごと）。合計は台帳の点数に必ず一致させる。

        学習のモデルが古い・読めないなどで差が出たら、隠さず「その他の補正」として出す。
        The per-item score; it always sums to the ledger's score, and any gap (a stale learned model)
        is shown as its own item rather than hidden.
        """
        if task.done:
            return []
        points, _, parts = score_breakdown(
            task, store.now().date(), learned.get("label_bonus") or {},
            learned.get("priority_delta") or {}, learned.get("pace") or {})
        gap = task.score - points
        if gap:
            parts.append({"kind": "other", "points": gap, "key": "s_other",
                          "params": {"gap": f"{gap:+d}"}})
        for part in parts:                              # 画面の言語の文章にする
            params = dict(part.get("params") or {})
            if part.get("key") == "s_priority" or part.get("key") == "s_priority_shift":
                params["priority"] = params.get("priority") or translate(lang, "s_unset")
            part["text"] = translate(lang, part["key"], **params)
        return parts

    def _task_row(t: Any, parts: list[dict[str, Any]], lang: str) -> dict[str, Any]:
        return {"id": t.id, "key": t.key, "title": task_title(lang, t.title, t.payload),
                "score": t.score, "parts": parts,
                "project": t.project, "tracker": tracker_of(t), "origin": t.origin,
                "assignee": t.assignee, "suggested_assignee": t.suggested_assignee,
                "due_date": t.due_date, "priority": t.priority, "status": t.status,
                "blocked": t.blocked, "done": t.done, "labels": t.labels, "effort": t.effort,
                "proposed": t.proposed}

    @app.get("/api/tasks", dependencies=[guard])
    def tasks_list(request: Request, project: str | None = None, assignee: str | None = None,
                   q: str | None = None, state: str = "active", sort: str = "score",
                   limit: int = 50, offset: int = 0, role: str = guard) -> dict[str, Any]:
        """タスクの一覧（読むだけ）。絞り込み・並び・ページつき。点数は項目ごとの内訳つき。

        state: active（未完了。既定）| done | all。sort: score | due | priority。
        The task list, read-only: filters, sort, paging, and the score as per-item parts.
        """
        lang = _lang_for(request)
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        if state not in ("active", "done", "all") or sort not in ("score", "due", "priority"):
            raise HTTPException(status_code=422, detail="bad state or sort")
        allowed = _allowed_projects(role, project)
        limit, offset = max(1, min(limit, 200)), max(0, offset)
        store = _open_store(ledger)
        try:
            pool = [t for t in store.tasks.values()
                    if not t.origin == "judgment"
                    and (allowed is None or t.project.lower() in allowed)]
            names = sorted({t.project for t in pool if t.project}, key=str.lower)
            people = sorted({t.assignee for t in pool if t.assignee}, key=str.lower)
            if state == "active":
                pool = [t for t in pool if not t.done and not t.proposed]
            elif state == "done":
                pool = [t for t in pool if t.done]
            if assignee == "-":
                pool = [t for t in pool if not t.assignee]
            elif assignee:
                pool = [t for t in pool if (t.assignee or "").lower() == assignee.lower()]
            if q:
                needle = q.strip().lower()
                pool = [t for t in pool if needle in t.title.lower()
                        or needle in (t.key or "").lower() or needle in t.id.lower()]
            if sort == "due":
                pool.sort(key=lambda t: (t.due_date or "9999-99-99", -t.score, t.id))
            elif sort == "priority":
                pool.sort(key=lambda t: (-_priority_rank(t.priority), -t.score, t.id))
            else:
                pool.sort(key=lambda t: (-t.score, t.due_date or "9999-99-99", t.id))
            page = pool[offset:offset + limit]
            learned = _learned_of(store)
            rows = [_task_row(t, _parts_of(store, t, learned, lang), lang) for t in page]
            return {"items": rows, "total": len(pool), "limit": limit, "offset": offset,
                    "projects": names, "assignees": people}
        finally:
            _release(store)

    @app.get("/api/tasks/{task_id}", dependencies=[guard])
    def task_detail(request: Request, task_id: str, role: str = guard) -> dict[str, Any]:
        """1 件の詳細（読むだけ）: 点数の内訳・由来・役割AIの実行・関係する警告と判断の履歴。"""
        lang = _lang_for(request)
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        allowed = _allowed_projects(role, None)
        store = _open_store(ledger)
        try:
            task = store.tasks.get(task_id)
            if task is None or task.origin == "judgment" or (
                    allowed is not None and task.project.lower() not in allowed):
                raise HTTPException(status_code=404, detail="no such task")
            parts = _parts_of(store, task, _learned_of(store), lang)
            briefing_text = store.side.read_doc(BRIEFING)
            history_lines = store.side.tail(DECISIONS, 2000)
            detail = _task_row(task, parts, lang)
            detail.update({
                "sources": task.sources[-10:], "dispatches": task.dispatches,
                "generated_from": task.generated_from, "first_seen": task.first_seen,
                "last_seen": task.last_seen, "started_at": task.started_at,
                "status_since": task.status_since, "blocked_since": task.blocked_since,
                "external_id": task.external_id or (task.key if tracker_of(task) == "jira" else None),
                "suggestion_reason": suggestion_reason(lang, task.suggestion_reason, task.payload),
                "filing": (task.payload or {}).get("filing") or None})
        finally:
            _release(store)
        alerts: list[Any] = []
        try:
            alerts = [localize_alert_text(lang, a)
                      for a in json.loads(briefing_text or "{}").get("alerts", [])
                      if a.get("task") == task_id]
        except ValueError:
            pass
        history = []
        for line in reversed(history_lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("task") == task_id:
                history.append(entry)
            if len(history) >= 20:
                break
        detail.update({"alerts": alerts, "history": history})
        return detail

    _health_cache: dict[str, tuple[float, bool]] = {}

    def _adapter_healthy(name: str) -> bool:
        """アダプタの疎通。外部サービスへ問い合わせることがあるので、30 秒は覚えておく。"""
        now = time.monotonic()
        hit = _health_cache.get(name)
        if hit is not None and now - hit[0] < 30:
            return hit[1]
        try:
            ok = bool(engine.adapters.get(name).health_check())
        except Exception:                                # noqa: BLE001 — 疎通できないだけで、画面は落とさない
            ok = False
        _health_cache[name] = (now, ok)
        return ok

    @app.get("/api/integrations", dependencies=[guard])
    def integrations(role: str = guard) -> dict[str, Any]:
        """連携の状態（読むだけ）: アダプタの疎通と書き戻せるか、収集の直近の結果、WBS ファイルの検査、
        起票の設定、メンバーのアカウントの対応表。

        担当者の名前からの引き当て（Plane・OpenProject）は外部サービスへの問い合わせなので、ここでは
        しない。`aipmo members` で事前に確かめる。組織全体の情報なので、範囲を限られた viewer には出さない。
        The state of the integrations, read-only. Name lookups need the tracker, so they are not done
        here (`aipmo members` does them). Org-wide, hence not for a scoped viewer.
        """
        if role == "viewer" and scoped_projects is not None:
            raise HTTPException(status_code=403, detail="not available to a scoped viewer")
        writable = writable_trackers(engine.adapters)
        adapters = [{"name": name, "healthy": _adapter_healthy(name), "writeback": name in writable}
                    for name in sorted(engine.adapters.names())]
        briefing: dict[str, Any] = {}
        ledger = pmo_ledger
        if ledger is not None:
            try:
                briefing = json.loads(_side_doc(ledger, BRIEFING) or "{}")
            except (OSError, ValueError, HTTPException):
                briefing = {}
        filing_info = None
        if filing is not None:
            pending = (briefing.get("filing") or {}).get("pending") or []
            filing_info = {"tracker": filing.tracker, "auto": list(filing.auto),
                           "can_file": engine.adapters.has(filing.tracker), "pending": len(pending)}
        accounts = [{"member": m.name, "is_agent": m.is_agent, "accounts": dict(m.accounts)}
                    for m in (members or [])]
        return {"adapters": adapters, "collection": briefing.get("collection"),
                "wbs": briefing.get("wbs_drift"), "filing": filing_info,
                "accounts": accounts, "lookup_assignees": lookup_assignees,
                "generated_at": briefing.get("generated_at")}

    _last_probe: list[float] = [0.0]

    @app.post("/api/integrations/check")
    def integrations_check(request: Request, payload: dict[str, Any],
                           role: str = operator_guard) -> dict[str, Any]:
        """課題管理ツールとの接続を、読み取りだけで診断する（operator のみ。外部サービスへ問い合わせる）。

        疎通 → 1 件の読み取り → 担当候補の一覧、の順に行い、止まった所と原因の分類（認証・見つからない・
        タイムアウト…）を返す。書き込みは呼ばない。外部への負荷を抑えるため、5 秒に 1 回まで。
        Read-only diagnosis of the tracker connections (operator only: it calls the outside). Throttled.
        """
        name = str(payload.get("name") or "") or None
        if name and name not in set(engine.adapters.names()):
            raise HTTPException(status_code=404, detail="no such adapter")
        now = time.monotonic()
        if now - _last_probe[0] < 5:
            raise HTTPException(status_code=429, detail="wait a few seconds", headers={"Retry-After": "5"})
        _last_probe[0] = now
        logger.info("integrations check (%s) by %s from %s", name or "all", role, _client_ip(request))
        return {"reports": probe_all(engine.adapters, name)}

    @app.get("/api/learning/members", dependencies=[guard])
    def learning_members(role: str = guard) -> dict[str, Any]:
        """メンバーごとの実績と、学習で補正した上限・ペース（読むだけ）。補正の根拠を見せるための API。

        実績は台帳の完了の記録。補正の係数は常駐が学習した pmo-learned。組織全体の数字なので、
        プロジェクトを限定された viewer には出さない。
        Per-member track record and the learned corrections, read-only, so the screen can show why a
        capacity was adjusted. Org-wide, hence not for a scoped viewer.
        """
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        if role == "viewer" and scoped_projects is not None:
            raise HTTPException(status_code=403, detail="not available to a scoped viewer")
        store = _open_store(ledger)
        try:
            outcomes = [dict(o) for o in store.outcomes]
            load: dict[str, int] = {}
            for t in store.tasks.values():
                if not t.done and not t.proposed and t.origin != "judgment" and t.assignee:
                    load[t.assignee.lower()] = load.get(t.assignee.lower(), 0) + 1
            learned = _learned_of(store)
        finally:
            _release(store)
        factors = learned.get("member_factor") or {}
        pace = learned.get("pace") or {}
        by_name: dict[str, list[dict[str, Any]]] = {}
        for o in outcomes:
            if o.get("assignee"):
                by_name.setdefault(str(o["assignee"]).lower(), []).append(o)
        names = {m.name.lower(): m for m in (members or [])}
        rows = []
        for key in sorted(set(names) | set(by_name), key=lambda k: (names[k].name if k in names else k)):
            member = names.get(key)
            done = by_name.get(key, [])
            dated = [o for o in done if o.get("late_days") is not None]
            factor = factors.get(key, 1.0)
            capacity = member.capacity if member else None
            rows.append({
                "name": member.name if member else key,
                "is_agent": bool(member and member.is_agent),
                "capacity": capacity, "factor": factor,
                "effective_capacity": (max(1, round(capacity * factor)) if capacity else None),
                "load": load.get(key, 0),
                "samples": len(dated),
                "on_time_rate": (round(sum(1 for o in dated if o["late_days"] <= 0) / len(dated), 3)
                                 if dated else None),
                "avg_late_days": (round(sum(o["late_days"] for o in dated) / len(dated), 1)
                                  if dated else None),
                "pace": (pace.get("members") or {}).get(key),
                "recent": [{k: o.get(k) for k in ("task", "late_days", "effort", "duration_days",
                                                 "priority", "labels", "done_at")}
                           for o in sorted(done, key=lambda o: str(o.get("done_at")), reverse=True)[:5]]})
        evidence = learned.get("evidence") or {}
        return {
            "members": rows, "samples": learned.get("samples", len(outcomes)),
            "baseline_late_rate": learned.get("baseline_late_rate"),
            "labels": [{"label": k, "bonus": v, **(evidence.get(f"label:{k}") or {})}
                       for k, v in sorted((learned.get("label_bonus") or {}).items())],
            "priorities": [{"priority": k, "delta": v, **(evidence.get(f"priority:{k}") or {})}
                           for k, v in sorted((learned.get("priority_delta") or {}).items())],
            "pace": {"team": pace.get("team"), "reliable": bool(pace.get("reliable")),
                     "median_error": pace.get("median_error"), "max_error": pace.get("max_error"),
                     "samples": pace.get("samples", 0)},
            "generated_at": learned.get("generated_at")}

    @app.get("/api/agents/reviews", dependencies=[guard])
    def agent_reviews(limit: int = 50, project: str | None = None, role: str = guard) -> dict[str, Any]:
        """役割AIの成果を人が確かめた履歴（新しい順。読むだけ）と、AI ごとの認めた／差し戻した件数。

        判断ログの `agent_reviewed` から作る。プロジェクトを限定された viewer には、範囲内のタスクだけ。
        The history of human reviews of role-AI results, newest first, with a per-agent tally, from the
        decision log. A scoped viewer sees only tasks in scope.
        """
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        limit = max(1, min(limit, 200))
        allowed = _allowed_projects(role, project)
        store = _open_store(ledger)
        try:
            lines = store.side.tail(DECISIONS, 5000)
            titles = {t.id: (t.title, t.project) for t in store.tasks.values()}
        finally:
            _release(store)
        latest: dict[tuple[Any, Any], dict[str, Any]] = {}
        history: list[dict[str, Any]] = []
        for line in lines:
            if '"agent_reviewed"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") != "agent_reviewed":
                continue
            title, task_project = titles.get(entry.get("task"), (None, ""))
            if allowed is not None and (task_project or "").lower() not in allowed:
                continue
            entry["title"], entry["project"] = title, task_project
            latest[(entry.get("task"), entry.get("dispatch"))] = entry
            history.append(entry)
        tally: dict[str, dict[str, int]] = {}
        for entry in latest.values():                 # 同じ実行を何度か確かめたら、最後の判断だけ数える
            counts = tally.setdefault(str(entry.get("agent", "")), {"accepted": 0, "rejected": 0})
            if entry.get("decision") in counts:
                counts[entry["decision"]] += 1
        history.reverse()
        return {"items": history[:limit], "total": len(history), "tally": tally}

    @app.get("/api/judgment/control", dependencies=[guard])
    def judgment_control_state() -> dict[str, Any]:
        """止める・戻すの依頼の現在値（読むだけ）。常駐は次の周でこれを読むので、画面は依頼済みかどうかを出す。"""
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        store = _open_store(ledger, sync=False)
        try:
            control = read_control(store.side)
        finally:
            _release(store)
        return {"control": control,
                "autonomy": {"config": dict(judgment.autonomy) if judgment else {},
                             "override": control.get("autonomy_override") or {},
                             "can_raise": bool(judgment and judgment.ui_can_raise)}}

    @app.post("/api/judgment/autonomy")
    def judgment_autonomy(request: Request, payload: dict[str, Any],
                          role: str = operator_guard) -> dict[str, Any]:
        """対処ごとの自律度（off / propose / auto）を変える依頼（operator のみ）。

        設定ファイルの値が基準で、画面からの変更は「上書き」として制御の文書に残る（基準に戻せば消える）。
        常駐が次の周で読む。基準より上げられるのは、設定で `ui_can_raise: true` のときだけ
        （既定は、下げる・止めるだけ）。変更は判断ログに残る。
        Change one remedy's autonomy as an override over the config value (operator only). Raising above
        the config needs `ui_can_raise: true`; lowering is always allowed. Audited in the decision log.
        """
        if judgment is None:
            raise HTTPException(status_code=404, detail="judgment is not configured")
        remedy, level = str(payload.get("remedy") or ""), str(payload.get("level") or "")
        if remedy not in REMEDIES or level not in AUTONOMY:
            raise HTTPException(status_code=422, detail=f"remedy in {REMEDIES}, level in {AUTONOMY}")
        base = judgment.autonomy[remedy]
        if RANK[level] > RANK[base] and not judgment.ui_can_raise:
            raise HTTPException(status_code=403, detail=(
                f"cannot raise {remedy} above the configured {base}: set judgment.ui_can_raise in the config"))
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        stamp = datetime.now(timezone.utc).isoformat()
        store = _open_store(ledger, sync=False)
        try:
            overrides = dict(read_control(store.side).get("autonomy_override") or {})
            before = overrides.get(remedy, base)
            if level == base:
                overrides.pop(remedy, None)
            else:
                overrides[remedy] = level
            state = write_control(store.side, autonomy_override=overrides)
            store.side.append(DECISIONS, json.dumps(
                {"at": stamp, "kind": "judgment_autonomy", "remedy": remedy, "from": before,
                 "to": level, "by": role}, ensure_ascii=False))
        finally:
            _release(store)
        logger.info("judgment autonomy %s %s->%s by %s from %s", remedy, before, level, role,
                    _client_ip(request))
        return {"remedy": remedy, "level": level, "control": state}

    @app.post("/api/judgment/{action}")
    def judgment_control(action: str, request: Request, role: str = operator_guard) -> dict[str, Any]:
        """自律的な判断を止める／再開する／遮断器を戻す（operator のみ）。

        `aipmo judgment pause|resume|reset` と同じ制御の文書に書く。常駐が次の周で読む。
        画面から変えられるのはこの 3 つだけで、自律度（off/propose/auto）は設定ファイルのまま。
        記録は判断ログにも残す。
        Pause, resume or reset the circuit breaker (operator only): the same control document the CLI
        writes, read by the resident process on its next cycle. Autonomy levels stay in the config file.
        """
        if action not in ("pause", "resume", "reset"):
            raise HTTPException(status_code=404, detail="unknown action")
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        stamp = datetime.now(timezone.utc).isoformat()
        changes: dict[str, dict[str, Any]] = {"pause": {"paused": True, "paused_at": stamp},
                                              "resume": {"paused": False, "resumed_at": stamp},
                                              "reset": {"reset_at": stamp}}
        change = changes[action]
        store = _open_store(ledger, sync=False)
        try:
            state = write_control(store.side, **change)
            store.side.append(DECISIONS, json.dumps(
                {"at": stamp, "kind": "judgment_control", "action": action, "by": role},
                ensure_ascii=False))
        finally:
            _release(store)
        logger.info("judgment %s by %s from %s", action, role, _client_ip(request))
        return {"action": action, "control": state}

    # -- 自己学習サイクルの「RAG を信用する」設定 / self-learning cycle's trust-RAG toggle --
    #
    # `templates/examples/self_learning_cycle.yaml` が提出した候補だけを対象にした、
    # オプトインの自動承認（aipmo/self_learning.py）。既定は無効——人が
    # aipmo knowledge / Web の「ナレッジ」で決める。運用者がここで明示的に
    # 有効にしたときだけ、このサイクル専用に自動化される。いつでも無効化できる。
    #
    # Opt-in auto-approval (aipmo/self_learning.py) for candidates submitted
    # specifically by `templates/examples/self_learning_cycle.yaml`. Off by
    # default — a human decides via `aipmo knowledge` / the web "Knowledge"
    # screen. Only turned on when an operator explicitly flips this switch,
    # and only for that one cycle; can be turned off again at any time.

    @app.get("/api/learning/control", dependencies=[guard])
    def learning_control_state() -> dict[str, Any]:
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        store = _open_store(ledger, sync=False)
        try:
            control = read_control(store.side)
        finally:
            _release(store)
        return {"trust_rag": bool(control.get("learning_trust_rag"))}

    @app.post("/api/learning/trust-rag")
    def set_learning_trust_rag(request: Request, payload: dict[str, Any],
                               role: str = operator_guard) -> dict[str, Any]:
        enabled = bool(payload.get("enabled"))
        ledger = _pmo_ledger()
        if not _ledger_present(ledger):
            raise HTTPException(status_code=404, detail="no PMO data yet")
        stamp = datetime.now(timezone.utc).isoformat()
        store = _open_store(ledger, sync=False)
        try:
            key = "learning_trust_rag_enabled_at" if enabled else "learning_trust_rag_disabled_at"
            state = write_control(store.side, learning_trust_rag=enabled, **{key: stamp})
            store.side.append(DECISIONS, json.dumps(
                {"at": stamp, "kind": "learning_trust_rag", "enabled": enabled, "by": role},
                ensure_ascii=False))
        finally:
            _release(store)
        logger.info("learning trust_rag=%s by %s from %s", enabled, role, _client_ip(request))
        return {"trust_rag": enabled, "control": state}

    @app.post("/api/pmo/proposals/decide")
    def pmo_decide(request: Request, payload: dict[str, Any],
                   role: str = operator_guard) -> dict[str, Any]:
        """PMO Core が起こした対応タスクの提案を、承認する／却下する(operator のみ)。"""
        ledger = _pmo_ledger()
        ref = str(payload.get("ref") or "")
        decision = str(payload.get("decision") or "")
        if not ref or decision not in ("approve", "reject"):
            raise HTTPException(status_code=422,
                                detail="ref and decision (approve|reject) are required")
        store = _open_store(ledger)
        try:
            task = PmoCore(task_engine=store).decide_proposal(ref, decision == "approve")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            _release(store)
        logger.info("pmo proposal %s: %s by %s from %s", decision, task.id, role,
                    _client_ip(request))
        return {"id": task.id, "decision": decision}

    @app.post("/api/pmo/agents/review")
    def pmo_agent_review(request: Request, payload: dict[str, Any],
                         role: str = operator_guard) -> dict[str, Any]:
        """役割AIの成果を人が確かめた記録を残す(operator のみ)。

        decision は accept か reject(reject には note が要る)。役割AIはレビューできない。
        """
        ledger = _pmo_ledger()
        ref = str(payload.get("ref") or "")
        decision = str(payload.get("decision") or "")
        if not ref or decision not in ("accept", "reject"):
            raise HTTPException(status_code=422,
                                detail="ref and decision (accept|reject) are required")
        reviewer = str(payload.get("by") or "").strip() or "web operator"
        store = _open_store(ledger)
        try:
            review = PmoCore(task_engine=store, members=members or []).review_dispatch(
                ref, "accepted" if decision == "accept" else "rejected", reviewer,
                str(payload.get("note") or ""), str(payload.get("dispatch") or "") or None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            _release(store)
        logger.info("pmo agent review %s: %s by %s from %s", decision, review["task"], role,
                    _client_ip(request))
        return review

    @app.post("/api/pmo/filing")
    def pmo_filing(request: Request, payload: dict[str, Any],
                   role: str = operator_guard) -> dict[str, Any]:
        """承認したタスクを課題管理ツールに起票する／見送る(operator のみ)。

        課題を作るのは外の世界を変える操作なので、閲覧者には許さない。
        """
        ledger = _pmo_ledger()
        ref = str(payload.get("ref") or "")
        decision = str(payload.get("decision") or "file")
        if not ref or decision not in ("file", "skip"):
            raise HTTPException(status_code=422,
                                detail="ref and decision (file|skip) are required")
        if filing is None:
            raise HTTPException(status_code=404, detail="filing is not configured")
        store = _open_store(ledger)
        core = PmoCore(task_engine=store, filing=filing)
        try:
            if decision == "skip":
                task = core.decline_filing(ref)
            else:
                task = core.file_task(ref, file=make_filer(engine.adapters, members or [], filing,
                                                       lookup=lookup_assignees))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FilingError as exc:
            status = {"adapter": 503, "target": 422}.get(exc.kind, 502)
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        finally:
            _release(store)
        key = filing_state(task).get("key")
        logger.info("pmo filing %s: %s -> %s by %s from %s", decision, task.id, key, role,
                    _client_ip(request))
        return {"id": task.id, "decision": decision, "key": key}

    @app.post("/api/pmo/assignments/accept")
    def pmo_accept(request: Request, payload: dict[str, Any],
                   role: str = operator_guard) -> dict[str, Any]:
        ledger = _pmo_ledger()
        ref = str(payload.get("ref") or "")
        if not ref:
            raise HTTPException(status_code=422, detail="ref is required")

        # そのタスクのトラッカーにも書くか。`jira` は従来の名前（同じ意味）。
        # Also write to the task's own tracker; `jira` is the old name.
        write = (make_writer(engine.adapters, members or [], lookup=lookup_assignees)
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
            _release(store)
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

    @app.get("/api/wbs-proposals/{proposal_id}/preview", dependencies=[guard])
    def preview_wbs_proposal(proposal_id: str) -> dict[str, Any]:
        """承認したらファイルがどう変わるか（書かない）。反映できない理由もそのまま返す。

        What approving would do to the WBS file, without writing anything; when it cannot apply,
        the reason is returned rather than an error.
        """
        pg = _postgres_or_503()
        result = pg.query("get_wbs_proposal", {"tenant": tenant, "id": proposal_id})
        if not result["rows"]:
            raise HTTPException(status_code=404, detail="no such proposal")
        row = dict(result["rows"][0])
        out: dict[str, Any] = {"id": proposal_id, "status": row.get("status"), "applicable": False,
                               "reason": None, "report": [], "diff": "", "changed": False,
                               "already_applied": False, "problems": []}
        if wbs_target is None:
            out["reason"] = "no_target"
            return out
        try:
            plan = plan_wbs_proposal(row, wbs_target)
        except ProposalError as exc:
            out.update(reason=exc.kind, problems=list(exc.problems), message=str(exc))
            return out
        if plan is None:
            out["reason"] = "free_form"
            return out
        out.update(applicable=True, report=plan.report, diff=plan.diff, changed=plan.changed,
                   already_applied=applied_before(wbs_target.decisions, proposal_id))
        return out

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
        if status == "approved" and wbs_target is not None:
            # WBS ファイルへの反映先が設定されているときは、承認と反映をひと続きで行う
            # （aipmo/wbs_proposals.py）。反映できない提案は、承認待ちのまま理由を返す。
            # With a target file, approving also applies; an unapplicable proposal stays
            # pending and the reasons are returned.
            try:
                outcome = approve_and_apply(pg, tenant, proposal_id, role, note, wbs_target)
            except ProposalError as exc:
                code = {"not_found": 404, "not_pending": 409, "invalid": 422}.get(exc.kind, 409)
                raise HTTPException(status_code=code, detail={
                    "message": str(exc), "problems": exc.problems}) from exc
            logger.info("wbs proposal approved: %s by %s from %s (applied=%s)", proposal_id,
                        role, _client_ip(request), outcome["applied"])
            return outcome
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

    # -- ナレッジ公開候補のレビュー（人間の承認フロー）/ knowledge candidate review ---
    #
    # `vector_store.submit_candidate`（generalize_knowledge などのテンプレート）
    # が私有コレクションに置いた、公開コレクションへの昇格待ちの候補を見る・直す・
    # 承認・却下する。アダプタ側の list_candidates / edit_candidate /
    # decide_candidate には @action を付けていないため、テンプレートからは
    # 呼べない——この画面（と CLI の `aipmo knowledge`）だけが呼ぶ。
    # 閲覧は viewer にも許し、修正・承認・却下は operator のみ
    # （WBS 変更提案と同じ役割分離）。
    #
    # Reviews candidates a template (e.g. generalize_knowledge) placed in the
    # private collection via `vector_store.submit_candidate`, awaiting
    # promotion to the public one. The adapter's list_candidates /
    # edit_candidate / decide_candidate are not @action-decorated, so no
    # template can call them — only this screen (and the CLI's `aipmo
    # knowledge`) do. Viewing is open to viewer; editing, approving, and
    # rejecting require operator (the same split as WBS proposals).

    def _knowledge_adapter_or_503(backend: str | None) -> Any:
        name = backend or "vector_store"
        if not engine.adapters.has(name):
            raise HTTPException(
                status_code=503,
                detail=f"{name} adapter is not configured / {name} アダプタが設定されていません",
            )
        return engine.adapters.get(name)

    @app.get("/api/knowledge", dependencies=[guard])
    def list_knowledge_candidates(status: str = "pending", backend: str | None = None) -> dict[str, Any]:
        adapter = _knowledge_adapter_or_503(backend)
        items = adapter.list_candidates(status=status)
        return {"items": items}

    @app.get("/api/knowledge/{candidate_id}", dependencies=[guard])
    def get_knowledge_candidate(candidate_id: str, backend: str | None = None) -> Any:
        adapter = _knowledge_adapter_or_503(backend)
        item = adapter.get_candidate(candidate_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such candidate")
        return item

    @app.get("/api/knowledge/{candidate_id}/stats", dependencies=[guard])
    def knowledge_candidate_stats(candidate_id: str, backend: str | None = None) -> Any:
        """判断の参考情報：似た過去の候補で、人は／LLM はどう判断したか（WBS 6.40）。"""
        adapter = _knowledge_adapter_or_503(backend)
        item = adapter.get_candidate(candidate_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such candidate")
        return adapter.similar_candidates_stats(item["payload"].get("text") or "",
                                                exclude_id=candidate_id)

    @app.post("/api/knowledge/{candidate_id}/edit")
    def edit_knowledge_candidate(
        candidate_id: str, payload: dict[str, Any], role: str = operator_guard,
    ) -> Any:
        adapter = _knowledge_adapter_or_503(payload.get("backend"))
        try:
            result = adapter.edit_candidate(candidate_id, text=payload.get("text"),
                                            fields=payload.get("fields"))
        except AdapterError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        logger.info("knowledge candidate edited: %s by %s", candidate_id, role)
        return result

    def _decide_knowledge_candidate(
        candidate_id: str, *, approve: bool, payload: dict[str, Any], role: str,
    ) -> Any:
        adapter = _knowledge_adapter_or_503(payload.get("backend"))
        try:
            result = adapter.decide_candidate(candidate_id, approve=approve, reviewer=role,
                                              note=payload.get("note"))
        except AdapterError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        logger.info("knowledge candidate decided: %s %s by %s", candidate_id,
                   result["status"], role)
        return result

    @app.post("/api/knowledge/{candidate_id}/approve")
    def approve_knowledge_candidate(
        candidate_id: str, payload: dict[str, Any] | None = None, role: str = operator_guard,
    ) -> Any:
        return _decide_knowledge_candidate(candidate_id, approve=True, payload=payload or {},
                                           role=role)

    @app.post("/api/knowledge/{candidate_id}/reject")
    def reject_knowledge_candidate(
        candidate_id: str, payload: dict[str, Any] | None = None, role: str = operator_guard,
    ) -> Any:
        return _decide_knowledge_candidate(candidate_id, approve=False, payload=payload or {},
                                           role=role)

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
