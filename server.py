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
  - Binding 0.0.0.0 emits a warning. TLS is expected from a reverse proxy.
"""
from __future__ import annotations

import logging
import secrets
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
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..dsl import loader
from ..engine.runner import Engine, StepFailure
from ..i18n import CATALOG, DEFAULT_LANG, detect, normalize

logger = logging.getLogger("aipmo.web")

STATIC_DIR = Path(__file__).parent / "static"

# 実行履歴の保持件数。Postgres 連携が入るまではメモリ上のみ。
# In-memory run history until the Postgres wiring lands.
HISTORY_LIMIT = 50


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
    token: str,
    tenant: str = "",
    lang: str | None = None,
    store: RunStore | None = None,
):
    runs = store or RunStore()
    ui_lang = normalize(lang) if lang else detect()

    app = FastAPI(title="AI-PMO", docs_url=None, redoc_url=None,
                  openapi_url=None)

    # -- 認証 / authentication --------------------------------------------

    def check(request: Request) -> None:
        supplied = (
            request.headers.get("x-aipmo-token")
            or request.cookies.get("aipmo_token")
            or request.query_params.get("token")
            or ""
        )
        # 定数時間比較。長さの違いも漏らさない。
        # Constant-time; does not leak length either.
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="invalid token")

    guard = Depends(check)

    # -- 画面 / screens ----------------------------------------------------

    @app.get("/")
    def index(request: Request) -> Response:
        supplied = (
            request.cookies.get("aipmo_token")
            or request.query_params.get("token")
            or ""
        )
        if not secrets.compare_digest(supplied, token):
            return FileResponse(STATIC_DIR / "locked.html", status_code=401)

        response = FileResponse(STATIC_DIR / "index.html")
        # クエリのトークンを Cookie に移す。以後 URL にキーが残らないので、
        # 共有・スクリーンショット・履歴からの漏洩を減らせる。
        # Move the token from the query string into a cookie so it stops
        # appearing in the address bar, screenshots and browser history.
        response.set_cookie(
            "aipmo_token", token, httponly=True, samesite="strict",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- API ---------------------------------------------------------------

    @app.get("/api/session", dependencies=[guard])
    def session() -> dict[str, Any]:
        return {
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

    @app.post("/api/runs", dependencies=[guard])
    def start_run(payload: dict[str, Any]) -> Any:
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

        try:
            ctx = engine.run(
                template,
                params=payload.get("params") or {},
                trigger=payload.get("trigger") or {},
            )
            status = "success"
            error = None
        except StepFailure as exc:
            ctx = exc.context
            status = "failed"
            error = str(exc)
            if ctx is None:
                record = {
                    "id": secrets.token_hex(6), "template": template.name,
                    "status": status, "error": error, "steps": [],
                    "started_at": None,
                }
                runs.add(record)
                return JSONResponse(status_code=200, content=record)

        record = {
            "id": ctx.run_id,
            "template": template.name,
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

    @app.get("/api/health", dependencies=[guard])
    def health() -> dict[str, Any]:
        return {
            "adapters": {
                name: engine.adapters.get(name).health_check()
                for name in engine.adapters.names()
            }
        }

    return app


def generate_token() -> str:
    return secrets.token_urlsafe(24)
