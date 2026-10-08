"""`aipmo learning`（自己学習サイクルの RAG 信用トグル）の CLI テスト。

アダプタ側の自動承認ロジックは tests/test_self_learning.py で確認済み。
ここでの主眼は CLI の状態表示と切り替え、台帳（SQLite）への永続化。

The auto-approval logic itself is covered in tests/test_self_learning.py.
The focus here is the CLI's status display, the toggle, and persistence to
the (SQLite) ledger.
"""
from __future__ import annotations

from pathlib import Path

from aipmo import cli


def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("tenant: acme\nllm:\n  default: {provider: echo}\n", encoding="utf-8")
    return path


def test_status_defaults_to_disabled(tmp_path, capsys):
    assert cli.main(["--config", str(config(tmp_path)), "learning"]) == 0
    assert "無効" in capsys.readouterr().out


def test_enable_then_status_reflects_it(tmp_path, capsys):
    path = config(tmp_path)
    assert cli.main(["--config", str(path), "learning", "enable"]) == 0
    assert "有効にしました" in capsys.readouterr().out

    assert cli.main(["--config", str(path), "learning"]) == 0
    assert "有効" in capsys.readouterr().out


def test_disable_after_enable_turns_it_back_off(tmp_path, capsys):
    path = config(tmp_path)
    cli.main(["--config", str(path), "learning", "enable"])
    capsys.readouterr()

    assert cli.main(["--config", str(path), "learning", "disable"]) == 0
    assert "無効にしました" in capsys.readouterr().out
    assert cli.main(["--config", str(path), "learning"]) == 0
    assert "無効" in capsys.readouterr().out


def test_the_setting_persists_across_separate_cli_invocations(tmp_path, capsys):
    """同じプロセス内のメモリではなく、台帳に書かれていること。"""
    path = config(tmp_path)
    cli.main(["--config", str(path), "learning", "enable"])
    capsys.readouterr()

    # 新しい main() 呼び出し = 新しい読み込み。ファイルに残っていることを確かめる。
    cli.main(["--config", str(path), "learning"])
    assert "有効" in capsys.readouterr().out
