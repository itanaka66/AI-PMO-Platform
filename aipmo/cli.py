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

    engine = build_engine(config)
    template_root = Path(web.get("templates_dir", "templates")).resolve()
    app = create_app(engine, template_root, token, viewer_token=viewer_token,
                     tenant=config.get("tenant", ""), lang=config.get("lang"))

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
    scheduler = Scheduler(engine, jobs, State.load(state_path))

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
        engine = build_engine(load_config(config_path), config_path.parent, approve=approve)
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
