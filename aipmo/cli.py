"""CLI エントリポイント。

  aipmo validate templates/examples/meeting_minutes.yaml
  aipmo run templates/examples/meeting_minutes.yaml --param meeting_id=MTG-001
  aipmo adapters
"""
from __future__ import annotations

import argparse
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

    # wbs_replan は postgres の上に合成される（JiraAgileAdapter が jira の
    # 上に合成されるのと同じ形）。postgres が無ければ wbs_replan_proposals
    # にもそもそも書けないので、postgres が設定されているときだけ登録する。
    #
    # wbs_replan is composed on top of postgres (same shape as
    # JiraAgileAdapter over jira). With no postgres there is nowhere for
    # wbs_replan_proposals to live, so this only registers when postgres is
    # configured.
    if "wbs_replan" in adapter_config:
        if not adapters.has("postgres"):
            raise ConfigError(
                "config.yaml の adapters.wbs_replan を使うには adapters.postgres の"
                "設定も必要です / adapters.wbs_replan requires adapters.postgres "
                "to also be configured"
            )
        from .adapters.wbs_replan import WbsReplanAdapter

        adapters.register(WbsReplanAdapter(
            postgres=cast(PostgresAdapter, adapters.get("postgres"))))

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
                          store=factory() if factory else None, **kwargs)
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
    return build_pmo_core(config, task_engine, engine, base if launch else None)


def build_pmo_core(config: dict[str, Any], task_engine: Any,
                   engine: Engine | None = None, launch_base: Path | None = None):
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

    # 高リスク時の応答と、役割AI（`kind: agent` のメンバー）は、どちらも
    # テンプレートを起動する。起動できるのは、運用者が設定に書いたものだけ。
    # Responses to high risk and role AIs (`kind: agent` members) both launch
    # templates, and only the ones the operator wrote into the config.
    launcher = None
    wanted = ({r.template for r in responses}
              | {str(m.template) for m in members if m.is_agent})
    if wanted:
        root = Path((config.get("web") or {}).get("templates_dir", "templates"))
        root = root if root.is_absolute() else (launch_base or Path.cwd()) / root
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

    learning = section.get("learning") or {}
    agents_config = section.get("agents") or {}
    return PmoCore(task_engine=task_engine, rules=rules,
                   members=members, notify=notify,
                   renotify_hours=int(notify_config.get("renotify_hours", 24)),
                   learning=bool(learning.get("enabled", True)),
                   min_samples=int(learning.get("min_samples", 5)),
                   agent_timeout_minutes=int(agents_config.get("timeout_minutes", 60)),
                   agent_max_per_day=int(agents_config.get("max_per_day", 20)),
                   responses=responses, launcher=launcher)


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


def _open_ledger(args: argparse.Namespace):
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    return config, build_pmo_core(config, open_ledger(config, base))


def cmd_pmo(args: argparse.Namespace) -> int:
    """PMO Core のブリーフィングを表示する / show the PMO Core briefing."""
    from .pmo_core import scope_briefing

    try:
        _, core = _open_ledger(args)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    briefing = core.cycle()
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
    if briefing["responses"]:
        print("\n高リスク時の応答 / responses")
        for r in briefing["responses"]:
            print(f"  {r['id']:<20} {r['template']:<24} {r['status']}")
    learned = briefing.get("learning")
    if learned and (learned["member_factor"] or learned["label_bonus"]):
        print(f"\n学習した補正 / learned adjustments (実績 {learned['samples']} 件)")
        for who, factor in learned["member_factor"].items():
            print(f"  {who}: キャパシティ x{factor}")
        for label, bonus in learned["label_bonus"].items():
            print(f"  ラベル {label}: 加点 +{bonus}")
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
            print(f"{t.key or t.id:<14} {t.title} → {t.suggested_assignee}"
                  f"  ({t.suggestion_reason})")
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
        write = make_writer(engine.adapters, core.members)

    try:
        task = core.accept_assignment(args.ref, write=write)
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    written = (core.last_writeback or {}).get("tracker")
    print(f"確定しました / assigned: {task.title} → {task.assignee}"
          + (f" ({written} 更新済み / {written} updated)" if written
             else " (台帳のみ / ledger only)"))
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """横断の優先順位を表示する / show the cross-template ranking."""
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent
    try:
        task_engine = open_ledger(config, base)
    except ConfigError as exc:
        print(f"設定エラー / config error: {exc}", file=sys.stderr)
        return 1
    task_engine.refresh()
    ranked = task_engine.ranked(assignee=args.assignee, limit=args.limit,
                                project=args.project)
    if not ranked:
        print("タスクはありません / no tasks. "
              "(`aipmo schedule` が走ると集まります / gathered while the scheduler runs)")
        return 0
    for position, task in enumerate(ranked, 1):
        who = task.assignee or "-"
        due = task.due_date or "-"
        if not task.assignee and task.suggested_assignee:
            who = f"担当未定→提案 {task.suggested_assignee}"
        where = f"{task.project}, " if task.project and not args.project else ""
        print(f"{position:>3}. [{task.score:>3}] {task.key or '':<10} {task.title}"
              f"  ({where}{who}, 期限 {due}, {', '.join(task.templates)})")
        if args.why:
            for reason in task.reasons:
                print(f"        - {reason}")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    """役割AIの状況を見る／タスクをいま任せる（失敗の再試行にも）。

    `aipmo agents`            … 役割AIごとの件数と、直近の実行
    `aipmo agents run REF`    … 役割AIに割り当てられたタスクを、いま任せて結果を待つ
    """
    config = load_config(Path(args.config))
    base = Path(args.config).resolve().parent

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
              + ("  自動確定" if item["auto_confirm"] else ""))
    runs = briefing["agent_runs"]
    print(f"\n直近の実行 / recent runs ({len(runs)})")
    for run in runs:
        print(f"  [{run['status']:<9}] {run['agent']:<12} {run['title'][:40]}")
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


def cmd_ledger(args: argparse.Namespace) -> int:
    """台帳の保存先を調べる／SQLite から PostgreSQL へ移す。"""
    from .ledger_store import (LedgerConfigError, LedgerTenantError, SqliteStore,
                               Snapshot)
    from .task_engine import MAX_OUTCOMES, TaskEngine

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
        return 0

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

    with target._store.write() as tx:
        tx.apply(snapshot.tasks, [], snapshot.outcomes, MAX_OUTCOMES)
    moved = Snapshot(tasks=snapshot.tasks, outcomes=snapshot.outcomes)
    print(f"移行しました / migrated: {len(moved.tasks)} tasks, "
          f"{len(moved.outcomes)} outcomes  {source_path} -> {target.describe()}")
    print("移行元のファイルは残してあります（確認後に削除してください）"
          " / the source file is left in place; delete it once you have checked")
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
    template_root = Path(web.get("templates_dir", "templates")).resolve()
    app = create_app(engine, template_root, token, viewer_token=viewer_token,
                     tenant=config.get("tenant", ""), lang=config.get("lang"),
                     cors_origins=cors_origins or None,
                     pmo_ledger=ledger_path(config, base),
                     viewer_projects=web.get("viewer_projects"),
                     ledger_store_factory=store_factory,
                     members=load_members((config.get("pmo_core") or {}).get("members")))

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

    p_ledger = sub.add_parser(
        "ledger", help="台帳の保存先 / the ledger's storage (info, migrate)")
    ledger_actions = p_ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_actions.add_parser("info", help="保存先・件数を表示 / show store and counts")
    p_migrate = ledger_actions.add_parser(
        "migrate", help="SQLite の台帳を PostgreSQL へ移す / copy a SQLite ledger to PostgreSQL")
    p_migrate.add_argument("--from-sqlite", metavar="PATH",
                           help="移行元（既定は設定の台帳ファイル）/ source file")
    p_migrate.add_argument("--force", action="store_true",
                           help="移行先に行があっても、同じ id を上書きする")
    p_ledger.set_defaults(func=cmd_ledger)

    p_agents = sub.add_parser(
        "agents", help="役割AI（開発・テスト・調査・文書・営業）の状況と実行 "
                       "/ role AIs: status and running one now")
    agents_actions = p_agents.add_subparsers(dest="agents_command")
    p_agent_run = agents_actions.add_parser(
        "run", help="役割AIに割り当てられたタスクを、いま任せる（失敗の再試行にも）")
    p_agent_run.add_argument("ref", help="タスクの id か課題のキー")
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
    p_wbs.set_defaults(func=cmd_wbs)

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
