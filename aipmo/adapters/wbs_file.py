"""WBS ファイルを読むアダプタ（読み取り専用）/ read-only adapter over a WBS file.

PMO AI 自身の開発を管理する WBS（`wbs/aipmo.yaml`）の状況を、テンプレートや
エージェントから引けるようにする。計算は [aipmo/wbs.py](aipmo/wbs.py) にあり、
ここは「どのファイルを読んでよいか」の門番だけを受け持つ。

- 読めるのは `root` の下の `.yaml` / `.yml` だけ。`..` や絶対パスで外へ出られない
  （エージェントに道具として渡しても、任意のファイルは読めない）。
- 書き込み系のアクションは無い。WBS を書き換えるのは人（PR）だけ。

Lets templates and agents ask how the project's own WBS stands. The maths is
in aipmo/wbs.py; this adapter is only the gatekeeper for which file may be
read — only `.yaml` / `.yml` under `root`, never outside it, and nothing here
can write: only people (via a PR) change the WBS.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..wbs import WbsError, analyse, load_wbs
from .base import Adapter, AdapterError, action


class WbsFileAdapter(Adapter):
    name = "wbs_file"

    def __init__(self, root: str = ".", file: str = "wbs/aipmo.yaml", **config: Any) -> None:
        super().__init__(**config)
        self.root = Path(root)
        self.file = file

    def _resolve(self, path: str | None) -> tuple[Path, Path]:
        root = self.root.resolve()
        target = (root / (path or self.file)).resolve()
        if not target.is_relative_to(root):
            raise AdapterError(
                f"wbs_file: root の外は読めません / outside the root: {path}")
        if target.suffix.lower() not in (".yaml", ".yml"):
            raise AdapterError(
                f"wbs_file: .yaml / .yml だけ読めます / only YAML files: {path}")
        return root, target

    def health_check(self) -> bool:
        try:
            return self._resolve(None)[1].exists()
        except AdapterError:
            return False

    def _load(self, path: str | None, as_of: str | None) -> dict[str, Any]:
        root, target = self._resolve(path)
        try:
            day = date.fromisoformat(as_of[:10]) if as_of else date.today()
        except ValueError as exc:
            raise AdapterError(f"wbs_file: as_of を読めません / bad date: {as_of!r}") from exc
        try:
            wbs, problems = load_wbs(target)
        except WbsError as exc:
            raise AdapterError(f"wbs_file: {exc}") from exc
        return analyse(wbs, root, day, problems)

    @action()
    def status(self, path: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        """WBS の状況（進捗・速度・完了見込み・クリティカルパス・要確認）を返す。

        `items` は Task Engine がそのまま拾える形、`summary_text` は人が読む報告。
        証拠（evidence）が実在するかもここで確かめる。
        """
        return self._load(path, as_of)

    @action()
    def check(self, path: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        """WBS の誤りだけを返す（error があれば ok は false）。"""
        analysis = self._load(path, as_of)
        return {"ok": analysis["error_count"] == 0,
                "error_count": analysis["error_count"],
                "warning_count": analysis["warning_count"],
                "problems": analysis["problems"]}
