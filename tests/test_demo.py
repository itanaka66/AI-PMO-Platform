"""デモ用サンプルデータの読み込み（aipmo demo）と、docs/DEMO.md の手順のテスト。

確かめること:
  (1) 読み込むと、仕込んだ状況（期限切れ・長期ブロック・過負荷・担当未定・役割AIの成果・WBS のずれ）が、
      本物の判定で警告・提案として現れる。履歴は過去の時刻でさかのぼる
  (2) 安全：tenant が demo でなければ読み込みも消去もしない。タスクがあれば読み込まない。--reset は
      デモのテナントの行だけを消し、何度やっても同じ形になる
  (3) サンプルデータの誤りは、読み込む前にはっきり止まる
  (4) docs/DEMO.md のコマンドが、そのとおり動く（手順が壊れていない）
  (5) 画面（Web）にも、同じ状況が出る。viewer にはボタンの元になる操作が許されない
  (6) 実 PostgreSQL でも同じ（あるとき）

What matters: loading produces the staged situations through the real rules (history reaches into the
past); the demo works only for `tenant: demo`, refuses a non-empty ledger, `--reset` clears only the demo
tenant and is repeatable; bad sample data stops before anything is written; every command in the guide
runs as written; the web shows the same state; PostgreSQL too when available.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from aipmo import cli, demo
from aipmo.task_engine import TaskEngine

ROOT = Path(__file__).resolve().parents[1]
PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")


@pytest.fixture
def demo_dir(tmp_path):
    """リポジトリの demo/ の写し。ledger やブリーフィングなど、実行時に出来るものは持ち込まない。"""
    target = tmp_path / "demo"
    shutil.copytree(ROOT / "demo", target, ignore=shutil.ignore_patterns(
        "task-ledger.db*", "pmo-*", "scheduler-state.json", "__pycache__"))
    return target


def config_of(demo_dir: Path, name: str = "config.yaml") -> Path:
    return demo_dir / name


def load(demo_dir: Path, *extra: str) -> int:
    return cli.main(["--config", str(config_of(demo_dir)), "demo", "load", *extra])


def run(demo_dir: Path, *args: str) -> int:
    return cli.main(["--config", str(config_of(demo_dir)), *args])


def open_ledger(demo_dir: Path) -> TaskEngine:
    return cli.open_ledger(cli.load_config(config_of(demo_dir)), demo_dir)


# ===== (1) 仕込んだ状況が、本物の判定で現れる ===========================================================

def test_loading_stages_the_situations_and_the_real_rules_react(demo_dir, capsys):
    assert load(demo_dir) == 0
    out = capsys.readouterr().out
    assert "demo data loaded" in out and "完了実績 20 件" in out and "学習 20 件" in out

    ledger = open_ledger(demo_dir)
    tasks = {t.key: t for t in ledger.tasks.values() if t.key and t.key[:3] in ("WEB", "MOB", "INF")}
    assert len(tasks) == 22
    assert {t.project for t in tasks.values()} == {"web-renewal", "mobile-app", "infra"}
    assert len(ledger.outcomes) == 20

    # 履歴は過去の時刻でさかのぼる（着手・ブロックの日が実際にさかのぼる）
    now = datetime.now(timezone.utc)

    def days_ago(stamp: str) -> float:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 86400

    assert 6 <= days_ago(tasks["INF-301"].blocked_since) <= 8                # 6 日ブロック(+周)
    assert days_ago(tasks["MOB-201"].first_seen) >= 13
    assert days_ago(tasks["MOB-201"].started_at) >= 11
    assert tasks["INF-301"].blocked and tasks["INF-301"].status == "Blocked"

    briefing = cli.build_pmo_core(cli.load_config(config_of(demo_dir)), ledger, base=demo_dir).cycle()
    messages = " ".join(a["message"] for a in briefing["alerts"])
    assert briefing["overall_level"] == "critical"
    assert "期限を" in messages and "ブロックが" in messages and "担当者が" in messages
    assert "差し戻されました" in messages                                       # 役割AIの差し戻し
    titles = {a["title"] for a in briefing["alerts"]}
    assert {"プッシュ通知が届かない", "ログイン画面の表示が遅い", "コスト削減案をレビューする"} <= titles

    # 過負荷: 佐藤は 6 件で、学習で上限が下がっている
    loads = {m["member"]: m for m in briefing["member_loads"]}
    assert loads["佐藤"]["load"] == 6 and loads["佐藤"]["load"] > loads["佐藤"]["capacity"]
    assert any(m["member"] == "佐藤" for m in briefing["overloaded_members"])

    # 学習
    learning = briefing["learning"]
    assert learning["samples"] == 20
    assert learning["member_factor"]["佐藤"] < 1 < learning["member_factor"]["鈴木"]
    assert "bug" in learning["label_bonus"] or "dev" in learning["label_bonus"]
    assert learning["pace"]["team"]

    # 担当の提案: 担当未定の 4 件に、スキルが合う人
    proposed = {p["key"] if "key" in p else p.get("task"): p for p in briefing["assignment_proposals"]}
    flat = {(p.get("title"), p.get("assignee") or p.get("member")) for p in briefing["assignment_proposals"]}
    assert len(briefing["assignment_proposals"]) == 4 and proposed
    assert {who for _, who in flat} == {"鈴木", "高橋"}

    # 承認待ちの提案: 警告からの対応・WBS のずれ・自律的な判断
    pending = ledger.proposals()
    ids = [t.id for t in pending]
    assert any(i.startswith("PMO:fu:") for i in ids)
    wbs_nodes = sorted(t.generated_from for t in pending if t.id.startswith("PMO:wb:"))
    assert wbs_nodes == ["wbs:evidence_missing:2.1", "wbs:maybe_done:1.2"]
    judgments = ledger.judgments()
    assert judgments and all(t.proposed for t in judgments)                  # 既定は提案どまり

    # 役割AI: 認め済み 1・差し戻し済み 1・レビュー待ち 1
    states = {}
    for task in ledger.tasks.values():
        for entry in task.dispatches:
            states[task.key] = (entry["status"], (entry.get("review") or {}).get("decision"))
    assert states == {"INF-320": ("done", "accepted"), "MOB-220": ("done", "rejected"),
                      "WEB-130": ("done", None)}

    # 起票待ち: 設定に書いた定期タスク（承認なしで作られた）
    recurring = [t for t in ledger.tasks.values() if t.origin == "recurring"]
    assert len(recurring) == 1 and recurring[0].assignee == "佐藤"
    ledger.close()


def test_the_load_summary_matches_the_ledger(demo_dir):
    config = cli.load_config(config_of(demo_dir))
    summary = demo.load(config, demo_dir)
    assert summary["outcomes"] == 20 and summary["learned_samples"] == 20
    assert summary["wbs_proposals"] == 2 and summary["followup_proposals"] >= 2
    assert summary["assignment_proposals"] == 4 and summary["judgments_pending"] >= 1
    assert summary["reviews_pending"] == 1 and summary["filing_pending"] == 1
    assert summary["reviews"] == {"accepted": "JIRA:INF-320", "rejected": "JIRA:MOB-220"}
    assert summary["level"] == "critical" and summary["alerts"] >= 10


# ===== (2) 安全 / safety ============================================================================

def test_it_refuses_a_ledger_that_already_has_tasks_and_reset_makes_it_repeatable(demo_dir, capsys):
    assert load(demo_dir) == 0
    capsys.readouterr()
    first = open_ledger(demo_dir)
    shape = (len([t for t in first.tasks.values() if t.key]), len(first.outcomes),
             len(first.proposals()), len(first.judgments()))
    first.close()

    assert load(demo_dir) == 1                                    # 入っているので入れない
    assert "--reset" in capsys.readouterr().err
    assert load(demo_dir, "--reset") == 0                         # 消して入れ直す
    again = open_ledger(demo_dir)
    assert (len([t for t in again.tasks.values() if t.key]), len(again.outcomes),
            len(again.proposals()), len(again.judgments())) == shape
    again.close()


def test_reset_removes_the_ledger_and_the_files_beside_it_only_in_the_demo_directory(demo_dir, tmp_path):
    other = tmp_path / "precious.txt"
    other.write_text("keep", encoding="utf-8")
    assert load(demo_dir) == 0
    assert (demo_dir / "task-ledger.db").exists() and (demo_dir / "pmo-briefing.json").exists()
    assert run(demo_dir, "demo", "reset") == 0
    assert not (demo_dir / "task-ledger.db").exists()
    assert not list(demo_dir.glob("pmo-*"))
    assert other.read_text(encoding="utf-8") == "keep" and (demo_dir / "config.yaml").exists()
    assert run(demo_dir, "demo", "reset") == 0                    # 空でも失敗しない


def test_it_never_touches_a_ledger_of_another_tenant(demo_dir, capsys):
    cfg = config_of(demo_dir)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["tenant"] = "acme"
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    ledger = TaskEngine(demo_dir / "task-ledger.db", tenant="acme")
    ledger.ingest("t", "r", [{"key": "A-1", "title": "本番のタスク", "assignee": None, "due_date": None,
                              "priority": None, "status": None, "blocked": False, "done": False,
                              "labels": []}])
    ledger.close()
    for command in (["load"], ["load", "--reset"], ["reset"], ["status"]):
        assert run(demo_dir, "demo", *command) == 1
        assert "tenant: demo" in capsys.readouterr().err
    assert "JIRA:A-1" in TaskEngine(demo_dir / "task-ledger.db", tenant="acme").tasks    # 無傷


def test_a_ledger_file_stamped_for_another_tenant_is_neither_loaded_into_nor_deleted(demo_dir, capsys):
    TaskEngine(demo_dir / "task-ledger.db", tenant="someone-else").close()
    for command in (["load"], ["load", "--reset"], ["reset"]):
        assert run(demo_dir, "demo", *command) == 1
        assert "someone-else" in capsys.readouterr().err
    assert (demo_dir / "task-ledger.db").exists()                  # 消していない
    assert TaskEngine(demo_dir / "task-ledger.db", tenant="someone-else").tasks == {}


# ===== (3) サンプルデータの誤り / bad sample data ========================================================

def write_data(demo_dir: Path, name: str, text: str) -> None:
    (demo_dir / "data" / name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("name,text,needle", [
    ("tasks.yaml", "tasks: []\n", "tasks"),
    ("tasks.yaml", "tasks:\n  - {title: 題名だけ}\n", "key と title"),
    ("tasks.yaml", "tasks:\n  - {key: A-1, title: a}\n  - {key: A-1, title: b}\n", "重複"),
    ("tasks.yaml", "tasks:\n  - {key: A-1, title: a, due: soon}\n", "due"),
    ("tasks.yaml", "not: a list\n", "tasks"),
    ("outcomes.yaml", "outcomes: []\n", "outcomes"),
    ("outcomes.yaml", "outcomes:\n  - {labels: [x]}\n", "assignee"),
])
def test_bad_sample_data_stops_before_anything_is_written(demo_dir, capsys, name, text, needle):
    write_data(demo_dir, name, text)
    assert load(demo_dir) == 1
    assert needle in capsys.readouterr().err
    assert not (demo_dir / "task-ledger.db").exists()               # 何も作らない


def test_missing_sample_files_are_reported(demo_dir, capsys):
    (demo_dir / "data" / "tasks.yaml").unlink()
    assert load(demo_dir) == 1 and "tasks.yaml" in capsys.readouterr().err


# ===== (4) docs/DEMO.md の手順 / the guide's commands run as written =====================================

GUIDE = (ROOT / "docs" / "DEMO.md").read_text(encoding="utf-8")


def guide_commands() -> list[str]:
    """DEMO.md の ```bash ブロックの、`aipmo ...` の行（コメントを除く）。"""
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", GUIDE, flags=re.S):
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("aipmo "):
                continue
            commands.append(line.split("   #")[0].split("  #")[0].strip())
    return commands


def test_the_guide_has_the_commands_it_promises():
    commands = guide_commands()
    assert len(commands) >= 20
    text = "\n".join(commands)
    for needle in ("demo load", "pmo", "tasks --why", "assign", "generated", "agents review", "judgment",
                   "file --all --apply", "wbs check", "wbs notify", "demo reset", "demo load --reset"):
        assert needle in text, needle


NOT_RUN = ("serve", "schedule", "demo load --reset", "demo reset", "config.postgres", "ledger migrate")


def test_every_runnable_command_in_the_guide_runs_on_a_loaded_demo(demo_dir, capsys):
    assert load(demo_dir) == 0
    capsys.readouterr()
    ran = 0
    for command in guide_commands():
        if "<" in command or any(skip in command for skip in NOT_RUN):
            continue
        argv = command.split()[1:]
        if argv[:2] == ["--config", "demo/config.yaml"]:
            argv[1] = str(config_of(demo_dir))
        elif argv[0] == "wbs":
            argv = [("demo" if a == "demo" and argv[i - 1] == "--root" else a)
                    for i, a in enumerate(argv)]
            argv[argv.index("--root") + 1] = str(demo_dir)
        if "demo load" in command:
            continue                                            # すでに入っている(手順 2 は最初に確認済み)
        code = cli.main(argv)
        expected = 1 if argv[:2] == ["wbs", "check"] else 0     # 意図したずれがあるので error になる
        if "assign" in command and "--apply" in command:
            expected = 0
        assert code == expected, f"{command} -> {code}\n{capsys.readouterr().err}"
        ran += 1
    assert ran >= 12


def test_the_guides_scenario_end_to_end(demo_dir, capsys):
    """手順 4 を、`<ID>` を出力から拾いながら、そのまま通す。"""
    assert load(demo_dir) == 0
    capsys.readouterr()

    def ids(prefix: str) -> list[str]:
        run(demo_dir, "generated")
        return re.findall(rf"^\s+({re.escape(prefix)}\S+)$", capsys.readouterr().out, flags=re.M)

    assert run(demo_dir, "assign", "INF-315", "--apply") == 0               # 4-3
    assert open_ledger(demo_dir).find("INF-315").assignee == "鈴木"

    followup = ids("PMO:fu:")[0]                                            # 4-4
    assert run(demo_dir, "generated", "approve", followup) == 0
    assert followup not in ids("PMO:fu:")

    assert run(demo_dir, "agents", "review", "WEB-130", "--accept", "--by", "あなた") == 0   # 4-5
    assert run(demo_dir, "agents", "review", "WEB-130", "--accept", "--by", "開発AI") == 1   # AI は不可
    capsys.readouterr()
    run(demo_dir, "agents", "review")
    assert "(0)" in capsys.readouterr().out

    judgment = ids("PMO:jd:")[0]                                            # 4-6
    assert run(demo_dir, "generated", "approve", judgment) == 0
    ledger = open_ledger(demo_dir)
    assert ledger.tasks[judgment].payload["state"] == "approved"            # 承認しただけでは実行されない
    ledger.close()

    assert run(demo_dir, "file") == 0                                       # 4-7
    capsys.readouterr()
    assert run(demo_dir, "file", "--all", "--apply") == 0
    filed = capsys.readouterr().out
    assert "DEMO-1" in filed and "DEMO-2" in filed                          # 週次レビュー＋承認した対応タスク

    evidence = demo_dir / "evidence" / "login.py"                           # 4-8
    assert run(demo_dir, "wbs", "check", "wbs-demo.yaml", "--root", str(demo_dir)) == 1
    evidence.write_text("def login(): ...\n", encoding="utf-8")
    assert run(demo_dir, "wbs", "check", "wbs-demo.yaml", "--root", str(demo_dir)) == 0


def test_the_resident_executes_an_approved_judgment(demo_dir):
    """4-6 の「常駐が実行する」を、周を 1 回回して確かめる（schedule を待たずに）。"""
    assert load(demo_dir) == 0
    config = cli.load_config(config_of(demo_dir))
    ledger = open_ledger(demo_dir)
    judgment = next(t for t in ledger.judgments() if t.proposed and t.payload.get("remedy") == "followup")
    ledger.close()
    assert run(demo_dir, "generated", "approve", judgment.id) == 0

    engine = cli.build_engine(config, base_dir=demo_dir)
    core = cli.attach_task_engine(engine, config, demo_dir, default=True, launch=True)
    core.background = False
    core.cycle()
    core.wait()
    states = {t.id: t.payload.get("state") for t in core.task_engine.judgments()}
    assert states[judgment.id] == "executed"
    core.task_engine.close()


# ===== (5) 画面 / the web ================================================================================

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.pmo_core import load_members  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "demo-operator", "demo-viewer"


def web(demo_dir: Path) -> TestClient:
    config = cli.load_config(config_of(demo_dir))
    members = load_members(config["pmo_core"]["members"])
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    engine = cli.build_engine(config, base_dir=demo_dir)
    (demo_dir / "templates").mkdir(exist_ok=True)
    app = create_app(Engine(engine.adapters, llms), demo_dir / "templates", OPERATOR,
                     viewer_token=VIEWER, lang="ja", store=RunStore(),
                     pmo_ledger=demo_dir / "task-ledger.db", members=members,
                     filing=cli._web_filing(config))
    return TestClient(app)


def test_the_web_shows_the_same_state_and_only_the_operator_gets_the_actions(demo_dir):
    assert load(demo_dir) == 0
    client = web(demo_dir)
    view = client.get("/api/pmo", headers={"x-aipmo-token": OPERATOR})
    assert view.status_code == 200
    data = view.json()
    assert data["briefing"]["overall_level"] == "critical"
    assert len(data["tasks"]) >= 10 and data["proposals"]
    assert [r["task"] for r in data["agent_review"]] == ["JIRA:WEB-130"]
    assert [p["title"] for p in data["filing"]["pending"]] == ["週次レビュー: 進捗と警告を確認する"]
    assert data["filing"]["can_file"] is True

    # operator: 役割AIの成果を認める → 一覧から消える
    ok = client.post("/api/pmo/agents/review", headers={"x-aipmo-token": OPERATOR},
                     json={"ref": "WEB-130", "decision": "accept", "by": "あなた"})
    assert ok.status_code == 200
    after = client.get("/api/pmo", headers={"x-aipmo-token": OPERATOR}).json()
    assert after["agent_review"] == []

    # operator: 起票する（mock の Jira）
    filed = client.post("/api/pmo/filing", headers={"x-aipmo-token": OPERATOR},
                        json={"ref": data["filing"]["pending"][0]["id"], "decision": "file"})
    assert filed.status_code == 200 and filed.json()["key"] == "DEMO-1"

    # viewer: 見られるが、操作は断られる
    seen = client.get("/api/pmo", headers={"x-aipmo-token": VIEWER})
    assert seen.status_code == 200
    denied = client.post("/api/pmo/agents/review", headers={"x-aipmo-token": VIEWER},
                         json={"ref": "WEB-130", "decision": "accept"})
    assert denied.status_code in (401, 403)


# ===== (6) 実 PostgreSQL / a real PostgreSQL ==============================================================

needs_pg = pytest.mark.skipif(not PG_DSN, reason="AIPMO_TEST_PG_DSN が未設定")


@needs_pg
def test_the_demo_loads_into_postgres_and_reset_leaves_other_tenants_alone(demo_dir, monkeypatch, capsys):
    import psycopg

    monkeypatch.setenv("AIPMO_DEMO_DSN", PG_DSN)
    other = f"t{uuid.uuid4().hex[:10]}"
    from aipmo.ledger_store import PostgresStore

    bystander = TaskEngine(demo_dir / "x.db", tenant=other, store=PostgresStore(PG_DSN, other))
    bystander.ingest("t", "r", [{"key": "Z-1", "title": "別のテナント", "assignee": None, "due_date": None,
                                 "priority": None, "status": None, "blocked": False, "done": False,
                                 "labels": []}])
    bystander.close()
    pg = config_of(demo_dir, "config.postgres.yaml")
    try:
        assert cli.main(["--config", str(pg), "demo", "load", "--reset"]) == 0
        out = capsys.readouterr().out
        assert "postgres" in out and "完了実績 20 件" in out
        assert cli.main(["--config", str(pg), "ledger", "info"]) == 0
        info = capsys.readouterr().out
        assert "database tables of postgres" in info                      # 隣に置くものも DB に入る
        assert not list(demo_dir.glob("pmo-*"))
        assert cli.main(["--config", str(pg), "demo", "load"]) == 1       # 入っているので入れない
        assert cli.main(["--config", str(pg), "demo", "reset"]) == 0
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            demo_rows = conn.execute("SELECT count(*) FROM ledger_tasks WHERE tenant = 'demo'").fetchone()[0]
            other_rows = conn.execute("SELECT count(*) FROM ledger_tasks WHERE tenant = %s",
                                      (other,)).fetchone()[0]
        assert demo_rows == 0 and other_rows == 1                          # 別のテナントは無傷
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta", "ledger_side_docs",
                          "ledger_side_log"):
                conn.execute(f"DELETE FROM {table} WHERE tenant IN ('demo', %s)", (other,))


def test_dates_in_the_sample_data_are_relative_so_overdue_stays_overdue():
    samples = demo.read_samples(ROOT / "demo" / "data")
    overdue = [t for t in samples.tasks if t.get("due", 0) < 0]
    soon = [t for t in samples.tasks if 0 <= t.get("due", 99) <= 3]
    assert len(overdue) >= 3 and len(soon) >= 4
    assert all(isinstance(t["due"], int) for t in samples.tasks)
    assert date.today().year >= 2026                                       # 絶対日付に依存しない
