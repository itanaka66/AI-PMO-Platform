"""CLI エントリポイント。

  aipmo validate templates/examples/meeting_minutes.yaml
  aipmo run templates/examples/meeting_minutes.yaml --param meeting_id=MTG-001
  aipmo adapters
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
from typing import cast
from pathlib import Path
from typing import Any

import yaml

from .console import configure_stdio, mark
from .messages import localize_briefing, suggestion_reason, task_title
from .adapters.base import AdapterRegistry
from .adapters.mock import MockJiraAdapter, MockSlackAdapter, MockTeamsAdapter
from .adapters.chroma import ChromaAdapter
from .adapters.milvus import MilvusAdapter
from .adapters.pgvector import PgVectorAdapter
from .adapters.postgres import PostgresAdapter
from .adapters.qdrant import QdrantAdapter
from .adapters.slack import SlackAdapter
from .adapters.weaviate import WeaviateAdapter
from .dsl import loader
from .engine.agent import ApprovalCallback
from .engine.runner import Engine, PromptLibrary, StepFailure
from .llm.embeddings import build_embedder
from .setup_wizard import load_env, run_interactive
from .llm.registry import LLMRegistry

DEFAULT_CONFIG = Path(os.environ.get("AIPMO_CONFIG", "config.yaml"))

# ベクトルストアの選択肢。どれか1つだけ config に書けばよい。
# 5種類のうちどれを選んでも、テンプレートからは同じ形（search / upsert /
# submit_candidate）で使える — 違いは接続方法だけ。
#
# The vector-store choices. Configure exactly one of them. Whichever is
# chosen, a template sees the same shape (search / upsert / submit_candidate)
# — only the connection differs.
VECTOR_STORE_ADAPTERS: dict[str, type] = {
    "qdrant": QdrantAdapter,
    "pgvector": PgVectorAdapter,
    "chroma": ChromaAdapter,
    "milvus": MilvusAdapter,
    "weaviate": WeaviateAdapter,
}


class ConfigError(Exception):
    """設定の誤り。利用者にそのまま見せる文面を持つ / user-facing message."""


ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """設定内の ${VAR} と ${VAR:-default} を環境変数で置き換える。

    資格情報を config.yaml に書かせないために必要。設定ファイルは
    共有され、Git に入り、サポートに貼られる。DSN やキーはそこに置けない。
    未定義の変数はそのまま残す。空文字に潰すと、間違った DSN で
    接続を試みて原因のわかりにくい失敗になる。

    Lets credentials stay out of config.yaml, which gets shared, committed and
    pasted into support threads. An undefined variable is left as-is rather
    than collapsed to an empty string: silently blanking it would produce a
    malformed DSN and a failure that is hard to trace back here.
    """
    if isinstance(value, str):
        def swap(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved is not None:
                return resolved
            return default if default is not None else match.group(0)

        return ENV_REF.sub(swap, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return expand_env(raw)


def build_engine(
    config: dict[str, Any],
    base_dir: Path | None = None,
    approve: ApprovalCallback | None = None,
) -> Engine:
    """設定からエンジンを組み立てる。

    相対パスは config.yaml のある場所を基準に解決する。ショートカットから
    起動すると作業ディレクトリが不定になるため、そこに依存させない。

    `approve` は既定で無し — 対話端末の無いスケジューラや Web サーバーからは
    渡さない。承認が要る書き込みは、そこでは常に断られる。

    Relative paths resolve against the directory holding config.yaml. Launching
    from a desktop shortcut leaves the working directory unpredictable, so it
    must not be the anchor.

    `approve` defaults to none — the scheduler and web server, which have no
    interactive terminal, do not pass one. Writes that require approval are
    always refused there.
    """
    base = base_dir or Path.cwd()
    adapters = AdapterRegistry()
    adapter_config = config.get("adapters") or {}
    tenant = config.get("tenant")

    def resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else base / path

    # mock は既定のまま。テンプレートを書く段階で実テナントを要求しない。
    # real に切り替えると、設定のある実アダプタだけが登録される。
    # Mock remains the default so writing templates needs no live tenant.
    # Under "real", only the adapters that are actually configured register.
    if adapter_config.get("mode", "mock") == "mock":
        adapters.register(MockTeamsAdapter())
        adapters.register(MockJiraAdapter())
        adapters.register(MockSlackAdapter())
    else:
        if "teams" in adapter_config:
            from .adapters.teams import TeamsAdapter

            adapters.register(TeamsAdapter(**dict(adapter_config["teams"])))

        if "jira" in adapter_config:
            from .adapters.jira import JiraAdapter

            adapters.register(JiraAdapter(**dict(adapter_config["jira"])))

        if "agile" in adapter_config:
            from .adapters.jira_agile import JiraAgileAdapter

            # Jira と同じ資格情報で動く。設定を二重に書かせない。
            # Runs on the same credentials; the config is not repeated.
            spec = {**dict(adapter_config.get("jira") or {}),
                    **dict(adapter_config["agile"])}
            adapters.register(JiraAgileAdapter(**spec))

        if "slack" in adapter_config:
            adapters.register(SlackAdapter(**dict(adapter_config["slack"])))

        if "github_projects" in adapter_config:
            from .adapters.github_projects import GitHubProjectsAdapter

            adapters.register(
                GitHubProjectsAdapter(**dict(adapter_config["github_projects"])))

        if "plane" in adapter_config:
            from .adapters.plane import PlaneAdapter

            adapters.register(PlaneAdapter(**dict(adapter_config["plane"])))

        if "openproject" in adapter_config:
            from .adapters.openproject import OpenProjectAdapter

            adapters.register(OpenProjectAdapter(**dict(adapter_config["openproject"])))

        if "azure_devops" in adapter_config:
            from .adapters.azure_devops import AzureDevOpsAdapter

            adapters.register(AzureDevOpsAdapter(**dict(adapter_config["azure_devops"])))

    if "postgres" in adapter_config:
        spec = dict(adapter_config["postgres"])
        queries_file = spec.pop("queries_file", None)
        queries = dict(spec.pop("queries", {}) or {})
        if queries_file:
            path = resolve(queries_file)
            if not path.exists():
                raise ConfigError(
                    f"クエリ定義が見つかりません / query file not found: {path}\n"
                    f"config.yaml の adapters.postgres.queries_file を確認してください "
                    f"/ check adapters.postgres.queries_file in config.yaml"
                )
            queries.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        adapters.register(PostgresAdapter(queries=queries, tenant=tenant, **spec))

    # risk_forecast は外部認証情報を持たない純粋な計算アダプタだが、
    # 他のすべての実アダプタと同じく明示的な opt-in にする — 有効かどうかが
    # config.yaml を見るだけで分かるようにするため（黙って常時有効にすると、
    # 「なぜこの機能が動いているのか」が config から読み取れなくなる）。
    #
    # risk_forecast needs no credentials, but is still opt-in like every
    # other real adapter — so whether it is active is visible from
    # config.yaml alone, rather than always running silently with no line
    # in the config to explain why.
    if "risk_forecast" in adapter_config:
        from .adapters.risk_forecast import RiskForecastAdapter

        adapters.register(RiskForecastAdapter(**dict(adapter_config["risk_forecast"])))

    # crawler も risk_forecast と同じ理由で opt-in — 認証情報は要らないが、
    # 外部サイトに実際に到達するアダプタなので、有効かどうかは
    # config.yaml から読み取れるようにする。
    #
    # crawler is opt-in for the same reason as risk_forecast — no
    # credentials needed, but it does reach real external sites, so whether
    # it is active should be visible from config.yaml.
    if "crawler" in adapter_config:
        from .adapters.crawler import CrawlerAdapter

        adapters.register(CrawlerAdapter(**dict(adapter_config["crawler"])))

    # wbs_file も opt-in。読む範囲は root の下の YAML だけ（既定は config.yaml
    # のあるディレクトリ）。
    # wbs_file is opt-in too; it reads YAML under `root` only (default: the
    # directory holding config.yaml).
    if "wbs_file" in adapter_config:
        from .adapters.wbs_file import WbsFileAdapter

        wbs_spec = dict(adapter_config["wbs_file"] or {})
        wbs_spec["root"] = str(resolve(str(wbs_spec.get("root", "."))))
        adapters.register(WbsFileAdapter(**wbs_spec))

    # wbs_replan は postgres か、無ければ台帳（LedgerProposalStore）の上に
    # 合成される（JiraAgileAdapter が jira の上に合成されるのと同じ形）。
    # ただし「新規提案の作成」（propose）は risk_forecast のスナップショット
    # （別の PostgreSQL 専用表）も要るため、台帳だけでは働かない——その場合
    # でも、既にある提案の一覧・承認・却下・反映（aipmo wbs proposals）は
    # 台帳だけで動く。
    #
    # wbs_replan is composed on top of postgres, or on the ledger
    # (LedgerProposalStore) when there is none (same shape as JiraAgileAdapter
    # over jira). *Creating* a proposal (propose) also needs a risk_forecast
    # snapshot (a separate PostgreSQL-only table), so that still fails without
    # postgres — but viewing, approving, rejecting and applying proposals
    # that already exist (`aipmo wbs proposals`) works on the ledger alone.
    if "wbs_replan" in adapter_config:
        if adapters.has("postgres"):
            store: Any = adapters.get("postgres")
        else:
            from .wbs_proposals import LedgerProposalStore

            try:
                store = LedgerProposalStore(open_ledger(config, base).side)
            except ConfigError as exc:
                raise ConfigError(
                    "config.yaml の adapters.wbs_replan を使うには adapters.postgres か、"
                    f"使える台帳（task_engine）のどちらかが必要です / adapters.wbs_replan "
                    f"needs either adapters.postgres or a usable ledger (task_engine): {exc}"
                ) from exc
        from .adapters.wbs_replan import WbsReplanAdapter

        replan_spec = dict(adapter_config["wbs_replan"] or {})
        adapters.register(WbsReplanAdapter(
            postgres=store,
            file=str(resolve(str(replan_spec["file"]))) if replan_spec.get("file") else None,
            root=str(resolve(str(replan_spec.get("root", ".")))) if replan_spec.get("file")
            else None))

    # ベクトルストアは5種類のうちどれを設定してもよい。ちょうど1つだけ
    # 設定されているときは、論理名 vector_store でも同じインスタンスを
    # 登録する — 新しいテンプレートはそちらを使えば、あとでバックエンドを
    # 乗り換えてもテンプレート側の変更が要らない。2つ以上設定された場合は
    # 曖昧になるため、論理名の別名づけは行わない（各バックエンド固有の
    # 名前では引き続き使える）。
    #
    # Configure whichever one of the five vector-store backends you want.
    # When exactly one is configured, the same instance is additionally
    # registered under the logical name vector_store — a new template can use
    # that name and survive a later backend switch untouched. With two or
    # more configured, the logical alias is skipped as ambiguous (each
    # backend's own name still works).
    configured_vector_stores = [name for name in VECTOR_STORE_ADAPTERS if name in adapter_config]
    for name in configured_vector_stores:
        spec = dict(adapter_config[name])
        embedder = build_embedder(spec.pop("embedding", None))
        instance = VECTOR_STORE_ADAPTERS[name](tenant=tenant, embedder=embedder, **spec)
        adapters.register(instance)
        if len(configured_vector_stores) == 1:
            adapters.register(instance, name="vector_store")

    # config.yaml の approval.slack は、渡された approve より優先する。
    # 明示的な運用設定の方が、呼び出し元既定の対話端末承認より意図が強い。
    # これにより aipmo run / aipmo schedule / aipmo serve のどこから実行
    # しても、Slack 上で承認できるようになる — 対話端末が無い実行環境でも
    # 承認ゲートが機能する。
    #
    # config.yaml's approval.slack takes priority over any `approve` passed
    # in: an explicit operator setting carries more intent than the caller's
    # own default (an interactive terminal prompt). This is what lets
    # `aipmo run` / `aipmo schedule` / `aipmo serve` all approve over Slack —
    # the approval gate works even where no terminal is attached.
    approval_config = config.get("approval") or {}
    if "slack" in approval_config:
        if not adapters.has("slack"):
            raise ConfigError(
                "config.yaml の approval.slack を使うには adapters.slack の設定も"
                "必要です / approval.slack requires adapters.slack to also be "
                "configured"
            )
        slack_cfg = dict(approval_config["slack"])
        channel = slack_cfg.get("channel")
        if not channel:
            raise ConfigError(
                "config.yaml の approval.slack.channel が必要です "
                "/ approval.slack.channel is required"
            )
        from .approval import SlackApprover

        approve = SlackApprover(
            slack=cast(SlackAdapter, adapters.get("slack")),
            channel=channel,
            poll_seconds=float(slack_cfg.get("poll_seconds", 5.0)),
            timeout_seconds=float(slack_cfg.get("timeout_seconds", 300.0)),
            approver_ids=frozenset(slack_cfg.get("approver_ids") or []),
        )

    llms = LLMRegistry.from_config(config.get("llm") or {"default": {"provider": "echo"}})
    prompts = PromptLibrary(resolve(config.get("prompts_dir", "prompts")))
    return Engine(adapters, llms, prompts, approve=approve)


def ledger_path(config: dict[str, Any], base: Path) -> Path:
    """台帳ファイルの場所。schedule / serve / pmo / assign で同じにする。

    The ledger's location — identical for schedule, serve, pmo and assign, so
    the web screen reads the file the scheduler writes.
    """
    section = config.get("task_engine")
    section = section if isinstance(section, dict) else {}
    path = Path(section.get("file", base / "task-ledger.db"))
    return path if path.is_absolute() else base / path


def ledger_store_factory(config: dict[str, Any]):
    """台帳の保存先を作る関数。SQLite（既定）なら None を返す。

    `task_engine.backend` が `postgres` のとき、接続先は `task_engine.dsn`、
    なければ `adapters.postgres.dsn`。設定の誤り（テナントが無い、接続先が
    無い、知らない種類）は、起動時にここで分かる。

    Returns a function that builds the ledger's store, or None for the default
    SQLite. With `task_engine.backend: postgres` the DSN is `task_engine.dsn`,
    else `adapters.postgres.dsn`. A misconfiguration (no tenant, no DSN,
    unknown kind) is found here at startup.
    """
    from .ledger_store import LedgerConfigError, PostgresStore

    section = config.get("task_engine")
    section = section if isinstance(section, dict) else {}
    backend = str(section.get("backend") or "sqlite").lower()
    if backend == "sqlite":
        return None
    if backend != "postgres":
        raise ConfigError(
            f"task_engine.backend が不正です: {backend!r}（sqlite か postgres）"
            f" / unknown task_engine.backend {backend!r} (sqlite or postgres)")

    dsn = section.get("dsn") or ((config.get("adapters") or {}).get("postgres") or {}).get("dsn")
    tenant = config.get("tenant") or None
    try:
        PostgresStore(str(dsn or ""), tenant)         # 設定の検査だけ（接続はしない）
    except LedgerConfigError as exc:
        raise ConfigError(str(exc)) from exc
    return lambda: PostgresStore(str(dsn), tenant)


def web_pool_settings(web: dict[str, Any]) -> dict[str, Any]:
    """`web.pool`（台帳の接続プール）。画面が台帳へ張る接続の上限と、待ち時間。

    size: 同時に持つ接続の上限（既定 8、0 でプールを使わない）。
    timeout_seconds: 全部使用中のとき、順番を待つ秒数（既定 10。過ぎたら 503）。
    idle_seconds: 使われないまま持ち続ける秒数（既定 300）。
    """
    section = web.get("pool") or {}
    if not isinstance(section, dict):
        raise ConfigError("web.pool はマッピングで書いてください / web.pool must be a mapping")

    def number(key: str, default: float, low: float, high: float) -> float:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ConfigError(f"web.pool.{key} は {low:g}〜{high:g} の数値: {value!r} "
                              f"/ web.pool.{key} must be a number in {low:g}..{high:g}")
        return float(value)

    return {"pool_size": int(number("size", 8, 0, 100)),
            "pool_timeout": number("timeout_seconds", 10, 0.1, 300),
            "pool_idle": number("idle_seconds", 300, 1, 86400)}


def side_storage_mode(config: dict[str, Any]) -> str:
    """`task_engine.side_storage`（auto / file / database）。台帳の隣に置くものの置き場。

    auto は、PostgreSQL なら同じデータベース、SQLite なら隣のファイル。
    Where the briefing, decision log and state live (auto: the database for PostgreSQL, files
    beside the ledger for SQLite).
    """
    from .side_store import STORAGE_MODES

    section = config.get("task_engine")
    section = section if isinstance(section, dict) else {}
    mode = str(section.get("side_storage") or "auto").lower()
    if mode not in STORAGE_MODES:
        raise ConfigError(f"task_engine.side_storage が不正です: {mode!r}（auto / file / database）"
                          f" / unknown task_engine.side_storage {mode!r}")
    return mode


def open_ledger(config: dict[str, Any], base: Path, **kwargs: Any):
    """台帳を開く。設定の誤り・別テナントの台帳・接続失敗は設定エラーにする。

    Opens the ledger. A misconfiguration, another tenant's ledger or a failed
    connection becomes a config error.
    """
    from .ledger_store import LedgerConfigError, LedgerTenantError
    from .task_engine import TaskEngine

    factory = ledger_store_factory(config)
    try:
        return TaskEngine(ledger_path(config, base),
                          tenant=config.get("tenant") or None,
                          store=factory() if factory else None,
                          side_storage=side_storage_mode(config), **kwargs)
    except (LedgerTenantError, LedgerConfigError) as exc:
        raise ConfigError(str(exc)) from exc


def attach_task_engine(engine: Engine, config: dict[str, Any], base: Path,
                       default: bool, launch: bool = False):
    """複数テンプレート横断の Task Engine を Engine に繋ぐ。

    常駐する `aipmo schedule` では既定で有効、それ以外は config.yaml に
    `task_engine:` を書いたときだけ。`enabled: false` でいつでも切れる。

    Hooks the cross-template Task Engine to the engine. On by default for the
    resident `aipmo schedule`; elsewhere only when config.yaml has a
    `task_engine:` section. `enabled: false` always turns it off.
    """
    section = config.get("task_engine")
    if isinstance(section, dict):
        enabled = bool(section.get("enabled", True))
    else:
        section, enabled = {}, default
    if not enabled:
        return None
    task_engine = open_ledger(config, base, stale_days=int(section.get("stale_days", 30)))
    task_engine.attach(engine)
    return build_pmo_core(config, task_engine, engine, base if launch else None, base=base)


def build_pmo_core(config: dict[str, Any], task_engine: Any,
                   engine: Engine | None = None, launch_base: Path | None = None,
                   base: Path | None = None):
    """Task Engine の上に PMO Core（担当割当・進捗ルール・統括）を載せる。

    `engine` を渡したときだけ、`pmo_core.notify.slack_channel` への通知を
    有効にする（表示だけのコマンドは通知しない）。

    Puts the PMO Core (assignment, progress rules, oversight) on the Task
    Engine. Notification to `pmo_core.notify.slack_channel` is wired only
    when an `engine` is given — display-only commands never notify. Likewise
    `pmo_core.responses` templates are launched only when `launch_base` (the
    config directory) is given, i.e. by the resident `aipmo schedule`; every
    other command merely reports what would launch.
    """
    from .pmo_core import (PmoCore, RuleError, load_members, load_responses,
                           load_rules, slack_notifier)

    section = config.get("pmo_core") or {}
    try:
        rules = load_rules(section.get("rules"))
    except RuleError as exc:
        raise ConfigError(f"pmo_core.rules: {exc}") from exc

    notify = None
    notify_config = section.get("notify") or {}
    channel = notify_config.get("slack_channel")
    if channel and engine is not None and engine.adapters.has("slack"):
        notify = slack_notifier(engine.adapters.get("slack"), channel)

    try:
        responses = load_responses(section.get("responses"))
    except RuleError as exc:
        raise ConfigError(f"pmo_core.responses: {exc}") from exc

    try:
        members = load_members(section.get("members"))
    except RuleError as exc:
        raise ConfigError(f"pmo_core.members: {exc}") from exc

    # 自律的な判断。`pmo_core.judgment` を書いたときだけ(オプトイン)。
    # Autonomous judgment, opt-in via `pmo_core.judgment`.
    from .judgment import JudgmentError, load_judgment

    try:
        judgment = load_judgment(section.get("judgment"))
    except JudgmentError as exc:
        raise ConfigError(f"pmo_core.judgment: {exc}") from exc

    # 高リスク時の応答と、役割AI（`kind: agent` のメンバー）は、どちらも
    # テンプレートを起動する。起動できるのは、運用者が設定に書いたものだけ。
    # Responses to high risk and role AIs (`kind: agent` members) both launch
    # templates, and only the ones the operator wrote into the config.
    launcher = None
    wanted = ({r.template for r in responses}
              | {str(m.template) for m in members if m.is_agent}
              | {e.template for e in (judgment.launches if judgment else ())})
    if wanted:
        root = Path((config.get("web") or {}).get("templates_dir", "templates"))
        root = root if root.is_absolute() else (launch_base or base or Path.cwd()) / root
        index = _template_index(root)
        # 3時に初めて気づくより、起動時に落とす。
        # Fail at startup rather than discover a typo at 3am.
        missing = sorted(wanted - set(index))
        if missing:
            raise ConfigError(
                f"pmo_core: テンプレートが見つかりません（responses / 役割AI）"
                f" / not found in {root}: {', '.join(missing)}")
        if engine is not None and launch_base is not None:
            def launcher(response, trigger):
                # 結果（実行の記録）を返す。役割AIの成果を台帳に残すのに使う。
                # Returns the run so a role AI's result can be recorded.
                return engine.run(index[response.template], params=response.params,
                                  trigger=trigger)

    from .collector import CollectError, Collector, load_sources
    from .generation import GenerationError, load_generation

    try:
        generation = load_generation(section.get("generate"))
    except GenerationError as exc:
        raise ConfigError(f"pmo_core.generate: {exc}") from exc
    if generation.wbs is not None:
        # WBS ファイルと証拠の基準は、config.yaml のあるディレクトリから見た場所。
        # The WBS file and its evidence root are relative to the config directory.
        home = base or launch_base or Path.cwd()
        generation = dataclasses.replace(generation, wbs=dataclasses.replace(
            generation.wbs, file=str((home / generation.wbs.file).resolve()),
            root=str((home / generation.wbs.root).resolve())))

    # 進捗の自動収集は、`pmo_core.collect` を書いたときだけ(課題管理ツールを読むので
    # オプトイン)。実際に動かすのは常駐のときだけで、表示専用のコマンドは読みに行かない。
    # Collection is opt-in (it reads trackers) and runs only in the resident process;
    # display-only commands never go out and read.
    collector = None
    collect_config = section.get("collect")
    if collect_config is not None:
        collect_config = collect_config or {}
        try:
            sources = load_sources(collect_config.get("sources"))
        except CollectError as exc:
            raise ConfigError(f"pmo_core.collect: {exc}") from exc
        if engine is not None and launch_base is not None:
            collector = Collector(
                task_engine=task_engine, adapters=engine.adapters, sources=sources,
                refresh_known=bool(collect_config.get("refresh_known", True)),
                max_refresh=int(collect_config.get("max_refresh", 50)),
                interval_minutes=int(collect_config.get("interval_minutes", 30)))

    # 起票は `pmo_core.filing` を書いたときだけ。課題を作るのは外の世界を変えるので、
    # 実際に作れるのは人の確定した操作と、運用者が auto と書いた由来の常駐だけ。
    # Filing is opt-in. Creating an issue changes the outside world, so only a human's
    # confirmed action, or the resident for origins listed under `auto`, can do it.
    from .filing import FilingConfigError, load_filing, make_filer

    try:
        filing = load_filing(section.get("filing"))
    except FilingConfigError as exc:
        raise ConfigError(f"pmo_core.filing: {exc}") from exc
    filer = None
    if filing is not None and engine is not None and launch_base is not None:
        if not engine.adapters.has(filing.tracker):
            raise ConfigError(f"pmo_core.filing: {filing.tracker} アダプタが設定されていません "
                              f"/ the {filing.tracker} adapter is not configured")
        filer = make_filer(engine.adapters, members, filing,
                           lookup=_lookup_assignees(config))

    learning = section.get("learning") or {}
    agents_config = section.get("agents") or {}
    return PmoCore(task_engine=task_engine, rules=rules, filing=filing, filer=filer,
                   collector=collector, generation=generation, judgment=judgment,
                   acting=engine is not None and launch_base is not None,
                   members=members, notify=notify, lang=_display_lang(config),
                   renotify_hours=int(notify_config.get("renotify_hours", 24)),
                   learning=bool(learning.get("enabled", True)),
                   min_samples=int(learning.get("min_samples", 5)),
                   learn_priority=bool(learning.get("priority", True)),
                   learn_estimates=bool(learning.get("estimates", True)),
                   max_estimate_error=float(learning.get("max_estimate_error", 0.75)),
                   agent_timeout_minutes=int(agents_config.get("timeout_minutes", 60)),
                   agent_max_per_day=int(agents_config.get("max_per_day", 20)),
                   responses=responses, launcher=launcher)


def _wbs_target(config: dict[str, Any], base: Path):
    """Web で承認した WBS 変更提案を反映する先。`adapters.wbs_replan.file` があるときだけ。"""
    from .wbs_proposals import Target

    spec = (config.get("adapters") or {}).get("wbs_replan") or {}
    if not spec.get("file"):
        return None
    try:
        decisions = open_ledger(config, base).side
    except ConfigError:
        return None
    file = Path(str(spec["file"]))
    root = Path(str(spec.get("root", ".")))
    return Target(file=(file if file.is_absolute() else base / file).resolve(),
                  root=(root if root.is_absolute() else base / root).resolve(),
                  decisions=decisions)


def _wbs_view(config: dict[str, Any], base: Path):
    """WBS 画面が読むファイルと、証拠の基準。`pmo_core.generate.wbs`、無ければ `adapters.wbs_replan`。"""
    spec = (((config.get("pmo_core") or {}).get("generate") or {}).get("wbs"))         or (config.get("adapters") or {}).get("wbs_replan") or {}
    if not isinstance(spec, dict) or not spec.get("file"):
        return None
    file, root = Path(str(spec["file"])), Path(str(spec.get("root", ".")))
    return ((file if file.is_absolute() else base / file).resolve(),
            (root if root.is_absolute() else base / root).resolve())


def _web_filing(config: dict[str, Any]):
    """Web の起票ボタン用。設定が壊れていれば、ボタンを出さない(起動は止めない)。"""
    from .filing import FilingConfigError, load_filing

    try:
        return load_filing(((config.get("pmo_core") or {}).get("filing")))
    except FilingConfigError:
        return None


def _template_index(root: Path) -> dict[str, Any]:
    """テンプレート名 → 読み込み済みテンプレート / template name to template."""
    index: dict[str, Any] = {}
    for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
        try:
            template = loader.load_file(path)
        except loader.TemplateError:
            continue   # 読めないものは応答に使えない。存在確認で弾かれる。
        index.setdefault(template.name, template)
    return index


def _why_lines(task, task_engine, lang: str) -> list[str]:
    """点数の内訳の各行。日本語は従来の文章、他の言語は項目（key + params）から組み立てる。"""
    from datetime import datetime as _dt

    from .messages import translate
    from .task_engine import score_breakdown

    if lang == "ja" or task.done:
        return list(task.reasons)
    _, _, parts = score_breakdown(task, _dt.now().date(), task_engine.label_bonus,
                                  task_engine.priority_delta, task_engine.pace)
    lines = []
    for part in parts:
        params = dict(part.get("params") or {})
        if part["key"] in ("s_priority", "s_priority_shift") and not params.get("priority"):
            params["priority"] = translate(lang, "s_unset")
        lines.append(f"{translate(lang, part['key'], **params)} {part['points']:+d}")
    return lines


def _judgment_config(config: dict[str, Any]):
    """画面が自律度の上書きを検証するための、設定ファイルの値。設定が壊れていれば None。"""
    from .judgment import JudgmentError, load_judgment

    try:
        return load_judgment(((config.get("pmo_core") or {}).get("judgment")))
    except JudgmentError:
        return None


def _display_lang(config: dict[str, Any]) -> str:
    """通知とCLI の表示に使う言語。設定の `lang`。無ければ従来どおり日本語。"""
    from .i18n import normalize

    return normalize(config.get("lang")) if config.get("lang") else "ja"


def _open_ledger(args: argparse.Namespace):
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    return config, build_pmo_core(config, open_ledger(config, base), base=base)


def cmd_pmo(args: argparse.Namespace) -> int:
    """PMO Core のブリーフィングを表示する / show the PMO Core briefing."""
    from .pmo_core import scope_briefing

    try:
        _, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    briefing = core.cycle()
    briefing = localize_briefing(core.lang, briefing)       # 警告・担当の理由などを、設定の言語で
    if args.project:
        # 周は組織全体で回し、表示だけをプロジェクトに絞る。
        # The cycle runs over everything; only what is shown is narrowed.
        wanted = {args.project.lower()}
        briefing = scope_briefing(
            briefing, core.task_engine.ranked(project=args.project), wanted,
            redact_org=False)
    if args.json:
        print(json.dumps(briefing, ensure_ascii=False, indent=2))
        return 0

    print(f"全体: {briefing['overall_level']}   未完了 {briefing['active_count']} 件")
    if len(briefing.get("projects", [])) > 1:
        print("プロジェクト / projects: " + ", ".join(
            f"{p['project']}({p['level']}, {p['active_count']}件)"
            for p in briefing["projects"]))
    print("\n優先順位 / top priorities")
    for i, item in enumerate(briefing["top_priorities"], 1):
        print(f"  {i}. [{item['score']:>3}] {item['title']}"
              f"  ({item['assignee'] or '担当未定'}, 期限 {item['due_date'] or '-'})")
    print(f"\n警告 / alerts ({len(briefing['alerts'])})")
    for alert in briefing["alerts"]:
        print(f"  [{alert['severity']}] {alert['title']} — {alert['message']}")
    print(f"\n担当の提案 / assignment proposals ({len(briefing['assignment_proposals'])})")
    for p in briefing["assignment_proposals"]:
        print(f"  {p['title']} → {p['assignee']}  ({p['reason']})")
    for m in briefing["overloaded_members"]:
        print(f"  ! {m['member']} は上限超過 {m['load']}/{m['capacity']}")
    if briefing["unassignable"]:
        print(f"  ! 割り当て先の空きが無い: {len(briefing['unassignable'])} 件")
    collection = briefing.get("collection")
    if collection:
        problems = sum(1 for s in collection["sources"] if s["error"]) + collection["failed"]
        print(f"\n進捗の収集 / collection ({collection['at'][:16]})  "
              f"再読み込み {collection['refreshed']} 件・完了と分かった "
              f"{collection['completed']} 件" + (f"・問題 {problems} 件" if problems else ""))
    pending = (briefing.get("generated") or {}).get("pending") or []
    if pending:
        print(f"\n承認待ちの提案 / pending proposals ({len(pending)})  — aipmo generated")
        for item in pending[:5]:
            print(f"  {item['title']}")
    drift = briefing.get("wbs_drift")
    if drift and (drift["created"] or drift["withdrawn"] or drift["error"] or drift["found"]):
        print(f"\nWBS の更新漏れ・証拠の欠け / WBS drift  (見つかった {drift['found']} 件"
              f"・新たに提案 {len(drift['created'])} 件・取り下げ {len(drift['withdrawn'])} 件)"
              + (f"  ! {drift['error']}" if drift["error"] else "") + "  — aipmo generated")
    filing = briefing.get("filing")
    if filing and (filing["pending"] or filing["filed_now"] or filing["failed_now"]):
        print(f"\n{filing['tracker']} への起票待ち / waiting to be filed ({len(filing['pending'])})"
              f"  — aipmo file")
        for item in filing["pending"][:5]:
            print(f"  {item['title']}" + (f"  ! {item['error']}" if item.get("error") else ""))
        for item in filing["filed_now"]:
            print(f"  → 起票しました / filed: {item['id']} = {item['key']}")
    if briefing["responses"]:
        print("\n高リスク時の応答 / responses")
        for r in briefing["responses"]:
            print(f"  {r['id']:<20} {r['template']:<24} {r['status']}")
    learned = briefing.get("learning")
    pace = (learned or {}).get("pace") or {}
    if learned and (learned["member_factor"] or learned["label_bonus"]
                    or learned.get("priority_delta") or pace.get("team")):
        print(f"\n学習した補正 / learned adjustments (実績 {learned['samples']} 件)")
        for who, factor in learned["member_factor"].items():
            print(f"  {who}: キャパシティ x{factor}")
        for label, bonus in learned["label_bonus"].items():
            print(f"  ラベル {label}: 加点 +{bonus}")
        for level, delta in (learned.get("priority_delta") or {}).items():
            print(f"  優先度 {level}: 重み {delta:+d}")
        if pace.get("team"):
            verdict = ("見積りが当たっているので順位に使う" if pace.get("reliable")
                       else "見積りの誤差が大きいので順位には使わない")
            print(f"  ペース: チーム {pace['team']:g} 日/点"
                  f"（見積り誤差の中央値 {pace['median_error']:.0%}、実績 {pace['samples']} 件）"
                  f" — {verdict}")
            for who, value in (pace.get("members") or {}).items():
                print(f"    {who}: {value:g} 日/点")
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    """担当の提案を見る／確定する / list or confirm assignment proposals."""
    try:
        config, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    core.cycle()

    if not args.ref:
        proposals = [t for t in core.task_engine.ranked(project=args.project)
                     if t.suggested_assignee]
        if not proposals:
            print("提案はありません / no proposals")
        for t in proposals:
            print(f"{t.key or t.id:<14} {task_title(core.lang, t.title, t.payload)} → {t.suggested_assignee}"
                  f"  ({suggestion_reason(core.lang, t.suggestion_reason, t.payload)})")
        return 0

    if not args.apply:
        print("確定するには --apply を付けてください "
              "/ add --apply to confirm this proposal", file=sys.stderr)
        return 1

    write = None
    if args.writeback:
        # そのタスクが載っているトラッカー（Jira・GitHub・Plane・OpenProject・
        # Azure DevOps）へ書く。宛先はタスク自身が持っている。
        # Writes to the tracker that owns the task; the task itself knows which.
        from .writeback import make_writer

        config_path = Path(args.config)
        engine = build_engine(config, config_path.resolve().parent)
        write = make_writer(engine.adapters, core.members, lookup=_lookup_assignees(config))

    try:
        task = core.accept_assignment(args.ref, write=write)
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    last = core.last_writeback or {}
    written = last.get("tracker")
    print(f"確定しました / assigned: {task.title} → {task.assignee}"
          + (f" ({written} 更新済み / {written} updated)" if written
             else " (台帳のみ / ledger only)"))
    if written and last.get("account_source") == "lookup":
        print(f"  名前から {written} のユーザーを引き当てました / resolved by name: "
              f"{last.get('resolved_as')} (id {last.get('account')})")
    return 0


def _lookup_assignees(config: dict[str, Any]) -> bool:
    """`pmo_core.lookup_assignees`（既定は真）：アカウント未設定の担当を、名前で引き当てるか。"""
    return bool((config.get("pmo_core") or {}).get("lookup_assignees", True))


def cmd_integrations(args: argparse.Namespace) -> int:
    """課題管理ツールとの接続を、読み取りだけで診断する(何も書かない)。

    `aipmo integrations`            … 設定したアダプタ全部
    `aipmo integrations jira`       … 1 つだけ

    実サービスにつなぐ前後に「どこで・なぜ止まるか」を見るためのもの（疎通 → 1 件の読み取り → 担当候補の一覧）。
    終了コードは、1 つでも失敗すれば 1。
    """
    from .probe import probe_all

    config = load_config(Path(args.config))
    engine = build_engine(config, Path(args.config).resolve().parent)
    names = set(engine.adapters.names())
    if args.name and args.name not in names:
        print(f"アダプタ {args.name} は設定されていません / not configured: {args.name}", file=sys.stderr)
        return 1
    failed = 0
    for report in probe_all(engine.adapters, args.name):
        print(f"{'OK ' if report['ok'] else 'NG '} {report['name']}"
              + ("  (担当を書き戻せる)" if report["writes_back"] else ""))
        for step in report["steps"]:
            glyph = mark("success" if step["ok"] else "failed")
            print(f"      {glyph} {step['id']:<7} {step['ms']:>5} ms  {step['detail']}"
                  + (f"  [{step['hint']}]" if step["hint"] else ""))
        failed += 0 if report["ok"] else 1
    return 1 if failed else 0


def cmd_members(args: argparse.Namespace) -> int:
    """メンバーを、トラッカーのユーザーに引き当てた結果を見る(読み取りだけ・何も書かない)。

    `aipmo members`                 … 人のメンバーごとに、Plane・OpenProject での引き当て結果
    `aipmo members --tracker plane` … 1 つのトラッカーだけ

    書き戻す前に「だれが、どのユーザーに当たるか」を確かめるためのもの。曖昧・該当なしは
    ここに出る（そのまま書き戻すと、書かずに止まる）。
    """
    from .identity import AssigneeResolver

    try:
        config, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    humans = [m for m in core.members if not m.is_agent]
    if not humans:
        print("メンバーがいません / no members")
        return 0
    engine = build_engine(config, Path(args.config).resolve().parent)
    resolver = AssigneeResolver(engine.adapters)
    trackers = [t for t in ("plane", "openproject") if resolver.can_resolve(t)]
    if args.tracker:
        trackers = [t for t in trackers if t == args.tracker]
    if not trackers:
        print("引き当てできるトラッカー（plane / openproject）のアダプタが設定されていません "
              "/ no plane/openproject adapter is configured")
        return 0
    failed = 0
    for tracker in trackers:
        print(f"{tracker}")
        for member in humans:
            configured = member.account(tracker)
            if configured:
                print(f"  {member.name:<14} 設定済み / configured: {configured}")
                continue
            try:
                found = resolver.resolve(tracker, member.name, member.email)
            except Exception as exc:                      # noqa: BLE001
                failed += 1
                print(f"  {member.name:<14} ! 候補を取得できません / cannot list: "
                      f"{type(exc).__name__}: {exc}")
                continue
            if found.ok and found.person is not None:
                print(f"  {member.name:<14} → {found.person.label()} (id {found.id}, {found.tier} 一致)")
            elif found.status == "ambiguous":
                failed += 1
                print(f"  {member.name:<14} ! 複数に当たる / ambiguous: "
                      + ", ".join(p.label() for p in found.candidates[:5])
                      + "  — accounts か email で決める")
            else:
                failed += 1
                print(f"  {member.name:<14} ! 該当なし / not found  — accounts に書く")
    return 1 if failed else 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """横断の優先順位を表示する / show the cross-template ranking."""
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    try:
        task_engine = open_ledger(config, base)
        core = build_pmo_core(config, task_engine, base=base)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    # 再採点は台帳に保存される。学習した補正を読み込んでから行う。
    # Re-scoring is saved to the ledger, so what was learned is loaded first.
    core.apply_learning()
    task_engine.refresh()
    ranked = task_engine.ranked(assignee=args.assignee, limit=args.limit,
                                project=args.project)
    if not ranked:
        print("タスクはありません / no tasks. "
              "(`aipmo schedule` が走ると集まります / gathered while the scheduler runs)")
        return 0
    lang = core.lang
    for position, task in enumerate(ranked, 1):
        who = task.assignee or "-"
        due = task.due_date or "-"
        if not task.assignee and task.suggested_assignee:
            who = f"担当未定→提案 {task.suggested_assignee}"
        where = f"{task.project}, " if task.project and not args.project else ""
        print(f"{position:>3}. [{task.score:>3}] {task.key or '':<10} {task_title(lang, task.title, task.payload)}"
              f"  ({where}{who}, 期限 {due}, {', '.join(task.templates)})")
        if args.why:
            for reason in _why_lines(task, task_engine, lang):
                print(f"        - {reason}")
    return 0


def cmd_judgment(args: argparse.Namespace) -> int:
    """PMO Core の自律的な判断を見る／止める・戻す。

    `aipmo judgment`         … いまの診断、自律度、承認待ち、直近の判断
    `aipmo judgment pause`   … 一時停止(診断だけ行い、何も実行しない)
    `aipmo judgment resume`  … 再開
    `aipmo judgment reset`   … 遮断器を戻す(自動実行を再び許す)
    """
    from datetime import datetime, timezone

    from .judgment import LABEL, write_control

    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    command = args.judgment_command or "status"
    if command in ("pause", "resume", "reset"):
        try:
            path = open_ledger(config, base).side      # 台帳の設定どおりの置き場に書く
        except ConfigError as exc:
            print(f"設定エラー / config error: {exc}", file=sys.stderr)
            return 1
        stamp = datetime.now(timezone.utc).isoformat()
        if command == "pause":
            write_control(path, paused=True, paused_at=stamp)
            print("一時停止しました。常駐は診断だけを行い、何も実行しません "
                  "/ paused: it diagnoses and executes nothing")
        elif command == "resume":
            write_control(path, paused=False, resumed_at=stamp)
            print("再開しました / resumed")
        else:
            write_control(path, reset_at=stamp)
            print("遮断器を戻しました。次の周から自動実行が再び許されます "
                  "/ circuit breaker reset")
        print("(常駐が次の周で読みます / the resident process reads this on its next cycle)")
        return 0

    try:
        _, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    if core.judgment is None:
        print("自律的な判断は設定されていません。config.yaml に pmo_core.judgment を書きます "
              "/ judgment is not configured: add pmo_core.judgment")
        return 0
    judgment = localize_briefing(core.lang, {"judgment": core.cycle().get("judgment") or {}})["judgment"]
    flags = ("  [一時停止中]" if judgment.get("paused") else "") + \
            ("  [遮断器が作動中: 自動は提案に落ちています]" if judgment.get("tripped") else "")
    print(f"自律的な判断 / autonomous judgment{flags}")
    print("自律度: " + "  ".join(f"{LABEL[r]}={level}" for r, level in judgment["autonomy"].items()))
    diagnoses = judgment["diagnoses"]
    print(f"\n診断 / diagnoses ({len(diagnoses)})")
    for item in diagnoses:
        print(f"  [{item['severity']:>3}] {item['title']}")
        for line in item["evidence"][:3]:
            print(f"        - {line}")
    print(f"\n承認待ちの判断 / awaiting approval: {judgment['pending']} 件"
          + ("  — aipmo generated" if judgment["pending"] else ""))
    print("\n直近の判断 / recent judgments")
    for item in judgment["recent"]:
        print(f"  [{item['state'] or '-':<9}]{'[auto]' if item['auto'] else '      '} {item['title']}")
        result = item.get("result")
        if result and result.get("error"):
            print(f"        ! {result['error']}")
    if not core.acting:
        print("\n(表示専用: 実行するのは常駐の aipmo schedule です)")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """課題管理ツールの今の状態を、いま台帳へ集める(読み取り専用)。

    設定の `pmo_core.collect` の収集元を走査し、走査に現れなかった未完了タスクを
    1 件ずつ読み直す。閉じられた課題はここで完了と分かり、実績に残る。
    """
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    try:
        engine = build_engine(config, base_dir=base)
        core = attach_task_engine(engine, config, base, default=True, launch=True)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    if core is None or core.collector is None:
        print("収集が設定されていません。config.yaml に pmo_core.collect を書きます "
              "/ collection is not configured: add pmo_core.collect", file=sys.stderr)
        return 1
    report = core.collect_now()
    if report.get("error"):
        print(f"収集に失敗しました / failed: {report['error']}", file=sys.stderr)
        return 1
    for source in report["sources"]:
        status = (f"! {source['error']}" if source["error"]
                  else f"{source['items']} 件（新規 {source['new']}）")
        print(f"  {source['id']:<18} {source['adapter']:<16} {status}")
    print(f"再読み込み {report['refreshed']} 件、失敗 {report['failed']} 件、"
          f"見つからない {len(report['missing'])} 件、"
          f"新たに完了と分かった {report['completed']} 件")
    for line in (report.get("errors") or [])[:5]:
        print(f"  ! {line}", file=sys.stderr)
    return 1 if report["failed"] or any(s["error"] for s in report["sources"]) else 0


def cmd_file(args: argparse.Namespace) -> int:
    """承認したタスクを、課題管理ツールにも起票する(承認つき)。

    `aipmo file`                    … 起票待ちの一覧(何も書かない)
    `aipmo file REF --apply`        … そのタスクを起票する
    `aipmo file --all --apply`      … 起票待ちを全部起票する
    `aipmo file REF --skip`         … 起票を見送る(台帳だけで使う)
    """
    from .filing import FilingError, make_filer

    try:
        config, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    if core.filing is None:
        print("起票は設定されていません。config.yaml に pmo_core.filing を書きます "
              "/ filing is not configured: add pmo_core.filing")
        return 0
    cfg = core.filing

    if args.ref and args.skip:
        try:
            task = core.decline_filing(args.ref)
        except (KeyError, ValueError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        print(f"起票を見送りました / skipped: {task.title}")
        return 0

    if args.ref and not args.apply:
        print("起票するには --apply を付けてください（見送るなら --skip） "
              "/ add --apply to file this task (or --skip)", file=sys.stderr)
        return 1
    candidates = core.filing_candidates()
    if not args.apply:
        print(f"{cfg.tracker} への起票待ち / waiting to be filed ({len(candidates)})"
              + (f"   自動: {', '.join(cfg.auto)}" if cfg.auto else ""))
        for task in candidates:
            state = (task.payload.get("filing") or {}).get("state")
            print(f"  {task.id}\n      {task.title}  [{task.origin}, {task.priority or '-'}, "
                  f"期限 {task.due_date or '-'}, 担当 {task.assignee or '未定'}]"
                  + ("  ! 前回失敗: " + str(task.payload["filing"].get("error"))
                     if state == "failed" else ""))
        if candidates:
            print("\n起票するには / to file: aipmo file REF --apply   (全部: --all --apply)")
        return 0

    if not args.ref and not args.all:
        print("起票するタスクの id か --all を指定してください "
              "/ give a task id or --all", file=sys.stderr)
        return 1
    targets = [t.id for t in candidates] if args.all else [args.ref]
    base = Path(args.config).resolve().parent
    engine = build_engine(config, base)
    write = make_filer(engine.adapters, core.members, cfg, lookup=_lookup_assignees(config))
    failed = 0
    for ref in targets:
        try:
            task = core.file_task(ref, file=write)
        except (KeyError, ValueError, FilingError) as exc:
            failed += 1
            print(f"✗ {ref}: {exc}", file=sys.stderr)
            continue
        info = core.last_filing or {}
        note = " (すでに作成済みの課題に結び付けました)" if info.get("reused") else ""
        if info.get("unassigned"):
            note += f" (担当 {info['unassigned']} のアカウント未設定のため担当なしで起票)"
        print(f"✓ 起票しました / filed: {task.title} → {info.get('key')}{note}")
    return 1 if failed else 0


def cmd_generated(args: argparse.Namespace) -> int:
    """PMO Core が自分で作ったタスク(提案・定期タスク)を見る/決める。

    `aipmo generated`              … 承認待ちの提案と、開いている台帳だけのタスク
    `aipmo generated approve REF`  … 提案を承認する(仕事になる)
    `aipmo generated reject REF`   … 提案を却下する(記録は残る)
    `aipmo generated done REF`     … 台帳だけのタスクを完了にする
    """
    try:
        _, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    command = args.generated_command or "list"
    if command in ("approve", "reject", "done"):
        try:
            if command == "done":
                task = core.complete_task(args.ref)
            else:
                task = core.decide_proposal(args.ref, command == "approve")
        except (KeyError, ValueError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        verb = {"approve": "承認しました", "reject": "却下しました", "done": "完了にしました"}
        print(f"{verb[command]} / {command}: {task.title}")
        return 0

    engine = core.task_engine
    pending = engine.proposals()
    own = [t for t in engine.ranked() if t.origin]
    print(f"承認待ちの提案 / pending proposals ({len(pending)})")
    for task in pending:
        print(f"  {task.id}\n      {task.title}  [{task.priority or '-'}, 期限 {task.due_date or '-'}]")
    print(f"\n開いている台帳だけのタスク / open ledger-only tasks ({len(own)})")
    for task in own:
        filed = (task.payload.get("filing") or {}).get("key")
        print(f"  [{task.score:>3}] {task.id}  {task.title}  "
              f"({task.assignee or '担当未定'}, 期限 {task.due_date or '-'})"
              + (f"  → {filed}" if filed else ""))
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    """役割AIの状況を見る／タスクをいま任せる（失敗の再試行にも）。

    `aipmo agents`            … 役割AIごとの件数と、直近の実行
    `aipmo agents run REF`    … 役割AIに割り当てられたタスクを、いま任せて結果を待つ
    `aipmo agents review`     … 人のレビューを待つ成果の一覧
    `aipmo agents review REF --accept|--reject [--note 理由] [--by 名前]`
                              … 成果を人が確かめた記録を台帳に残す(差し戻しには理由が要る)
    """
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent

    if args.agents_command == "review":
        try:
            _, core = _open_ledger(args)
        except ConfigError as exc:
            print(f"設定エラー / config error: {exc}", file=sys.stderr)
            return 1
        if not args.ref:
            pending = core.reviews_pending()
            print(f"レビュー待ちの成果 / results awaiting review ({len(pending)})")
            for item in pending:
                print(f"  {item['task']}  [{item['agent']}]  {item['title'][:50]}")
                if item.get("excerpt"):
                    print("      " + str(item["excerpt"]).splitlines()[0][:100])
            if pending:
                print("\n確かめたら / after checking: aipmo agents review REF --accept   "
                      "または / or  --reject --note 理由")
            return 0
        if bool(args.accept) == bool(args.reject):
            print("--accept か --reject のどちらか一つを指定してください "
                  "/ give exactly one of --accept / --reject", file=sys.stderr)
            return 1
        import getpass

        try:
            review = core.review_dispatch(
                args.ref, "accepted" if args.accept else "rejected",
                args.by or getpass.getuser(), args.note or "", args.dispatch)
        except (KeyError, ValueError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        verb = "認めました / accepted" if args.accept else "差し戻しました / sent back"
        print(f"{verb}: {review['task']}  [{review['agent']}]  by {review['by']}"
              + ("  (前のレビューを上書き / overwrote the earlier review)"
                 if review.get("previous") else ""))
        return 0

    if args.agents_command == "run":
        # 実行するので、常駐のときと同じ形でエンジンを組み立てる。
        # Running one needs a real engine, built as the resident process builds it.
        try:
            engine = build_engine(config, base_dir=base)
            core = attach_task_engine(engine, config, base, default=True, launch=True)
        except ConfigError as exc:
            print(f"設定エラー / config error: {exc}", file=sys.stderr)
            return 1
        if core is None:
            print("台帳が無効です / the ledger is disabled", file=sys.stderr)
            return 1
        try:
            outcome = core.dispatch_now(args.ref)
        except (KeyError, ValueError, RuntimeError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        latest = outcome.get("latest") or {}
        print(f"{outcome['status']}: {outcome['task']} → {outcome['agent']}"
              + (f"（{outcome['reason']}）" if outcome.get("reason") else ""))
        if latest.get("status") == "done":
            print(f"完了 / done  run={latest.get('run_id')}")
            if latest.get("excerpt"):
                print("\n" + latest["excerpt"])
        elif latest.get("status") in ("failed", "skipped"):
            print(f"{latest['status']}: {latest.get('error')}", file=sys.stderr)
            return 1
        return 0

    try:
        _, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    agents = [m for m in core.members if m.is_agent]
    if not agents:
        print("役割AIはありません。config.yaml の pmo_core.members に kind: agent の"
              "メンバーを書きます / no role AIs: add members with `kind: agent`")
        return 0
    briefing = core.cycle()
    print("役割AI / role AIs")
    for item in briefing["agents"]:
        print(f"  {item['member']:<14} {item['template']:<18} 割当 {item['open_assigned']}"
              f"（実行中 {item['running']}・待ち {item['waiting']}）"
              f"  今日 {item['dispatched_today']} 件  上限 {item['capacity']}"
              + ("  自動確定" if item["auto_confirm"] else "")
              + f"  レビュー: 認めた {item['accepted']}・差し戻し {item['rejected']}"
              f"・待ち {item['awaiting_review']}")
    runs = briefing["agent_runs"]
    print(f"\n直近の実行 / recent runs ({len(runs)})")
    for run in runs:
        verdict = (run.get("review") or {}).get("decision")
        print(f"  [{run['status']:<9}] {run['agent']:<12} {run['title'][:40]}"
              + (f"  ({'認めた' if verdict == 'accepted' else '差し戻し'}: "
                 f"{run['review']['by']})" if verdict else ""))
        if run.get("error"):
            print(f"      ! {run['error']}")
        elif run.get("excerpt"):
            print("      " + run["excerpt"].splitlines()[0][:100])
    return 0


def cmd_wbs(args: argparse.Namespace) -> int:
    """WBS ファイルを検証する／状況を見る / validate the WBS file or show its status.

    PMO AI 自身の開発を管理する `wbs/aipmo.yaml` が対象（docs/SELF-WBS.md）。
    CI でも使う: error があれば終了コード 1。
    """
    from datetime import date

    from .wbs import WbsError, analyse, load_wbs

    if args.wbs_command == "proposals":
        return cmd_wbs_proposals(args)
    if args.wbs_command == "notify":
        return cmd_wbs_notify(args)

    root = Path(args.root).resolve() if args.root else Path.cwd()
    target = (root / args.file).resolve()
    try:
        wbs, problems = load_wbs(target)
    except WbsError as exc:
        print(f"WBS を読めません / cannot load the WBS: {exc}", file=sys.stderr)
        return 1
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    except ValueError:
        print(f"--as-of は YYYY-MM-DD で / bad date: {args.as_of}", file=sys.stderr)
        return 1
    analysis = analyse(wbs, root, as_of, problems)

    if args.wbs_command == "status" and args.json:
        print(json.dumps({k: v for k, v in analysis.items() if k not in ("tasks", "items")},
                         ensure_ascii=False, indent=2))
    elif args.wbs_command == "status":
        print(analysis["summary_text"].replace("*", ""))
    else:
        for p in analysis["problems"]:
            print(f"{p['level']:<7} {p['node'] or '-':<8} {p['code']}: {p['message']}")
        s = analysis["summary"]
        print(f"{s['done']}/{s['leaves']} 件完了、error {analysis['error_count']}、"
              f"warning {analysis['warning_count']}  ({target.name})")

    failed = analysis["error_count"] > 0 or (
        args.wbs_command == "check" and args.strict and analysis["warning_count"] > 0)
    return 1 if failed else 0


def cmd_wbs_notify(args: argparse.Namespace) -> int:
    """WBS の更新漏れ・証拠の欠けを、PR のコメントにする（既定は本文を表示するだけ）。

    `aipmo wbs notify --base origin/main`               … 本文を表示（何も書かない）
    `aipmo wbs notify --base origin/main --pr 12 --post` … PR #12 の目印つきコメントを作る／更新する
    """
    from datetime import date

    from .wbs import WbsError, load_wbs
    from .wbs_notify import GitHubComments, NotifyError, changed_files, collect, render

    root = Path(args.root).resolve() if args.root else Path.cwd()
    target = (root / args.file).resolve()
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    except ValueError:
        print(f"--as-of は YYYY-MM-DD で / bad date: {args.as_of}", file=sys.stderr)
        return 1
    try:
        wbs, _ = load_wbs(target)
        changed: list[str] | None = None
        if args.changed:
            changed = list(args.changed)
        elif args.base:
            changed = changed_files(args.base, root)
        findings = collect(wbs, root, as_of, changed)
        try:
            shown = str(target.relative_to(root)).replace("\\", "/")
        except ValueError:
            shown = target.name
        body = render(findings, file=shown, as_of=as_of, changed_known=changed is not None)
        if not args.post:
            print(body)
            print(f"\n（表示だけです。PR に書くには --pr 番号 --post / dry run: {len(findings)} 件）",
                  file=sys.stderr)
            return 0
        if not args.pr:
            print("--post には --pr（PR の番号）が要ります / --post needs --pr", file=sys.stderr)
            return 1
        client = GitHubComments(
            args.repo or os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_API_URL", "https://api.github.com"), author=args.author)
        result = client.post_or_update(args.pr, body, has_findings=bool(findings))
    except (WbsError, NotifyError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    said = {"created": "コメントを作りました", "updated": "コメントを更新しました",
            "unchanged": "コメントは更新の必要がありません",
            "skipped": "コメントは投稿しませんでした（指摘なし）"}[result["action"]]
    print(f"PR #{args.pr}: {said}（{len(findings)} 件）"
          + (f" {result['url']}" if result.get("url") else ""))
    return 0


def cmd_wbs_proposals(args: argparse.Namespace) -> int:
    """承認待ちの WBS 変更提案（wbs_replan）を見る／承認して WBS ファイルへ反映する。

    `aipmo wbs proposals`                  … 承認待ちの一覧（反映できる形かも表示）
    `aipmo wbs proposals show ID`          … 提案の中身と、WBS ファイルがどう変わるか（書かない）
    `aipmo wbs proposals approve ID`       … 承認して WBS ファイルへ反映する（人の確定した操作）
    `aipmo wbs proposals reject ID`        … 却下する（ファイルは変えない）
    `aipmo wbs proposals apply ID`         … 承認済みの提案を反映し直す（反映に失敗したとき）
    """
    from .wbs_edit import WbsEditError
    from .wbs_proposals import (ProposalError, Target, apply_approved, approve, changes_of,
                                decide, fetch, plan_for)

    try:
        config = load_config(Path(args.config))
        base = Path(args.config).resolve().parent
        engine = build_engine(config, base)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    tenant = str(config.get("tenant") or "")
    spec = dict((config.get("adapters") or {}).get("wbs_replan") or {})
    file = Path(args.file or spec.get("file") or "wbs/aipmo.yaml")
    root = Path(args.root or spec.get("root") or ".")
    file = file if file.is_absolute() else base / file
    root = root if root.is_absolute() else base / root
    try:
        side = open_ledger(config, base).side
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    # postgres アダプタがあればそちら、無ければ台帳（SideStore）に提案を置く
    # （LedgerProposalStore は pg.query/pg.execute と同じ形を持つので、以降の
    # 呼び出しはどちらが相手かを区別しない）。「提案の新規作成」（wbs_replan
    # テンプレート）は risk_forecast のスナップショットが別途 PostgreSQL に
    # 要るため、ここでは扱わない——ここが扱うのは既にある提案の一覧・決定・反映。
    #
    # Uses the postgres adapter when configured, otherwise the ledger's own
    # SideStore (LedgerProposalStore matches pg.query/pg.execute's shape, so
    # everything below is indifferent to which one it's talking to).
    # *Creating* a proposal (the wbs_replan template) still needs PostgreSQL
    # separately for its risk_forecast snapshot; this only covers viewing,
    # deciding, and applying proposals that already exist.
    if engine.adapters.has("postgres"):
        pg: Any = engine.adapters.get("postgres")
    else:
        from .wbs_proposals import LedgerProposalStore

        pg = LedgerProposalStore(side)
    target = Target(file=file.resolve(), root=root.resolve(), decisions=side)
    by = args.by or __import__("getpass").getuser()
    command = args.proposals_command or "list"

    try:
        if command == "list":
            rows = pg.query("pending_wbs_proposals", {"tenant": tenant})["rows"]
            print(f"承認待ちの WBS 変更提案 / pending WBS proposals ({len(rows)})")
            for row in rows:
                raw = changes_of(row)
                mark = (f"反映できる変更 {len(raw)} 件" if isinstance(raw, list)
                        else "自由な形（承認しても記録だけ）")
                label = f" [{row['option_label']}]" if row.get("option_label") else ""
                print(f"  {row['id']}  tier{row['tier']}{label}  {row.get('wbs_version_from')}"
                      f"  — {mark}\n      {(row.get('rationale') or '')[:100]}")
            if rows:
                print("\n中身を見る: aipmo wbs proposals show ID   /   承認して反映: "
                      "aipmo wbs proposals approve ID")
            return 0

        if not args.ref:
            print("提案の id を指定してください / give the proposal id", file=sys.stderr)
            return 1

        if command == "show":
            row = fetch(pg, tenant, args.ref)
            print(f"{row['id']}  状態 {row.get('status')}  tier{row['tier']}  "
                  f"対象 WBS {row.get('wbs_version_from')}")
            print(f"根拠: {row.get('rationale') or '-'}")
            plan = plan_for(row, target)
            if plan is None:
                print("\n決まった形の変更（diff.changes）が無い、自由な形の提案です。"
                      "承認しても WBS ファイルは変わりません。")
                print(json.dumps(row.get("diff"), ensure_ascii=False, indent=2)[:2000])
                return 0
            print(f"\n反映すると（{target.file.name}）:")
            for line in plan.report or ["（すでにその内容です。変わりません）"]:
                print(f"  - {line}")
            print("\n" + (plan.diff or "（差分なし）"))
            return 0

        if command == "reject":
            row = fetch(pg, tenant, args.ref)
            if row.get("status") != "pending":
                print(f"承認待ちではありません（{row.get('status')}）", file=sys.stderr)
                return 1
            decide(pg, tenant, args.ref, "rejected", by, args.note)
            print(f"却下しました / rejected: {args.ref}（WBS ファイルは変えていません）")
            return 0

        if command == "approve":
            outcome = approve(pg, tenant, args.ref, by, args.note, target)
            print(f"承認しました / approved: {args.ref}")
            if outcome["applied"] is True:
                print(f"WBS ファイルへ反映しました / applied to {target.file}:")
                for line in outcome["report"] or ["（すでにその内容でした）"]:
                    print(f"  - {line}")
                print("内容を確かめて、コミット（PR）してください。")
            elif outcome["applied"] is False:
                print(f"! 反映できませんでした: {outcome['error']}", file=sys.stderr)
                return 1
            else:
                print("（自由な形の提案なので、WBS ファイルは変えていません）")
            return 0

        done = apply_approved(pg, tenant, args.ref, by, target, force=args.force)
        print(f"反映しました / applied: {args.ref}" + ("" if done.get("changed", True)
                                                          else "（すでにその内容でした）"))
        for line in done["report"]:
            print(f"  - {line}")
        return 0
    except ProposalError as exc:
        print(f"{exc}", file=sys.stderr)
        for problem in exc.problems[1:]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    except WbsEditError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


def _knowledge_adapter(args: argparse.Namespace) -> Any | None:
    """ベクトルストア・アダプタを解決する。設定エラーなら None（呼び出し側が終了コード1にする）。"""
    try:
        config = load_config(Path(args.config))
        engine = build_engine(config)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return None
    name = args.backend or "vector_store"
    if not engine.adapters.has(name):
        hint = (
            " 論理名 vector_store は、ベクトルストアがちょうど1つ設定されているときだけ"
            "使えます。複数設定している場合は --backend でバックエンド名"
            "（qdrant・pgvector・chroma・milvus・weaviate のいずれか）を指定してください"
            if name == "vector_store" else ""
        )
        print(f"設定エラー / config error: アダプタ '{name}' が設定されていません。{hint}",
              file=sys.stderr)
        return None
    return engine.adapters.get(name)


def cmd_knowledge(args: argparse.Namespace) -> int:
    """ナレッジ公開候補のレビュー（人間の承認フロー）/ review workflow for knowledge candidates.

    `generalize_knowledge` のようなテンプレートが `vector_store.submit_candidate`
    で提出した、公開コレクションへの昇格待ちの候補を見る・直す・承認（公開）・却下する。
    承認・却下は、どちらも私有コレクションのその行に「誰が・いつ・どう判断したか」を
    書き足して残す——これが「人間の判断の記録」。

    `aipmo knowledge list`              … 承認待ちの一覧（publicability_score の降順）
    `aipmo knowledge show ID`           … 候補の中身
    `aipmo knowledge edit ID --text …`  … 承認待ちの内容を書き直す（決定済みは不可）
    `aipmo knowledge approve ID`        … 承認して公開コレクションへ複製する
    `aipmo knowledge reject ID`         … 却下する（公開コレクションには書かない）

    Reviews knowledge candidates a template like `generalize_knowledge` submitted via
    `vector_store.submit_candidate`, awaiting promotion to the public collection. Both
    approving and rejecting append who decided what and when onto the private record —
    that is the recorded human decision.
    """
    from .adapters.base import AdapterError

    adapter = _knowledge_adapter(args)
    if adapter is None:
        return 1
    command = args.knowledge_command or "list"
    by = args.by or __import__("getpass").getuser()

    try:
        if command == "list":
            items = adapter.list_candidates(status=args.status or "pending")
            label = {"pending": "承認待ち", "approved": "承認済み", "rejected": "却下済み"}.get(
                args.status or "pending", args.status)
            print(f"{label}のナレッジ候補 / knowledge candidates ({len(items)})")
            for item in items:
                payload = item["payload"]
                text = (payload.get("text") or "")[:80]
                print(f"  {item['id']}  score={payload.get('publicability_score', 0):.0f}"
                      f"  level={payload.get('knowledge_level', '?')}\n      {text}")
            if items:
                print("\n中身を見る: aipmo knowledge show ID   /   承認: aipmo knowledge approve ID"
                      "   /   却下: aipmo knowledge reject ID")
            return 0

        if not args.ref:
            print("候補の id を指定してください / give the candidate id", file=sys.stderr)
            return 1

        if command == "show":
            item = adapter.get_candidate(args.ref)
            if item is None:
                print(f"見つかりません / no such candidate: {args.ref}", file=sys.stderr)
                return 1
            payload = item["payload"]
            print(f"{item['id']}  状態 {payload.get('review_status')}  "
                  f"score={payload.get('publicability_score', 0):.0f}  "
                  f"level={payload.get('knowledge_level', '?')}")
            print(f"\n{payload.get('text') or ''}")
            if payload.get("publicability_reasons"):
                print("\n根拠 / reasons:")
                for reason in payload["publicability_reasons"]:
                    print(f"  - {reason}")
            if payload.get("review_status") != "pending":
                print(f"\n判断 / decision: {payload.get('reviewed_by')}"
                      f"（{payload.get('reviewed_at')}）"
                      + (f" — {payload['review_note']}" if payload.get("review_note") else ""))
            return 0

        if command == "edit":
            if not args.text:
                print("--text で新しい内容を渡してください / give the new text with --text",
                      file=sys.stderr)
                return 1
            adapter.edit_candidate(args.ref, text=args.text)
            print(f"書き直しました / edited: {args.ref}")
            return 0

        approve = command == "approve"
        adapter.decide_candidate(args.ref, approve=approve, reviewer=by, note=args.note)
        if approve:
            print(f"承認して公開コレクションへ複製しました / approved and promoted: {args.ref}")
        else:
            print(f"却下しました（公開コレクションには書いていません）/ rejected: {args.ref}")
        return 0
    except AdapterError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


def cmd_demo(args: argparse.Namespace) -> int:
    """デモ用のサンプルデータを台帳（DB）に入れる／消す／確かめる（docs/DEMO.md）。

    `aipmo --config demo/config.yaml demo load`            … 入れる（台帳が空のとき）
    `aipmo --config demo/config.yaml demo load --reset`    … デモのテナントの行を消して入れ直す
    `aipmo --config demo/config.yaml demo reset`           … デモのテナントの行を消す
    `aipmo --config demo/config.yaml demo status`          … いまの台帳の状況
    `tenant: demo` の設定でだけ動く（別のテナントには触れない）。
    """
    from . import demo

    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    try:
        if args.demo_command == "reset":
            result = demo.reset(config, base)
            print(f"デモのテナントの台帳を消しました / reset ({result['backend']}): "
                  + (", ".join(result["removed"]) or "（もともと空）"))
            return 0
        if args.demo_command == "status":
            demo._require_demo(config)
            ledger = open_ledger(config, base)
            try:
                core = build_pmo_core(config, ledger, base=base)
                summary = demo.summarize(core, core.cycle())
            finally:
                ledger.close()
        else:
            summary = demo.load(config, base, do_reset=args.reset)
    except (demo.DemoError, ConfigError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    _print_demo_summary(summary, loaded=args.demo_command == "load")
    return 0


def _print_demo_summary(s: dict[str, Any], *, loaded: bool) -> None:
    print(("デモのデータを入れました / demo data loaded" if loaded else "デモの台帳の状況 / demo status")
          + f"  [{s['store']}]")
    print(f"  タスク {s['tasks']} 件（未完了 {s['open_tasks']}）・完了実績 {s['outcomes']} 件"
          f"（学習 {s['learned_samples']} 件）")
    print(f"  警告 {s['alerts']} 件（全体のレベル: {s['level']}）")
    print(f"  承認待ちの提案 {s['proposals']} 件（警告からの対応 {s['followup_proposals']}・"
          f"WBS のずれ {s['wbs_proposals']}）・担当の提案 {s['assignment_proposals']} 件")
    print(f"  自律的な判断 {s['judgments']} 件（承認待ち {s['judgments_pending']}）")
    print(f"  役割AIの成果のレビュー待ち {s['reviews_pending']} 件・起票待ち {s['filing_pending']} 件")
    if loaded:
        print("\n次: aipmo --config demo/config.yaml pmo   /   aipmo --config demo/config.yaml serve"
              "   （操作の手順は docs/DEMO.md）")


def cmd_ledger(args: argparse.Namespace) -> int:
    """台帳の保存先を調べる／SQLite から PostgreSQL へ移す。"""
    from .ledger_store import (LedgerConfigError, LedgerTenantError, SqliteStore,
                               Snapshot)
    from .task_engine import TaskEngine

    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    try:
        target = open_ledger(config, base)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1

    if args.ledger_command == "info":
        target.sync()
        open_tasks = [t for t in target.tasks.values() if not t.done]
        print(f"保存先 / store:     {target.describe()}")
        print(f"テナント / tenant:   {target.tenant or '-'}")
        print(f"タスク / tasks:      {len(target.tasks)}（未完了 {len(open_tasks)}）")
        print(f"完了実績 / outcomes: {len(target.outcomes)}")
        print(f"プロジェクト / projects: {', '.join(target.projects()) or '-'}")
        print(f"ブリーフィング・判断ログ・状態 / side data: {target.side.describe()}")
        return 0

    if args.ledger_command == "migrate-to-sqlite":
        return _migrate_to_sqlite(args, config, base, target)

    if args.ledger_command == "side-import":
        return _import_side_files(target, ledger_path(config, base), args.from_dir,
                                  overwrite=args.force)

    # migrate
    if target.backend != "postgres":
        print("移行先が PostgreSQL ではありません。config.yaml で "
              "task_engine.backend: postgres を設定してください "
              "/ the target is not PostgreSQL: set task_engine.backend: postgres",
              file=sys.stderr)
        return 1
    source_path = TaskEngine.resolve_path(args.from_sqlite or ledger_path(config, base))
    if not source_path.exists():
        print(f"移行元が見つかりません / source not found: {source_path}", file=sys.stderr)
        return 1

    source = SqliteStore(source_path)
    try:
        source.prepare(config.get("tenant") or None, None)
        snapshot = source.read()
    except (LedgerTenantError, LedgerConfigError) as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    finally:
        source.close()

    existing = len(target._store.read().tasks)
    if existing and not args.force:
        print(f"移行先にはすでに {existing} 件のタスクがあります。同じ id の行を上書きして"
              f"よければ --force を付けてください / the target already holds {existing} "
              f"tasks; add --force to overwrite rows with the same id", file=sys.stderr)
        return 1

    added = _copy_ledger_rows(snapshot, target._store)
    moved = Snapshot(tasks=snapshot.tasks, outcomes=snapshot.outcomes)
    print(f"移行しました / migrated: {len(moved.tasks)} tasks, "
          f"{added} outcomes  {source_path} -> {target.describe()}")
    print("移行元のファイルは残してあります（確認後に削除してください）"
          " / the source file is left in place; delete it once you have checked")
    # 台帳の隣のファイル（ブリーフィング・判断ログ・状態）も、置き場がデータベースなら一緒に移す。
    # The files beside the ledger go too, when they live in the database.
    if target.side.kind == "database":
        _import_side_files(target, source_path, None, overwrite=False)
    return 0


def _canonical(raw: str) -> str:
    """JSON 文字列を、保存先による書式の違い（PostgreSQL の jsonb は整形し直す）に依らない形にする。"""
    try:
        return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return raw


def _copy_ledger_rows(snapshot: Any, destination: Any) -> int:
    """台帳の行を移行先へ写す。同じ id のタスクは上書き、完了実績は**まだ無いものだけ**足す
    （`--force` でやり直しても、実績が二重にならない）。足した実績の件数を返す。"""
    from .task_engine import MAX_OUTCOMES

    known = {_canonical(o) for o in destination.read().outcomes}
    fresh = [o for o in snapshot.outcomes if _canonical(o) not in known]
    with destination.write() as tx:
        tx.apply(snapshot.tasks, [], fresh, MAX_OUTCOMES)
    return len(fresh)


def _verify_copy(snapshot: Any, destination: Any) -> list[str]:
    """写した結果を読み直して、移行元と同じか確かめる。違いがあれば、その説明。"""
    from .task_engine import MAX_OUTCOMES

    copied = destination.read()
    problems = []
    for task_id, raw in snapshot.tasks.items():
        got = copied.tasks.get(task_id)
        if got is None:
            problems.append(f"タスク {task_id} が移行先にありません")
        elif _canonical(got) != _canonical(raw):
            problems.append(f"タスク {task_id} の中身が違います")
    want = [_canonical(o) for o in snapshot.outcomes][-MAX_OUTCOMES:]
    have = {_canonical(o) for o in copied.outcomes}
    missing = [o for o in want if o not in have]
    if missing:
        problems.append(f"完了実績が {len(missing)} 件、移行先にありません")
    return problems


def _migrate_to_sqlite(args: argparse.Namespace, config: dict[str, Any], base: Path,
                       source: Any) -> int:
    """PostgreSQL の台帳を、SQLite のファイルへ写す（`migrate` の逆向き）。

    設定が指す台帳（PostgreSQL の、この `tenant` の行）が移行元。移行先は `--to`（既定は設定の
    台帳ファイルの場所）。写したあと読み直して、移行元と同じか確かめる。移行元は消さない。
    台帳の隣に置くもの（ブリーフィング・判断ログ・状態）がデータベースにあれば、移行先の
    ファイル（`--side file`、既定）か SQLite の表（`--side database`）へ写す。
    """
    from .ledger_store import LedgerConfigError, LedgerTenantError, SqliteStore
    from .side_store import DbSide, FileSide, import_files
    from .task_engine import TaskEngine

    if source.backend != "postgres":
        print("移行元が PostgreSQL ではありません。config.yaml の task_engine.backend: postgres の台帳を"
              "SQLite へ写します / the source is not PostgreSQL: this copies a PostgreSQL ledger "
              "to SQLite", file=sys.stderr)
        return 1
    dest_path = TaskEngine.resolve_path(args.to or ledger_path(config, base))
    tenant = config.get("tenant") or None
    destination = SqliteStore(dest_path)
    try:
        try:
            destination.prepare(tenant, None)
        except (LedgerTenantError, LedgerConfigError) as exc:
            print(f"設定エラー / config error: {exc}", file=sys.stderr)
            return 1
        existing = len(destination.read().tasks)
        if existing and not args.force:
            print(f"移行先 {dest_path} にはすでに {existing} 件のタスクがあります。同じ id の行を上書き"
                  f"してよければ --force を付けてください / the target already holds {existing} "
                  f"tasks; add --force to overwrite rows with the same id", file=sys.stderr)
            return 1
        snapshot = source._store.read()
        added = _copy_ledger_rows(snapshot, destination)
        problems = _verify_copy(snapshot, destination)
        if problems:
            print("! 写した結果が移行元と一致しません / the copy does not match the source:",
                  file=sys.stderr)
            for problem in problems[:10]:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(f"移行しました / migrated: {len(snapshot.tasks)} tasks, {added} outcomes  "
              f"{source.describe()} -> sqlite:{dest_path}（読み直して一致を確認 / verified）")

        if source.side.kind == "database":
            side_target = (DbSide(destination) if args.side == "database" else FileSide(dest_path))
            report = import_files(source.side, side_target, overwrite=args.force)
            words = {"imported": "写しました", "kept": "移行先にあるので残しました（上書きは --force）",
                     "absent": "移行元にありません"}
            print(f"台帳の隣に置くもの / beside the ledger: {source.side.describe()} -> "
                  f"{side_target.describe()}")
            for name, state in report.items():
                print(f"  {name:<28} {words[state]}")
        else:
            print("台帳の隣に置くものはファイルのままです（移す必要はありません）。")
        print("移行元の PostgreSQL の行は残してあります。SQLite を使うには config.yaml の "
              "task_engine.backend を sqlite にしてください（確認後に PostgreSQL 側を削除） "
              "/ the PostgreSQL rows are left; set task_engine.backend: sqlite to use the copy")
        return 0
    finally:
        destination.close()


def _import_side_files(target: Any, ledger_file: Path, from_dir: str | None, *,
                       overwrite: bool) -> int:
    """ローカルのファイル（台帳の隣）を、台帳の置き場（データベース）へ取り込む。移行元は消さない。"""
    from .side_store import FileSide, import_files

    if target.side.kind != "database":
        print("取り込み先が データベース ではありません。config.yaml の task_engine.side_storage を "
              "database にするか、PostgreSQL の台帳を使ってください "
              "/ the target is not the database: set task_engine.side_storage: database",
              file=sys.stderr)
        return 1
    source = FileSide(Path(from_dir) / Path(ledger_file).name if from_dir else ledger_file)
    report = import_files(source, target.side, overwrite=overwrite)
    words = {"imported": "取り込みました", "kept": "移行先にあるので残しました（上書きは --force）",
             "absent": "移行元にありません"}
    print(f"台帳の隣のファイル / files beside the ledger: {source.describe()} -> "
          f"{target.side.describe()}")
    for name, state in report.items():
        print(f"  {name:<28} {words[state]}")
    print("移行元のファイルは残してあります（確認後に削除してください）")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_wizard import SetupError

    try:
        written = run_interactive(Path(args.dir))
    except SetupError as exc:
        print(f"セットアップエラー / setup error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n中止しました / Cancelled.", file=sys.stderr)
        return 1
    return 0 if written else 1


def resolve_cors_origins(web_config: dict[str, Any],
                          env_value: str | None) -> list[str]:
    """web.cors_origins の実効値を決める / resolve the effective CORS origins.

    環境変数はカンマ区切りで、config.yaml の web.cors_origins（リスト）より
    優先する——コンテナ経由の展開では、資格情報と同じ経路（環境変数）で
    渡す方が一貫するため。環境変数が設定されていれば（空文字列でも）、
    config 側の値は完全に無視する。

    The env var is comma-separated and takes priority over config.yaml's
    web.cors_origins (a list) — in a container deployment, this travels the
    same path (environment variables) as everything else that isn't checked
    in. When the env var is set at all (even to an empty string), it fully
    overrides config, rather than merging with it.
    """
    if env_value is not None:
        return [origin.strip() for origin in env_value.split(",") if origin.strip()]
    return list(web_config.get("cors_origins") or [])


def cmd_serve(args: argparse.Namespace) -> int:
    """スマホ向け Web 画面を起動する / start the mobile web interface."""
    try:
        import uvicorn

        from .web.server import create_app, generate_token
    except ImportError:
        print("Web 画面には追加の導入が必要です / the web interface needs extra packages:\n"
              '  pip install "aipmo[web]"', file=sys.stderr)
        return 1

    from .i18n import translator
    from .pmo_core import load_members

    config = load_config(Path(args.config))
    web = dict(config.get("web") or {})

    host = args.host or web.get("host", "127.0.0.1")
    port = args.port or int(web.get("port", 8765))
    # トークンは config ではなく環境変数か自動生成から取る。
    # config.yaml は共有される前提なので、そこに常設の鍵を置かせない。
    # The token comes from the environment or is generated. config.yaml gets
    # shared, so it is not a place to park a standing credential.
    token = os.environ.get("AIPMO_WEB_TOKEN") or generate_token()
    # 閲覧用は既定で発行する。必要になってから作ろうとすると、
    # そのときには実行用を配ってしまっている。
    # Issued by default: leaving it until it is needed means the operator token
    # has already been handed out by then.
    viewer_token = os.environ.get("AIPMO_VIEWER_TOKEN") or generate_token()

    cors_origins = resolve_cors_origins(web, os.environ.get("AIPMO_CORS_ORIGINS"))

    base = Path(args.config).resolve().parent
    engine = build_engine(config)
    try:
        attach_task_engine(engine, config, base, default=False)
        store_factory = ledger_store_factory(config)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    # 相対パスは設定ファイルのあるディレクトリから見る（schedule・PMO Core と同じ）。
    # Relative paths are taken from the config's directory, like schedule and the PMO Core.
    template_root = Path(web.get("templates_dir", "templates"))
    template_root = (template_root if template_root.is_absolute() else base / template_root).resolve()
    try:
        pool_settings = web_pool_settings(web)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    app = create_app(engine, template_root, token, viewer_token=viewer_token,
                     tenant=config.get("tenant", ""), lang=config.get("lang"),
                     cors_origins=cors_origins or None,
                     pmo_ledger=ledger_path(config, base),
                     viewer_projects=web.get("viewer_projects"),
                     ledger_store_factory=store_factory,
                     members=load_members((config.get("pmo_core") or {}).get("members")),
                     filing=_web_filing(config),
                     lookup_assignees=_lookup_assignees(config),
                     wbs_target=_wbs_target(config, base),
                     wbs_view=_wbs_view(config, base),
                     judgment=_judgment_config(config),
                     side_storage=side_storage_mode(config),
                     **pool_settings)

    t = translator(config.get("lang"))
    shown = host if host not in ("0.0.0.0", "::") else _lan_address()

    print()
    print(f"  {t('serve_ready')}")
    print(f"    {t('role_operator')}")
    print(f"      http://{shown}:{port}/?token={token}")
    print(f"    {t('role_viewer')}")
    print(f"      http://{shown}:{port}/?token={viewer_token}")
    print()
    if host in ("127.0.0.1", "localhost", "::1"):
        print(f"  ! {t('serve_local_only')}")
        print("    aipmo serve --host 0.0.0.0")
    else:
        print(f"  ! {t('serve_exposed')}")
    print()

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _lan_address() -> str:
    """LAN 側のアドレスを推定する / best guess at the LAN address.

    スマホから開く URL を人手で調べさせないため。接続はしない。
    Saves the user from hunting for their own IP. No traffic is sent.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 1))   # TEST-NET-1: 到達しない / never routed
        return sock.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        sock.close()


def cmd_schedule(args: argparse.Namespace) -> int:
    """定時実行を開始する / start the scheduler."""
    from .engine.scheduler import Scheduler, State, discover_jobs

    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    engine = build_engine(config, base_dir=base)
    try:
        task_engine = attach_task_engine(engine, config, base, default=True,
                                         launch=True)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1

    web = dict(config.get("web") or {})
    root = Path(web.get("templates_dir", "templates"))
    if not root.is_absolute():
        root = base / root

    jobs, problems = discover_jobs(root)

    for problem in problems:
        # 起動しないテンプレートを黙って捨てない。
        # 動かない理由が分からないのが一番困る。
        # Never drop a template silently: not knowing why something does not
        # run is the worst outcome.
        print(f"!  {problem}", file=sys.stderr)

    if not jobs:
        print("定時起動のテンプレートがありません "
              "/ no templates declare a schedule.\n"
              '  trigger: "schedule:0 9 * * MON-FRI" のように書きます。',
              file=sys.stderr)
        return 1

    state_path = Path(config.get("state_file", base / "scheduler-state.json"))
    scheduler = Scheduler(engine, jobs, State.load(state_path),
                          task_engine=task_engine)

    if args.list:
        for job in scheduler.jobs:
            when = (job.next_run.astimezone(job.tz()).strftime("%Y-%m-%d %H:%M %Z")
                    if job.next_run else "なし / never")
            print(f"{job.name:<28} {when}   {job.cron_expression}")
        return 0

    if args.once:
        for result in scheduler.tick():
            print(f"{result['status']:<18} {result['job']}")
        return 0

    logging.getLogger("aipmo.scheduler").setLevel(logging.INFO)
    scheduler.run_forever(interval=args.interval)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """接続確認 / connection check."""
    config_path = Path(args.config)
    engine = build_engine(load_config(config_path), config_path.parent)
    ok = True
    for name in engine.adapters.names():
        healthy = engine.adapters.get(name).health_check()
        print(f"{mark('success' if healthy else 'failed')} {name}")
        ok = ok and healthy
    return 0 if ok else 1


def cmd_validate(args: argparse.Namespace) -> int:
    ok = True
    for path in args.paths:
        try:
            template = loader.load_file(path)
        except loader.TemplateError as exc:
            print(f"NG  {path}\n    {exc}")
            ok = False
            continue
        print(f"OK  {path}  [{template.industry}] ステップ {len(template.steps)} 件")
    return 0 if ok else 1


def _confirm_agent_write(tool: str, arguments: dict[str, Any]) -> bool:
    """対話端末で承認を求める / ask for approval at an interactive terminal.

    `aipmo run` の既定の承認方法。標準入力が対話端末でないとき
    （スクリプト・CI・パイプ）はそもそも呼ばれない — `cmd_run` 側で
    先に判定している。ここでの EOFError は、それでも対話できなかった
    場合の保険で、承認できないのだから断る。

    The default approval path for `aipmo run`. Not called at all when stdin
    is not an interactive terminal (a script, CI, a pipe) — `cmd_run` checks
    that first. Catching EOFError here is a fallback for the rare case where
    it turns out not to be interactive after all: with no way to ask, the
    write is refused.
    """
    print(f"\n[承認が必要 / approval needed] {tool}")
    print(json.dumps(arguments, ensure_ascii=False, indent=2, default=str))
    try:
        answer = input("実行してよいですか？ / proceed? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    # 対話端末があるときだけ、その場で承認を求める。スケジューラや CI
    # からの呼び出しには対話端末が無く、input() が固まるかすぐ落ちる
    # ので渡さない — その場合、承認が要る書き込みは常に断られる。
    #
    # Only offer an interactive approval prompt when stdin is actually a
    # terminal. A scheduler or CI invocation has none, and input() there
    # would either hang or fail immediately, so none is passed — writes that
    # require approval are simply refused in that case.
    approve = _confirm_agent_write if sys.stdin.isatty() else None
    try:
        run_config = load_config(config_path)
        engine = build_engine(run_config, config_path.parent, approve=approve)
        attach_task_engine(engine, run_config, config_path.resolve().parent,
                           default=False)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1

    try:
        template = loader.load_file(args.path)
    except loader.TemplateError as exc:
        print(f"テンプレートエラー: {exc}", file=sys.stderr)
        return 1

    params = dict(kv.split("=", 1) for kv in args.param)
    trigger = json.loads(args.trigger) if args.trigger else dict(params)

    try:
        ctx = engine.run(template, params=params, trigger=trigger)
    except StepFailure as exc:
        print(f"実行失敗: {exc}", file=sys.stderr)
        return 1

    for step_id, result in ctx.results.items():
        print(f"{mark(result.status)} {step_id:<20} {result.duration_ms:>5}ms")

    if args.json:
        print(json.dumps(
            {k: v.output for k, v in ctx.results.items()},
            ensure_ascii=False, indent=2, default=str,
        ))
    return 0


def cmd_adapters(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    engine = build_engine(load_config(config_path), config_path.parent)
    for name in engine.adapters.names():
        adapter = engine.adapters.get(name)
        print(f"{name}: {', '.join(sorted(adapter.actions())) or '(アクションなし)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aipmo")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="テンプレートを検証する")
    p_validate.add_argument("paths", nargs="+")
    p_validate.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="テンプレートを実行する")
    p_run.add_argument("path")
    p_run.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    p_run.add_argument("--trigger", help="トリガーペイロード (JSON)")
    p_run.add_argument("--json", action="store_true", help="全ステップの出力を表示")
    p_run.set_defaults(func=cmd_run)

    p_adapters = sub.add_parser("adapters", help="利用可能なアダプタとアクションを表示")
    p_adapters.set_defaults(func=cmd_adapters)

    p_doctor = sub.add_parser("doctor", help="各アダプタへの接続を確認する")
    p_doctor.set_defaults(func=cmd_doctor)


    p_serve = sub.add_parser("serve", help="スマホ向け Web 画面を起動 / mobile web interface")
    p_serve.add_argument("--host", help="待ち受けアドレス / bind address")
    p_serve.add_argument("--port", type=int, help="待ち受けポート / port")
    p_serve.set_defaults(func=cmd_serve)

    p_schedule = sub.add_parser(
        "schedule", help="定時実行を開始 / start the scheduler")
    p_schedule.add_argument("--list", action="store_true",
                            help="次回時刻を表示して終了 / show next times and exit")
    p_schedule.add_argument("--once", action="store_true",
                            help="いま実行すべきものだけ実行 / run what is due, then exit")
    p_schedule.add_argument("--interval", type=float, default=20.0,
                            help="確認の間隔（秒）/ check interval in seconds")
    p_schedule.set_defaults(func=cmd_schedule)

    p_tasks = sub.add_parser(
        "tasks", help="テンプレート横断のタスク優先順位 / cross-template task ranking")
    p_tasks.add_argument("--assignee", help="担当者で絞る / filter by assignee")
    p_tasks.add_argument("--project", help="プロジェクトで絞る / filter by project")
    p_tasks.add_argument("--limit", type=int, default=20,
                         help="表示件数 / how many to show")
    p_tasks.add_argument("--why", action="store_true",
                         help="点数の内訳も表示 / show the score breakdown")
    p_tasks.set_defaults(func=cmd_tasks)

    p_pmo = sub.add_parser(
        "pmo", help="PMO Core のブリーフィング / PMO Core briefing")
    p_pmo.add_argument("--json", action="store_true", help="JSON で出力 / print as JSON")
    p_pmo.add_argument("--project", help="プロジェクトに絞って表示 / show one project only")
    p_pmo.set_defaults(func=cmd_pmo)

    p_assign = sub.add_parser(
        "assign", help="担当の提案を見る・確定する / list or confirm assignment proposals")
    p_assign.add_argument("ref", nargs="?", help="確定するタスクの Jira キー／id")
    p_assign.add_argument("--project", help="一覧をプロジェクトで絞る / filter the list by project")
    p_assign.add_argument("--apply", action="store_true",
                          help="提案を確定する（ref が必要）/ confirm the proposal for ref")
    p_assign.add_argument("--writeback", "--jira", dest="writeback", action="store_true",
                          help="確定時に、そのタスクのトラッカー（Jira・GitHub Projects・Plane・"
                               "OpenProject・Azure DevOps）の担当者も更新 "
                               "/ also update the assignee in the task's own tracker "
                               "(--jira is the old name)")
    p_assign.set_defaults(func=cmd_assign)

    p_demo = sub.add_parser(
        "demo", help="デモ用のサンプルデータを台帳に入れる・消す（tenant: demo の設定でだけ動く）"
                     " / load or clear the demo sample data (tenant: demo only)")
    demo_actions = p_demo.add_subparsers(dest="demo_command", required=True)
    p_demo_load = demo_actions.add_parser("load", help="サンプルデータを入れる")
    p_demo_load.add_argument("--reset", action="store_true",
                             help="デモのテナントの行を消してから入れ直す")
    demo_actions.add_parser("reset", help="デモのテナントの行を消す")
    demo_actions.add_parser("status", help="いまの台帳の状況を表示")
    p_demo.set_defaults(func=cmd_demo)

    p_ledger = sub.add_parser(
        "ledger", help="台帳の保存先 / the ledger's storage (info, migrate)")
    ledger_actions = p_ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_actions.add_parser("info", help="保存先・件数を表示 / show store and counts")
    p_back = ledger_actions.add_parser(
        "migrate-to-sqlite", help="PostgreSQL の台帳を SQLite のファイルへ写す（migrate の逆向き）"
                                  " / copy the PostgreSQL ledger back to a SQLite file")
    p_back.add_argument("--to", metavar="PATH",
                        help="移行先の SQLite ファイル（既定は設定の台帳ファイルの場所）")
    p_back.add_argument("--side", choices=("file", "database"), default="file",
                        help="台帳の隣に置くもの（ブリーフィング・判断ログ・状態）の移し先"
                             "（file=隣のファイル、database=SQLite の表。既定 file）")
    p_back.add_argument("--force", action="store_true",
                        help="移行先に行があっても、同じ id を上書きする（実績は二重にしない）")
    p_side = ledger_actions.add_parser(
        "side-import", help="台帳の隣のファイル（ブリーフィング・判断ログ・状態）を、"
                            "データベースへ取り込む / import the files beside the ledger")
    p_side.add_argument("--from-dir", metavar="DIR",
                        help="取り込み元のディレクトリ（既定は台帳のあるディレクトリ）")
    p_side.add_argument("--force", action="store_true",
                        help="移行先に文書があっても上書きする（ログは足さない）")
    p_migrate = ledger_actions.add_parser(
        "migrate", help="SQLite の台帳を PostgreSQL へ移す / copy a SQLite ledger to PostgreSQL")
    p_migrate.add_argument("--from-sqlite", metavar="PATH",
                           help="移行元（既定は設定の台帳ファイル）/ source file")
    p_migrate.add_argument("--force", action="store_true",
                           help="移行先に行があっても、同じ id を上書きする")
    p_ledger.set_defaults(func=cmd_ledger)

    p_judgment = sub.add_parser(
        "judgment", help="PMO Core の自律的な判断を見る・止める・戻す "
                         "/ autonomous judgment: show, pause, resume, reset")
    judgment_actions = p_judgment.add_subparsers(dest="judgment_command")
    for action_name, text in (("pause", "一時停止(診断だけ行う)"), ("resume", "再開"),
                              ("reset", "遮断器を戻す")):
        judgment_actions.add_parser(action_name, help=text)
    p_judgment.set_defaults(func=cmd_judgment)

    p_collect = sub.add_parser(
        "collect", help="課題管理ツールの今の状態を台帳へ集める(読み取り専用) "
                        "/ collect progress from the trackers now (read-only)")
    p_collect.set_defaults(func=cmd_collect)

    p_generated = sub.add_parser(
        "generated", help="PMO Core が作ったタスク(提案・定期)を見る・決める "
                          "/ tasks the PMO Core made: list, approve, reject, done")
    generated_actions = p_generated.add_subparsers(dest="generated_command")
    for action_name, text in (("approve", "提案を承認する"), ("reject", "提案を却下する"),
                              ("done", "台帳だけのタスクを完了にする")):
        generated_actions.add_parser(action_name, help=text).add_argument(
            "ref", help="タスクの id")
    p_generated.set_defaults(func=cmd_generated)

    p_members = sub.add_parser(
        "members", help="メンバーがトラッカーのどのユーザーに当たるかを確かめる(読み取りだけ) "
                        "/ check which tracker user each member resolves to (read-only)")
    p_members.add_argument("--tracker", choices=("plane", "openproject"),
                           help="1 つのトラッカーだけ / one tracker only")
    p_members.set_defaults(func=cmd_members)

    p_integrations = sub.add_parser(
        "integrations", help="課題管理ツールとの接続を読み取りだけで診断する "
                             "/ diagnose tracker connections (read-only)")
    p_integrations.add_argument("name", nargs="?", help="アダプタ名(省略で全部) / one adapter (default: all)")
    p_integrations.set_defaults(func=cmd_integrations)

    p_file = sub.add_parser(
        "file", help="承認したタスクを課題管理ツールにも起票する(承認つき) "
                     "/ file approved PMO-made tasks into the tracker")
    p_file.add_argument("ref", nargs="?", help="起票するタスクの id")
    p_file.add_argument("--apply", action="store_true",
                        help="起票する(課題管理ツールに課題を作る) / actually create the issue")
    p_file.add_argument("--all", action="store_true", help="起票待ちを全部 / every waiting task")
    p_file.add_argument("--skip", action="store_true",
                        help="起票を見送る(台帳だけ) / do not file this one")
    p_file.set_defaults(func=cmd_file)

    p_agents = sub.add_parser(
        "agents", help="役割AI（開発・テスト・調査・文書・営業）の状況と実行 "
                       "/ role AIs: status and running one now")
    agents_actions = p_agents.add_subparsers(dest="agents_command")
    p_agent_run = agents_actions.add_parser(
        "run", help="役割AIに割り当てられたタスクを、いま任せる（失敗の再試行にも）")
    p_agent_run.add_argument("ref", help="タスクの id か課題のキー")
    p_review = agents_actions.add_parser(
        "review", help="役割AIの成果を人が確かめた記録を残す（引数なしで待ちの一覧）")
    p_review.add_argument("ref", nargs="?", help="タスクの id か課題のキー")
    p_review.add_argument("--accept", action="store_true", help="成果を認める")
    p_review.add_argument("--reject", action="store_true", help="成果を差し戻す（--note が要る）")
    p_review.add_argument("--note", help="理由・メモ")
    p_review.add_argument("--by", help="確かめた人の名前（既定はOSのユーザー名）")
    p_review.add_argument("--dispatch", help="実行記録の id（既定は直近の完了した実行）")
    p_agents.set_defaults(func=cmd_agents)

    p_wbs = sub.add_parser(
        "wbs", help="PMO AI 自身の開発 WBS の検証・状況 / validate or show the project's own WBS")
    wbs_actions = p_wbs.add_subparsers(dest="wbs_command", required=True)
    for action_name, text in (("check", "誤りと、証拠の欠けを調べる（error があれば終了コード 1）"),
                              ("status", "進捗・速度・完了見込み・クリティカルパスを表示")):
        p_action = wbs_actions.add_parser(action_name, help=text)
        p_action.add_argument("file", nargs="?", default="wbs/aipmo.yaml",
                              help="WBS ファイル（既定 wbs/aipmo.yaml）")
        p_action.add_argument("--root", help="証拠のパスの基準（既定はカレントディレクトリ）")
        p_action.add_argument("--as-of", help="基準日 YYYY-MM-DD（既定は今日）")
        if action_name == "check":
            p_action.add_argument("--strict", action="store_true",
                                  help="warning があっても失敗にする")
        else:
            p_action.add_argument("--json", action="store_true", help="JSON で出力")
    p_notify = wbs_actions.add_parser(
        "notify", help="WBS の更新漏れ・証拠の欠けを PR のコメントにする（--post で書く）")
    p_notify.add_argument("file", nargs="?", default="wbs/aipmo.yaml",
                          help="WBS ファイル（既定 wbs/aipmo.yaml）")
    p_notify.add_argument("--root", help="証拠のパスの基準（既定はカレントディレクトリ）")
    p_notify.add_argument("--as-of", help="基準日 YYYY-MM-DD（既定は今日）")
    p_notify.add_argument("--base", help="PR の土台のブランチ（例 origin/main）。git diff で、この PR の"
                                         "変更ファイルを調べる")
    p_notify.add_argument("--changed", nargs="*", help="変更ファイルを直接渡す（--base の代わり）")
    p_notify.add_argument("--pr", type=int, help="コメントする PR の番号")
    p_notify.add_argument("--repo", help="owner/name（既定は環境変数 GITHUB_REPOSITORY）")
    p_notify.add_argument("--author", help="目印つきコメントの書き手の login（既定は Bot。"
                                           "個人のトークンで書くときに指定する）")
    p_notify.add_argument("--post", action="store_true",
                          help="GitHub に書く（トークンは環境変数 GITHUB_TOKEN）")
    p_props = wbs_actions.add_parser(
        "proposals", help="承認待ちの WBS 変更提案（wbs_replan）を見る・承認して WBS ファイルへ反映する")
    p_props.add_argument("proposals_command", nargs="?",
                         choices=("list", "show", "approve", "reject", "apply"), default="list")
    p_props.add_argument("ref", nargs="?", help="提案の id")
    p_props.add_argument("--file", help="反映先の WBS ファイル（既定は adapters.wbs_replan.file、"
                                         "なければ wbs/aipmo.yaml）")
    p_props.add_argument("--root", help="証拠のパスの基準（既定は adapters.wbs_replan.root）")
    p_props.add_argument("--by", help="決めた人の名前（既定はOSのユーザー名）")
    p_props.add_argument("--note", help="承認・却下のメモ")
    p_props.add_argument("--force", action="store_true",
                         help="すでに反映した提案でも、もう一度反映する（apply）")
    p_wbs.set_defaults(func=cmd_wbs)

    p_knowledge = sub.add_parser(
        "knowledge", help="ナレッジ公開候補のレビュー（一覧・修正・承認・却下）"
                          " / review knowledge candidates (list/edit/approve/reject)")
    p_knowledge.add_argument("knowledge_command", nargs="?",
                             choices=("list", "show", "edit", "approve", "reject"),
                             default="list")
    p_knowledge.add_argument("ref", nargs="?", help="候補の id")
    p_knowledge.add_argument("--backend",
                             help="使うアダプタ名（既定は論理名 vector_store。"
                                  "複数のベクトルストアを設定している場合に指定）")
    p_knowledge.add_argument("--status", choices=("pending", "approved", "rejected"),
                             help="list で見る状態（既定 pending）")
    p_knowledge.add_argument("--text", help="edit で書き直す新しい内容")
    p_knowledge.add_argument("--by", help="決めた人の名前（既定はOSのユーザー名）")
    p_knowledge.add_argument("--note", help="承認・却下のメモ")
    p_knowledge.set_defaults(func=cmd_knowledge)

    p_setup = sub.add_parser("setup", help="初回セットアップ / first-run setup")
    p_setup.add_argument("--dir", default=".", help="設定の出力先 / where to write config")
    p_setup.set_defaults(func=cmd_setup)

    args = parser.parse_args(argv)

    # 出力先が受け付けない文字で落ちないようにする。
    # 日本語版 Windows のコンソールは CP932 で、記号の一部が入らない。
    # Keeps an unprintable character from ending the command: a Japanese
    # Windows console runs CP932, which lacks some of the glyphs used here.
    configure_stdio()
    load_env(Path(args.config).parent if Path(args.config).parent != Path("") else Path("."))
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
