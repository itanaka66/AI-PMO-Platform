"""WBS の更新漏れ・証拠の欠けを、プルリクエスト（PR）に知らせる。

WBS（`wbs/aipmo.yaml`）は人が PR で更新する。実装を足したのに WBS を更新し忘れる（証拠が
すべて揃ったのに「完了」にしていない＝`maybe_done`）、証拠のファイルを消したのに「完了」の
ままにしている（`evidence_missing`）、は `aipmo wbs check` では warning / error として出るだけで、
PR を見ている人の目には入らない。ここは、その結果を **PR のコメント**にする。

- **この PR に関係するものを先に出す。** PR で変えたファイル（`--base` との差分、または
  `--changed`）が、ある作業の証拠に当たるとき、その作業を「この PR に関係する」として先頭に並べる
  （例: この PR で最後の証拠が揃ったので、完了にできる）。
- **完了にする書き方をそのまま添える。** `status: done` と `done_on:`（基準日）の行を示す。
  WBS を書き換えるのは人（PR の著者）で、ここは書き換えない。
- **コメントは 1 つだけ。** 目印（`<!-- aipmo-wbs-drift -->`）つきのコメントを見つけて**更新**する。
  PR を更新するたびに増えない。指摘が無くなったら「更新漏れはありません」に更新する
  （もともと指摘が無ければ、何も投稿しない）。
- **何も書かないで確かめられる。** 既定は本文を表示するだけ。`--post` を付けたときだけ GitHub に書く。
- **PR の著者が書いた文字を、そのまま流さない。** 作業名などは PR で変えられるので、HTML・メンション・
  バッククォートを無害にして、長さも切る。

Posts the WBS's drift (done-looking-but-not-marked, evidence gone) as one sticky PR comment, with
the items this PR's changed files touch first and the exact lines to mark a node done. Only the
marked comment is updated, never duplicated; it says "no drift" once fixed and posts nothing when
there never was any. Dry-run by default; text from the PR author is neutralised.
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .wbs import Wbs, check_evidence, validate_evidence

MARKER = "<!-- aipmo-wbs-drift -->"
CODES = ("maybe_done", "evidence_missing", "done_without_evidence")
MAX_ITEMS = 30
MAX_BODY = 60_000          # GitHub のコメントの上限（65,536 字）より小さく


@dataclass
class Finding:
    code: str                        # maybe_done | evidence_missing | done_without_evidence | partial
    node: str
    name: str
    message: str
    relevant: bool = False           # この PR の変更ファイルが、この作業の証拠に当たる
    touched: list[str] = field(default_factory=list)


class NotifyError(RuntimeError):
    pass


# =============================================================================
# 集める / collecting
# =============================================================================

def _matches(path_part: str, changed: set[str]) -> bool:
    spec = path_part.strip().strip("/")
    if not spec:
        return False
    return any(c == spec or c.startswith(spec + "/") or fnmatch.fnmatch(c, spec) for c in changed)


def _evidence_paths(specs: list[str]) -> list[str]:
    return [s.partition("::")[0].strip() for s in specs]


def collect(wbs: Wbs, root: Path, as_of: date, changed: list[str] | None = None) -> list[Finding]:
    """更新漏れ・証拠の欠けを集める。`changed` があれば、この PR に関係するかも付ける。"""
    changed_set = {c.replace("\\", "/").lstrip("./") for c in (changed or [])}
    leaves = {leaf.id: leaf for leaf in wbs.leaves()}
    found: list[Finding] = []
    for problem in validate_evidence(wbs, root, as_of):
        if problem.code not in CODES or problem.node not in leaves:
            continue
        leaf = leaves[problem.node]
        touched = [p for p in _evidence_paths(leaf.evidence) if _matches(p, changed_set)]
        found.append(Finding(problem.code, leaf.id, leaf.name, problem.message,
                             relevant=bool(touched), touched=touched))
    # 一部の証拠だけ揃った（残りがある）作業を、この PR が進めたとき: 参考として出す。
    # A node still open whose evidence this PR moved forward but did not complete.
    for leaf in leaves.values():
        if leaf.done or not leaf.evidence or any(f.node == leaf.id for f in found):
            continue
        touched = [p for p in _evidence_paths(leaf.evidence) if _matches(p, changed_set)]
        results = [check_evidence(spec, root)[0] for spec in leaf.evidence]
        if touched and any(results) and not all(results):
            missing = sum(1 for ok in results if not ok)
            found.append(Finding("partial", leaf.id, leaf.name,
                                 f"証拠の一部がこの PR で揃いました（残り {missing} 件）",
                                 relevant=True, touched=touched))
    found.sort(key=lambda f: (not f.relevant, CODES.index(f.code) if f.code in CODES else 9,
                              f.node))
    return found


# =============================================================================
# 書く / rendering
# =============================================================================

def neutralise(text: str, limit: int = 140) -> str:
    """PR の著者が変えられる文字を、コメントに流して無害なものにする。"""
    value = " ".join(str(text).split())
    value = (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace("`", "'").replace("@", "@\u200b").replace("|", "¦"))
    value = re.sub(r"(?m)^(\s*)([#>*+-])", r"\1\\\2", value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


_ACTION = {
    "maybe_done": "証拠がすべて揃っています。完了にできるか確認してください",
    "evidence_missing": "「完了」ですが、証拠が見つかりません。証拠を直すか、完了を取り消してください",
    "done_without_evidence": "「完了」ですが、証拠（evidence）が書かれていません",
    "partial": "",
}


def render(findings: list[Finding], *, file: str, as_of: date, changed_known: bool) -> str:
    lines = [MARKER, "### WBS の更新漏れ・証拠の欠け（自動チェック）", "",
             f"基準日 {as_of.isoformat()} ・ WBS `{neutralise(file, 80)}`"
             + ("" if changed_known else " ・ この PR の変更ファイルは取得していません"), ""]
    if not findings:
        lines += ["更新漏れ・証拠の欠けはありません。", ""]
    else:
        shown = findings[:MAX_ITEMS]
        for title, picked in (("この PR に関係する", [f for f in shown if f.relevant]),
                              ("そのほか（WBS 全体）", [f for f in shown if not f.relevant])):
            if not picked:
                continue
            lines += [f"**{title}**", ""]
            for f in picked:
                action = _ACTION.get(f.code) or f.message
                lines.append(f"- [ ] `{neutralise(f.node, 40)}` {neutralise(f.name)} — "
                             f"{neutralise(action, 200)}")
                if f.touched:
                    lines.append("  - この PR が変えた証拠: "
                                 + ", ".join(f"`{neutralise(t, 80)}`" for t in f.touched[:3]))
                if f.code == "maybe_done":
                    lines += ["  - 完了にするなら（`wbs` ファイルの該当ノード）:",
                              "    ```yaml", "    status: done",
                              f"    done_on: {as_of.isoformat()}", "    ```"]
            lines.append("")
        if len(findings) > len(shown):
            lines += [f"ほか {len(findings) - len(shown)} 件（`aipmo wbs check {neutralise(file, 80)}` で全部）", ""]
    lines += ["<sub>このコメントは PR の更新のたびに自動で更新されます。WBS を書き換えるのは "
              "PR の著者です（`aipmo wbs check` / docs/SELF-WBS.md）。</sub>"]
    body = "\n".join(lines)
    return body if len(body) <= MAX_BODY else body[:MAX_BODY] + "\n…（長すぎるため省略）"


# =============================================================================
# PR の変更ファイル / the PR's changed files
# =============================================================================

def changed_files(base: str, cwd: Path, runner: Callable[..., Any] = subprocess.run) -> list[str]:
    """`base...HEAD` で変わったファイル（消されたものも含む）。git が無い・失敗したら NotifyError。"""
    if not re.fullmatch(r"[A-Za-z0-9._/@{}~^-]+", base) or base.startswith("-"):
        raise NotifyError(f"--base の形が不正です: {base!r}")
    try:
        done = runner(["git", "diff", "--name-only", "--no-renames", f"{base}...HEAD"], cwd=str(cwd),
                      capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotifyError(f"git を実行できません: {exc}") from exc
    if done.returncode != 0:
        raise NotifyError(f"git diff が失敗しました: {(done.stderr or '').strip()[:200]}")
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


# =============================================================================
# GitHub のコメント / the sticky comment
# =============================================================================

class GitHubComments:
    """PR（Issue）のコメントを、見つけて・作って・更新するだけの最小のクライアント。"""

    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com",
                 timeout: float = 20.0, author: str | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise NotifyError(f"リポジトリは owner/name の形で: {repo!r}")
        if not token:
            raise NotifyError("GITHUB_TOKEN がありません（--post には書き込み権限のあるトークンが要ります）")
        self.repo, self.token, self.timeout = repo, token, timeout
        # 目印つきのコメントの書き手。GITHUB_TOKEN（Actions）なら Bot。個人のトークンなら
        # その login を渡す。他の人が目印を真似て書いたコメントを、更新の対象にしないため。
        # Who wrote our comment: a Bot for the Actions token, else the given login. A comment
        # that merely imitates the marker, by anyone else, is never the one we update.
        self.author = author
        self.api_url = api_url.rstrip("/")

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self.api_url}{path}", method=method,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "aipmo-wbs-notify"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            hint = {401: "トークンを確認してください", 403: "権限がありません（pull-requests: write が要ります。"
                    "fork からの PR では書けません）", 404: "PR 番号かリポジトリを確認してください"}.get(exc.code, "")
            raise NotifyError(f"GitHub が {exc.code} を返しました。{hint} {detail}") from exc
        except urllib.error.URLError as exc:
            raise NotifyError(f"GitHub に接続できません: {exc}") from exc
        return json.loads(raw) if raw else {}

    def find(self, pr: int) -> dict[str, Any] | None:
        for page in range(1, 11):                                   # 1000 件まで
            items = self._call("GET", f"/repos/{self.repo}/issues/{pr}/comments?per_page=100&page={page}")
            for item in items:
                user = item.get("user") or {}
                ours = (user.get("type") == "Bot" if self.author is None
                        else str(user.get("login")) == self.author)
                if ours and str(item.get("body") or "").startswith(MARKER):
                    return dict(item)
            if len(items) < 100:
                break
        return None

    def post_or_update(self, pr: int, body: str, *, has_findings: bool) -> dict[str, Any]:
        existing = self.find(pr)
        if existing is not None:
            if existing.get("body") == body:
                return {"action": "unchanged", "url": existing.get("html_url")}
            updated = self._call("PATCH", f"/repos/{self.repo}/issues/comments/{existing['id']}",
                                 {"body": body})
            return {"action": "updated", "url": updated.get("html_url")}
        if not has_findings:
            return {"action": "skipped", "url": None}               # もともと指摘が無い: 何も投稿しない
        created = self._call("POST", f"/repos/{self.repo}/issues/{pr}/comments", {"body": body})
        return {"action": "created", "url": created.get("html_url")}
