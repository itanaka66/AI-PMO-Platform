"""WBS の更新漏れを PR に知らせる（aipmo/wbs_notify.py）のテスト。

確かめること:
  (1) 集める：証拠が揃ったのに未完了／完了なのに証拠が無い・消えた。この PR の変更ファイルに
      当たるものを先に出す（ファイル・ディレクトリ・パターン）
  (2) 書く：完了にする書き方を添える。PR の著者が変えられる文字を無害にする。長さを切る
  (3) 変更ファイル：git の差分から取る（不正な base は拒否）
  (4) コメントは 1 つだけ：作る・更新する・変わらなければ触らない・指摘が無ければ投稿しない。
      他の人が目印を真似て書いたコメントは更新しない
  (5) CLI は既定で何も書かない。ワークフローは fork で動かず、PR を失敗にしない

What matters: drift is found and the items this PR touches come first; the text is neutralised and
bounded; changed files come from git (a bad base is refused); one sticky comment is created, updated,
left alone when unchanged, not posted when there is nothing, and an imitation by someone else is
never touched; the CLI writes nothing unless told; the workflow skips forks and never fails the PR.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from aipmo import cli
from aipmo.wbs import load_wbs
from aipmo.wbs_notify import (
    MARKER,
    GitHubComments,
    NotifyError,
    changed_files,
    collect,
    neutralise,
    render,
)

TODAY = date(2026, 10, 2)

HEAD = "wbs:\n  id: demo\n  name: デモ\n  nodes:\n"


def leaf(id_, name, status="todo", evidence=(), **extra) -> str:
    out = [f'    - id: "{id_}"', f'      name: "{name}"', f"      status: {status}"]
    if status == "done":
        out.append("      done_on: 2026-09-01")
    out += [f"      {k}: {v}" for k, v in extra.items()]
    if evidence:
        out.append("      evidence:")
        out += [f'        - "{e}"' for e in evidence]
    return "\n".join(out) + "\n"


def make(tmp_path: Path, *leaves: str):
    (tmp_path / "wbs").mkdir(exist_ok=True)
    path = tmp_path / "wbs" / "w.yaml"
    path.write_text(HEAD + "".join(leaves), encoding="utf-8")
    wbs, _ = load_wbs(path)
    return wbs, path


def files(tmp_path: Path, *names: str) -> None:
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")


# ===== (1) 集める / collecting =========================================================================

def test_finds_the_three_kinds_of_drift_and_ignores_healthy_nodes(tmp_path):
    files(tmp_path, "a.py", "b.py")
    wbs, _ = make(tmp_path,
                  leaf("1", "終わっていそう", evidence=["a.py"]),
                  leaf("2", "証拠の無い完了", status="done"),
                  leaf("3", "証拠が消えた完了", status="done", evidence=["gone.py"]),
                  leaf("4", "健全な完了", status="done", evidence=["b.py"]),
                  leaf("5", "ふつうの未完了"))
    found = {f.node: f.code for f in collect(wbs, tmp_path, TODAY)}
    assert found == {"1": "maybe_done", "2": "done_without_evidence", "3": "evidence_missing"}


def test_what_this_pr_touches_comes_first_by_file_directory_or_pattern(tmp_path):
    files(tmp_path, "a.py", "pkg/mod.py", "t/test_x.py", "t/test_y.py")
    wbs, _ = make(tmp_path,
                  leaf("1", "別件", evidence=["a.py"]),
                  leaf("2", "ディレクトリ", evidence=["pkg"]),
                  leaf("3", "パターン", evidence=["t/test_*.py"]),
                  leaf("4", "ファイル", evidence=["pkg/mod.py::x"]))
    plain = collect(wbs, tmp_path, TODAY)
    assert [f.node for f in plain] == ["1", "2", "3", "4"] and not any(f.relevant for f in plain)
    found = collect(wbs, tmp_path, TODAY, ["pkg/mod.py", "t/test_x.py"])
    assert [f.node for f in found if f.relevant] == ["2", "3", "4"]
    assert [f.node for f in found][:3] == ["2", "3", "4"] and found[-1].node == "1"
    assert found[0].touched == ["pkg"] or found[0].touched


def test_deleting_the_evidence_of_a_done_node_is_flagged_as_relevant(tmp_path):
    wbs, _ = make(tmp_path, leaf("1", "完了のはず", status="done", evidence=["removed.py"]))
    (finding,) = collect(wbs, tmp_path, TODAY, ["removed.py"])
    assert finding.code == "evidence_missing" and finding.relevant


def test_a_pr_that_completes_only_part_of_the_evidence_is_mentioned_as_progress(tmp_path):
    files(tmp_path, "a.py")
    wbs, _ = make(tmp_path, leaf("1", "半分", evidence=["a.py", "later.py"]))
    assert collect(wbs, tmp_path, TODAY) == []                       # PR の文脈が無ければ黙る
    (finding,) = collect(wbs, tmp_path, TODAY, ["a.py"])
    assert finding.code == "partial" and finding.relevant and "残り 1 件" in finding.message


def test_windows_style_paths_in_the_changed_list_still_match(tmp_path):
    files(tmp_path, "pkg/mod.py")
    wbs, _ = make(tmp_path, leaf("1", "x", evidence=["pkg/mod.py"]))
    assert collect(wbs, tmp_path, TODAY, ["pkg\\mod.py"])[0].relevant


# ===== (2) 書く / rendering =============================================================================

def test_the_comment_starts_with_the_marker_and_shows_how_to_mark_a_node_done(tmp_path):
    files(tmp_path, "a.py")
    wbs, _ = make(tmp_path, leaf("1", "実装済み", evidence=["a.py"]))
    body = render(collect(wbs, tmp_path, TODAY, ["a.py"]), file="wbs/w.yaml", as_of=TODAY,
                  changed_known=True)
    assert body.startswith(MARKER)
    assert "**この PR に関係する**" in body and "- [ ] `1` 実装済み" in body
    assert "status: done" in body and "done_on: 2026-10-02" in body
    assert "変更ファイルは取得していません" not in body
    unknown = render(collect(wbs, tmp_path, TODAY), file="wbs/w.yaml", as_of=TODAY,
                     changed_known=False)
    assert "**そのほか（WBS 全体）**" in unknown and "変更ファイルは取得していません" in unknown


def test_no_findings_says_so(tmp_path):
    body = render([], file="wbs/w.yaml", as_of=TODAY, changed_known=True)
    assert body.startswith(MARKER) and "ありません" in body


def test_text_a_pr_author_controls_cannot_inject_markup_mentions_or_the_marker():
    nasty = "<script>alert(1)</script> @everyone `rm` <!-- aipmo-wbs-drift --> | x\n# 見出し\n> 引用"
    out = neutralise(nasty, 500)
    assert "<" not in out and ">" not in out and "`" not in out and "\n" not in out
    assert "@everyone" not in out and "@\u200beveryone" in out
    assert MARKER not in out and "|" not in out
    assert len(neutralise("あ" * 1000)) == 140 and neutralise("あ" * 1000).endswith("…")


def test_a_hostile_node_name_cannot_forge_the_marker_in_the_rendered_comment(tmp_path):
    files(tmp_path, "a.py")
    wbs, _ = make(tmp_path, leaf("1", "<!-- aipmo-wbs-drift --> @all", evidence=["a.py"]))
    body = render(collect(wbs, tmp_path, TODAY), file="wbs/w.yaml", as_of=TODAY, changed_known=False)
    assert body.count(MARKER) == 1 and "@all" not in body


def test_a_long_list_is_cut_and_the_rest_is_counted(tmp_path):
    files(tmp_path, "a.py")
    wbs, _ = make(tmp_path, *[leaf(str(i), f"作業{i}", evidence=["a.py"]) for i in range(1, 41)])
    body = render(collect(wbs, tmp_path, TODAY), file="wbs/w.yaml", as_of=TODAY, changed_known=False)
    assert body.count("- [ ]") == 30 and "ほか 10 件" in body and len(body) < 60_100


# ===== (3) 変更ファイル / changed files ===================================================================

git = shutil.which("git")


@pytest.mark.skipif(git is None, reason="git is not installed")
def test_changed_files_come_from_the_diff_against_the_base_including_deletions(tmp_path):
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
                            "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})

    run("init", "-q", "-b", "main")
    files(tmp_path, "keep.py", "gone.py")
    run("add", "-A")
    run("commit", "-q", "-m", "base")
    run("checkout", "-q", "-b", "feature")
    files(tmp_path, "new.py")
    (tmp_path / "gone.py").unlink()
    run("add", "-A")
    run("commit", "-q", "-m", "change")
    assert sorted(changed_files("main", tmp_path)) == ["gone.py", "new.py"]


def test_a_bad_base_is_refused_without_running_git(tmp_path):
    calls: list = []
    for bad in ("--output=/tmp/x", "a b", "$(whoami)", "x;y", ""):
        with pytest.raises(NotifyError):
            changed_files(bad, tmp_path, runner=lambda *a, **k: calls.append(a))
    assert calls == []


def test_git_failures_are_reported(tmp_path):
    class Done:
        returncode, stdout, stderr = 128, "", "fatal: bad revision"

    with pytest.raises(NotifyError, match="bad revision"):
        changed_files("origin/main", tmp_path, runner=lambda *a, **k: Done())

    def boom(*a, **k):
        raise OSError("no git")

    with pytest.raises(NotifyError, match="git を実行できません"):
        changed_files("origin/main", tmp_path, runner=boom)


# ===== (4) コメントは 1 つ / one sticky comment =============================================================

class FakeGitHub:
    def __init__(self) -> None:
        self.comments: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.status_override: int | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def _handle(self, method):
                payload = self._body() if method in ("POST", "PATCH") else {}
                outer.requests.append((method, self.path, payload))
                if self.headers.get("Authorization") != "Bearer secret-token":
                    return self._send(401, {"message": "bad credentials"})
                if outer.status_override:
                    return self._send(outer.status_override, {"message": "nope"})
                if method == "GET":
                    query = __import__("urllib.parse").parse.parse_qs(
                        __import__("urllib.parse").parse.urlparse(self.path).query)
                    page = int(query.get("page", ["1"])[0])
                    return self._send(200, outer.comments[(page - 1) * 100: page * 100])
                if method == "POST":
                    item = {"id": 1000 + len(outer.comments), "body": payload["body"],
                            "user": {"login": "github-actions[bot]", "type": "Bot"},
                            "html_url": "https://example/c"}
                    outer.comments.append(item)
                    return self._send(201, item)
                cid = int(self.path.rsplit("/", 1)[1])
                item = next(c for c in outer.comments if c["id"] == cid)
                item["body"] = payload["body"]
                return self._send(200, item)

            do_GET = lambda self: self._handle("GET")      # noqa: E731
            do_POST = lambda self: self._handle("POST")    # noqa: E731
            do_PATCH = lambda self: self._handle("PATCH")  # noqa: E731

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, body: str, login="github-actions[bot]", type_="Bot") -> dict:
        item = {"id": 500 + len(self.comments), "body": body, "html_url": "u",
                "user": {"login": login, "type": type_}}
        self.comments.append(item)
        return item

    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def gh():
    server = FakeGitHub()
    yield server
    server.close()


def client(gh, **kw) -> GitHubComments:
    return GitHubComments("acme/widgets", "secret-token", gh.url, **kw)


BODY = MARKER + "\n本文 A"


def test_the_first_finding_creates_the_comment_then_updates_never_duplicates(gh):
    c = client(gh)
    assert c.post_or_update(7, BODY, has_findings=True)["action"] == "created"
    assert c.post_or_update(7, BODY, has_findings=True)["action"] == "unchanged"
    assert c.post_or_update(7, MARKER + "\n本文 B", has_findings=True)["action"] == "updated"
    assert len(gh.comments) == 1 and gh.comments[0]["body"].endswith("本文 B")
    assert [r[0] for r in gh.writes()] == ["POST", "PATCH"]            # 変わらなければ書かない


def test_nothing_is_posted_when_there_never_was_a_finding_but_a_fixed_one_is_updated(gh):
    c = client(gh)
    assert c.post_or_update(7, MARKER + "\nなし", has_findings=False)["action"] == "skipped"
    assert gh.writes() == []
    gh.add(BODY)
    assert c.post_or_update(7, MARKER + "\nなし", has_findings=False)["action"] == "updated"
    assert gh.comments[0]["body"].endswith("なし")


def test_a_comment_that_merely_imitates_the_marker_is_never_updated(gh):
    gh.add(BODY, login="mallory", type_="User")                         # 人が目印を真似て書いた
    gh.add("前置き\n" + BODY)                                            # Bot でも先頭に目印が無い
    c = client(gh)
    assert c.post_or_update(7, BODY, has_findings=True)["action"] == "created"
    assert [r[0] for r in gh.writes()] == ["POST"]
    assert gh.comments[0]["body"] == BODY and len(gh.comments) == 3


def test_a_personal_token_can_name_its_own_login(gh):
    gh.add(BODY, login="maintainer", type_="User")
    other = client(gh, author="maintainer")
    assert other.post_or_update(7, MARKER + "\n新", has_findings=True)["action"] == "updated"
    assert len(gh.comments) == 1


def test_the_comment_is_found_on_a_later_page(gh):
    for i in range(120):
        gh.add(f"雑談 {i}", login="someone", type_="User")
    gh.add(BODY)
    assert client(gh).post_or_update(7, MARKER + "\n更新", has_findings=True)["action"] == "updated"
    assert len(gh.comments) == 121


def test_errors_are_explained_and_the_token_is_never_echoed(gh):
    with pytest.raises(NotifyError, match="401"):
        GitHubComments("acme/widgets", "wrong", gh.url).post_or_update(7, BODY, has_findings=True)
    gh.status_override = 403
    with pytest.raises(NotifyError, match="pull-requests: write") as caught:
        client(gh).post_or_update(7, BODY, has_findings=True)
    assert "secret-token" not in str(caught.value)
    with pytest.raises(NotifyError, match="owner/name"):
        GitHubComments("not-a-repo", "t")
    with pytest.raises(NotifyError, match="GITHUB_TOKEN"):
        GitHubComments("a/b", "")
    with pytest.raises(NotifyError, match="接続できません"):
        GitHubComments("a/b", "t", "http://127.0.0.1:9", timeout=1).find(1)


# ===== (5) CLI とワークフロー / CLI and the workflow ========================================================

def cli_wbs(tmp_path: Path):
    files(tmp_path, "a.py")
    make(tmp_path, leaf("1", "実装済み", evidence=["a.py"]))
    return ["wbs", "notify", "wbs/w.yaml", "--root", str(tmp_path)]


def test_cli_prints_the_body_and_writes_nothing_by_default(tmp_path, capsys, gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("GITHUB_API_URL", gh.url)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    args = cli_wbs(tmp_path)
    assert cli.main([*args, "--changed", "a.py", "--as-of", "2026-10-02", "--pr", "7"]) == 0
    out = capsys.readouterr()
    assert MARKER in out.out and "実装済み" in out.out and "dry run: 1" in out.err
    assert gh.requests == []                                              # --post が無ければ通信しない


def test_cli_posts_with_post_and_reports_what_it_did(tmp_path, capsys, gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("GITHUB_API_URL", gh.url)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    args = cli_wbs(tmp_path)
    assert cli.main([*args, "--pr", "7", "--post", "--as-of", "2026-10-02"]) == 0
    assert "作りました" in capsys.readouterr().out and len(gh.comments) == 1
    assert cli.main([*args, "--pr", "7", "--post", "--as-of", "2026-10-02"]) == 0
    assert "更新の必要がありません" in capsys.readouterr().out
    assert [r[0] for r in gh.writes()] == ["POST"]


def test_cli_refuses_post_without_a_pr_or_a_token_and_reports_a_bad_wbs(tmp_path, capsys, monkeypatch):
    args = cli_wbs(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    assert cli.main([*args, "--post"]) == 1 and "--pr" in capsys.readouterr().err
    assert cli.main([*args, "--pr", "7", "--post"]) == 1 and "GITHUB_TOKEN" in capsys.readouterr().err
    assert cli.main(["wbs", "notify", "missing.yaml", "--root", str(tmp_path)]) == 1
    assert cli.main([*args, "--as-of", "bad"]) == 1
    assert cli.main([*args, "--base=--evil"]) == 1


def test_the_real_projects_wbs_can_be_reported_without_error(capsys):
    root = Path(__file__).resolve().parents[1]
    assert cli.main(["wbs", "notify", "wbs/aipmo.yaml", "--root", str(root),
                     "--changed", "aipmo/wbs_notify.py"]) == 0
    assert MARKER in capsys.readouterr().out


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "wbs-drift.yml"


def test_the_workflow_is_informational_skips_forks_and_has_only_the_permissions_it_needs():
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = flow.get("on") or flow.get(True)
    assert list(triggers) == ["pull_request"]                            # pull_request_target は使わない
    assert flow["permissions"] == {"contents": "read", "pull-requests": "write"}
    job = flow["jobs"]["notify"]
    assert job["continue-on-error"] is True
    assert "head.repo.full_name == github.repository" in job["if"]       # fork では動かさない
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--post" in text and "secrets.GITHUB_TOKEN" in text and "fetch-depth: 0" in text
    # PR の値は環境変数経由で渡し、シェルに直接埋め込まない
    run = next(s["run"] for s in job["steps"] if "run" in s and "wbs notify" in s["run"])
    assert "${{" not in run
