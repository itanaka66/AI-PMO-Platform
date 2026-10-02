"""台帳の「隣のファイル」の置き場（ブリーフィング・判断ログ・状態など）。

PMO Core は、台帳のタスクのほかに、次のものを残す：

| 名前 | 中身 | 形 |
|---|---|---|
| `pmo-briefing.json` | 直近のブリーフィング（画面・CLI が読む） | 文書（上書き） |
| `pmo-core-state.json` | 警告の継続・通知の履歴・役割AIの件数など、周をまたぐ状態 | 文書（上書き） |
| `pmo-learned.json` | 学習した補正 | 文書（上書き） |
| `pmo-judgment-control.json` | 自律的な判断の一時停止・遮断器の解除（CLI が書き、常駐が読む） | 文書（上書き） |
| `pmo-decisions.jsonl` | 判断ログ（追記だけ。レビュー件数・反映の記録もここから数える） | ログ（追記） |

これまでは台帳（SQLite）の隣の**ローカルファイル**だった。そのため、常駐の `aipmo schedule` と
`aipmo serve` を別のホストに分けると、serve はブリーフィングを読めなかった。ここに置き場を抽象化して、
2 通りにする：

- `FileSide` … 台帳の隣のファイル（従来どおり。SQLite の既定）。
- `DbSide` … 台帳と同じデータベースの表（PostgreSQL の既定。テナントごとに行で分かれる）。
  schedule と serve が別ホストでも、同じ PostgreSQL を見れば同じブリーフィング・判断ログを見る。

`task_engine.side_storage: auto | file | database` で選ぶ（`auto` は PostgreSQL なら database、
SQLite なら file）。SQLite でも `database` にできる（台帳の `.db` ひとつに全部入る）。

Where the files beside the ledger live. Previously local files next to the SQLite ledger, so
splitting `schedule` and `serve` across hosts left `serve` without a briefing. Two homes now:
files (the default for SQLite) or tables in the ledger's own database (the default for
PostgreSQL, tenant-scoped), so two hosts on one PostgreSQL see one briefing and one decision log.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

BRIEFING = "pmo-briefing.json"
STATE = "pmo-core-state.json"
LEARNED = "pmo-learned.json"
CONTROL = "pmo-judgment-control.json"
DECISIONS = "pmo-decisions.jsonl"
DOCS = (BRIEFING, STATE, LEARNED, CONTROL)
LOGS = (DECISIONS,)
ALL_NAMES = DOCS + LOGS

STORAGE_MODES = ("auto", "file", "database")


_APPEND_LOCK = threading.Lock()


def _lock_file(handle: Any) -> None:
    """ファイルの末尾への追記を、他のプロセス・スレッドと重ならないようにする。

    Windows の追記モードは「末尾へ移ってから書く」の 2 段で、同時に追記すると行が上書きで
    消える（実測: 4 スレッドで 40 行中 39 行）。OS の排他ロックを取ってから書く。
    """
    if sys.platform == "win32":
        import msvcrt

        deadline = time.monotonic() + 10
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.005)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SideStore(ABC):
    """台帳の隣に置くもの。文書（丸ごと上書き）と、ログ（追記だけ）の 2 種類。"""

    kind = "base"

    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def read_doc(self, name: str) -> str | None:
        """文書を読む。まだ無ければ None。"""

    @abstractmethod
    def write_doc(self, name: str, text: str) -> None:
        """文書を丸ごと置き換える（途中の状態を読ませない）。"""

    @abstractmethod
    def append(self, name: str, line: str) -> None:
        """ログに 1 行足す。過去は書き換えない。"""

    @abstractmethod
    def read_log(self, name: str, after: int = 0) -> tuple[list[str], int]:
        """`after`（前回の戻り値の位置）より後の行と、次の位置。書きかけの行は返さない。"""

    @abstractmethod
    def tail(self, name: str, limit: int) -> list[str]:
        """ログの最後の `limit` 行（古い順）。"""

    def exists(self, name: str) -> bool:
        return self.read_doc(name) is not None if name in DOCS else bool(self.tail(name, 1))

    def close(self) -> None:           # noqa: B027 — 既定は何もしない / nothing to release
        pass


def resolve_ledger_path(ledger: Path | str) -> Path:
    """`*.json` が指定されたら、隣の `*.db` を台帳の実体とする（TaskEngine と同じ）。"""
    p = Path(ledger)
    return p.with_suffix(".db") if p.suffix == ".json" else p


def side_file(ledger: Path | str, name: str) -> Path:
    """台帳の隣に置くファイルの場所。

    既定の台帳 `task-ledger.db` では従来の名前のまま。別名の台帳（`acme.db` など）では
    `acme.` を前に付け、同じディレクトリに置いた複数の台帳がお互いのファイルを上書きしない。
    """
    db = resolve_ledger_path(ledger)
    return db.parent / (name if db.stem == "task-ledger" else f"{db.stem}.{name}")


# =============================================================================
# ファイル / files
# =============================================================================

class FileSide(SideStore):
    kind = "file"

    def __init__(self, ledger: Path | str, overrides: dict[str, Path] | None = None) -> None:
        self.ledger = resolve_ledger_path(ledger)
        self.overrides = dict(overrides or {})

    def path_of(self, name: str) -> Path:
        return self.overrides.get(name) or side_file(self.ledger, name)

    def describe(self) -> str:
        return f"files beside {self.ledger}"

    def read_doc(self, name: str) -> str | None:
        if name in LOGS:
            return None
        try:
            return self.path_of(name).read_text(encoding="utf-8")
        except OSError:
            return None

    def write_doc(self, name: str, text: str) -> None:
        if name in LOGS:
            raise ValueError(f"{name} はログです（append を使う） / a log, use append")
        # 常駐の `aipmo schedule` と、`aipmo assign|agents|pmo` のような CLI は、別プロセスで
        # 同じファイルを書く。一時ファイル名を共有すると、Windows では互いの置き換えが
        # 「別のプロセスが使用中」で失敗する（実測）。名前をプロセスごとに分け、読まれている
        # 最中の置き換えは短く再試行する。
        # The resident and CLIs write the same files from separate processes; a shared temp name
        # collided on Windows (seen in practice), so the name is per process and a replace that
        # races a reader is retried briefly.
        path = self.path_of(name)
        temporary = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            for attempt in range(5):
                try:
                    temporary.replace(path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def append(self, name: str, line: str) -> None:
        if name not in LOGS:
            raise ValueError(f"{name} はログではありません / not a log")
        path = self.path_of(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (line + "\n").encode("utf-8")
        with _APPEND_LOCK, path.open("ab") as handle:
            _lock_file(handle)
            try:
                handle.seek(0, os.SEEK_END)
                handle.write(data)
                handle.flush()
            finally:
                _unlock_file(handle)

    def read_log(self, name: str, after: int = 0) -> tuple[list[str], int]:
        if name not in LOGS:
            return [], 0
        path = self.path_of(name)
        try:
            size = path.stat().st_size
        except OSError:
            return [], 0
        if size < after:                                 # 切り詰め・入れ替え: 最初から
            after = 0
        if size == after:
            return [], after
        with path.open("rb") as handle:
            handle.seek(after)
            data = handle.read()
        end = data.rfind(b"\n") + 1                      # 書きかけの最後の行は次回に回す
        lines = [raw.decode("utf-8", errors="replace") for raw in data[:end].splitlines()]
        return lines, after + end

    def tail(self, name: str, limit: int) -> list[str]:
        if name not in LOGS:
            return []
        try:
            lines = self.path_of(name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:] if limit else lines


# =============================================================================
# データベース / the ledger's database
# =============================================================================

class DbSide(SideStore):
    """台帳と同じデータベースの表。実際の読み書きは台帳の保存先（LedgerStore）が行う。"""

    kind = "database"

    def __init__(self, store: Any) -> None:
        self.store = store

    def describe(self) -> str:
        return f"database tables of {self.store.describe()}"

    def read_doc(self, name: str) -> str | None:
        return self.store.side_read_doc(name)

    def write_doc(self, name: str, text: str) -> None:
        self.store.side_write_doc(name, text)

    def append(self, name: str, line: str) -> None:
        self.store.side_append(name, line)

    def read_log(self, name: str, after: int = 0) -> tuple[list[str], int]:
        return self.store.side_read_log(name, after)

    def tail(self, name: str, limit: int) -> list[str]:
        return self.store.side_tail(name, limit)


def make_side(mode: str, ledger: Path | str, store: Any) -> SideStore:
    """`task_engine.side_storage` の値から、置き場を決める。"""
    mode = (mode or "auto").lower()
    if mode not in STORAGE_MODES:
        raise ValueError(f"side_storage は {', '.join(STORAGE_MODES)} のどれか: {mode!r}")
    if mode == "auto":
        mode = "database" if getattr(store, "kind", "") == "postgres" else "file"
    return DbSide(store) if mode == "database" else FileSide(ledger)


def import_files(source: FileSide, target: SideStore, *, overwrite: bool = False) -> dict[str, str]:
    """ローカルのファイルを、別の置き場（データベース）へ取り込む。移行元は消さない。

    文書は、移行先に既にあれば**上書きしない**（`overwrite` のときだけ上書き）。ログは、移行先に
    1 行でもあれば**常に**足さない（`overwrite` でも。二重に取り込まない）。結果は名前ごとの `imported` / `kept` / `absent`。
    """
    report: dict[str, str] = {}
    for name in DOCS:
        text = source.read_doc(name)
        if text is None:
            report[name] = "absent"
        elif target.read_doc(name) is not None and not overwrite:
            report[name] = "kept"
        else:
            target.write_doc(name, text)
            report[name] = "imported"
    for name in LOGS:
        lines, _ = source.read_log(name, 0)
        if not lines:
            report[name] = "absent"
        elif target.tail(name, 1):                       # ログは、1 行でもあれば足さない（二重にしない）
            report[name] = "kept"
        else:
            for line in lines:
                target.append(name, line)
            report[name] = "imported"
    return report
