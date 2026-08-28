"""PostgreSQL アダプタ / PostgreSQL adapter.

用途 / Purpose:
  - 実行履歴の永続化 / run history
  - ナレッジ候補と昇格ワークフローの管理 / knowledge candidates and promotion workflow
  - テナントごとの利用許諾レベル / per-tenant data-use consent level

設計判断 / Design decision — 生 SQL をテンプレートに書かせない:
  テンプレートは第三者が書いて配布される想定（教材販売）。
  生 SQL を許すと、配布テンプレートが他テナントのデータを読める。
  そこで SQL は config 側の「名前付きクエリ」にのみ存在させ、
  テンプレートはクエリ名とパラメータしか指定できない。
  DSL の式評価を Jinja2 にしなかったのと同じ理由。

  Templates are authored by third parties and distributed (they are sold as
  teaching material). Allowing raw SQL in a template would let a distributed
  template read another tenant's data. SQL therefore lives only in the
  operator's config as named queries; templates may pass a query name and
  bound parameters, nothing else. Same reasoning as rejecting Jinja2 in the
  expression evaluator.
"""
from __future__ import annotations

import re
from typing import Any

from .base import Adapter, AdapterError, action

# 名前付きクエリ内のプレースホルダ / placeholders inside a named query
PARAM_RE = re.compile(r":([a-z_][a-z0-9_]*)", re.I)

WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|merge)\b", re.I
)


class PostgresAdapter(Adapter):
    name = "postgres"

    def __init__(
        self,
        dsn: str | None = None,
        queries: dict[str, str] | None = None,
        tenant: str | None = None,
        connection: Any = None,
        connect_attempts: int = 3,
        connect_backoff: float = 2.0,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.dsn = dsn
        self.queries = dict(queries or {})
        self.tenant = tenant
        self.connect_attempts = connect_attempts
        self.connect_backoff = connect_backoff
        self._connection = connection  # テスト時に注入 / injected in tests
        self._injected = connection is not None

    # -- 接続 / connection -------------------------------------------------

    def _connect(self) -> Any:
        """接続を確保する。切れていれば張り直す。

        無料枠のマネージド PostgreSQL は、使われない間サービスを停止する。
        接続を抱えたまま再接続しないと、翌朝の最初の実行が必ず落ちる。
        起床には数秒〜数十秒かかるので、待って数回試す。

        Managed PostgreSQL on a free plan powers off while idle. Holding one
        connection and never redialling means the first run of the next morning
        always fails. Waking the service takes seconds to tens of seconds, so
        this retries with a backoff rather than failing on the first refusal.
        """
        if self._connection is not None and not self._is_closed(self._connection):
            return self._connection
        if self._injected:
            return self._connection
        if not self.dsn:
            raise AdapterError("postgres: dsn が設定されていません / dsn is not configured")

        import time

        import psycopg  # 遅延 import / lazy import

        last: Exception | None = None
        for attempt in range(1, max(1, self.connect_attempts) + 1):
            try:
                self._connection = psycopg.connect(self.dsn)
                return self._connection
            except psycopg.OperationalError as exc:
                last = exc
                if attempt < self.connect_attempts:
                    time.sleep(self.connect_backoff * attempt)

        raise AdapterError(
            f"postgres: 接続できませんでした / could not connect after "
            f"{self.connect_attempts} attempts: {last}"
        )

    @staticmethod
    def _is_closed(connection: Any) -> bool:
        closed = getattr(connection, "closed", False)
        return bool(closed)

    def health_check(self) -> bool:
        try:
            with self._connect().cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception:
            return False

    def _retry_once_on_dead_connection(self, work):
        """接続が途中で死んでいたら、一度だけ張り直して再実行する。

        停止中のサービスに対しては、接続自体は成功しても最初の問い合わせで
        切れることがある。_connect のリトライだけでは拾えない。

        A powered-down service can accept a connection and then drop it on the
        first statement, which the connect-time retry alone does not catch.
        """
        try:
            return work(self._connect())
        except Exception:
            if self._injected:
                raise
            import psycopg

            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:
                    pass
            self._connection = None
            try:
                return work(self._connect())
            except psycopg.OperationalError as exc:
                raise AdapterError(f"postgres: {exc}") from exc

    # -- 内部 / internals --------------------------------------------------

    def _resolve(self, query_name: str) -> str:
        if query_name not in self.queries:
            raise AdapterError(
                f"postgres: 名前付きクエリ '{query_name}' が未定義です "
                f"/ named query '{query_name}' is not defined "
                f"(定義済み / defined: {', '.join(sorted(self.queries)) or 'なし / none'})"
            )
        return self.queries[query_name]

    def _bind(self, sql: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
        """:name プレースホルダを %s に変換し、値を位置引数に並べ替える。

        文字列連結は一切しない。値は必ずドライバのパラメータ機構を通す。
        Values never touch string concatenation; they always go through the
        driver's parameter binding.
        """
        scoped = dict(params)
        if self.tenant is not None:
            # テナントはテンプレートではなく接続設定から来る
            # The tenant comes from connection config, never from the template.
            scoped["tenant"] = self.tenant

        values: list[Any] = []
        missing: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in scoped:
                missing.append(key)
                return match.group(0)
            values.append(scoped[key])
            return "%s"

        converted = PARAM_RE.sub(substitute, sql)
        if missing:
            raise AdapterError(
                f"postgres: パラメータが不足しています / missing parameters: "
                f"{', '.join(sorted(set(missing)))}"
            )
        return converted, values

    # -- アクション / actions ----------------------------------------------

    @action()
    def query(self, name: str, params: dict[str, Any] | None = None,
              limit: int | None = None) -> dict[str, Any]:
        """読み取り専用。書き込み系 SQL は拒否する。
        Read-only. Write statements are rejected."""
        sql = self._resolve(name)
        if WRITE_RE.match(sql):
            raise AdapterError(
                f"postgres: '{name}' は書き込みクエリです。execute を使ってください "
                f"/ is a write query; use execute instead"
            )
        bound, values = self._bind(sql, params or {})

        def work(connection: Any) -> tuple[list[str], list[Any]]:
            with connection.cursor() as cur:
                cur.execute(bound, values)
                columns = [c[0] for c in (cur.description or [])]
                return columns, (cur.fetchmany(limit) if limit else cur.fetchall())

        columns, rows = self._retry_once_on_dead_connection(work)
        records = [dict(zip(columns, row)) for row in rows]
        return {"rows": records, "count": len(records)}

    @action(writes=True)
    def execute(self, name: str, params: dict[str, Any] | None = None,
                idempotency_key: str | None = None) -> dict[str, Any]:
        """書き込み。冪等キーはパラメータとして名前付きクエリに渡せる。

        Writes. The idempotency key is exposed to the named query as
        :idempotency_key so the SQL can use ON CONFLICT DO NOTHING.
        """
        sql = self._resolve(name)
        merged = dict(params or {})
        if idempotency_key is not None and ":idempotency_key" in sql:
            merged["idempotency_key"] = idempotency_key
        bound, values = self._bind(sql, merged)

        def work(connection: Any) -> dict[str, Any]:
            with connection.cursor() as cur:
                cur.execute(bound, values)
                affected = cur.rowcount
                returned: list[dict[str, Any]] = []
                if cur.description:
                    columns = [c[0] for c in cur.description]
                    returned = [dict(zip(columns, row)) for row in cur.fetchall()]
            connection.commit()
            return {"affected": affected, "rows": returned}

        return self._retry_once_on_dead_connection(work)

    @action()
    def consent_level(self, tenant: str | None = None) -> dict[str, Any]:
        """テナントの利用許諾レベルを引く / look up a tenant's data-use consent.

        A: 二次利用不可 / no secondary use
        B: 匿名化ノウハウとして利用可 / anonymized knowledge may be reused
        C: 事例公開可 / case study may be published
        """
        target = tenant or self.tenant
        if not target:
            raise AdapterError("postgres: tenant が指定されていません / tenant not specified")
        result = self.query("consent_level_by_tenant", {"tenant": target})
        rows = result["rows"]
        return {"tenant": target, "level": rows[0]["level"] if rows else "A"}
