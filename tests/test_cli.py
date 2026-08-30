"""CLI のテスト / CLI tests.

`aipmo run` の承認プロンプト（`_confirm_agent_write`）だけを見る。
実際の入出力の見た目ではなく、y/N の判定と EOF の扱いが正しいことが
確かめたい点。

Only `_confirm_agent_write`, the approval prompt `aipmo run` offers, is
covered here. What matters is the y/N decision and EOF handling, not how the
prompt looks.
"""
from __future__ import annotations

from aipmo.cli import _confirm_agent_write


def test_confirm_agent_write_accepts_y(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _confirm_agent_write("jira.create_issues", {"project": "PROJ"}) is True


def test_confirm_agent_write_accepts_yes_case_insensitively(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    assert _confirm_agent_write("jira.create_issues", {"project": "PROJ"}) is True


def test_confirm_agent_write_declines_on_empty_input(monkeypatch):
    """Enter だけ押した場合は既定で断る側 — [y/N] の N が既定。"""
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert _confirm_agent_write("jira.create_issues", {"project": "PROJ"}) is False


def test_confirm_agent_write_declines_on_n(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _confirm_agent_write("jira.create_issues", {"project": "PROJ"}) is False


def test_confirm_agent_write_declines_on_eof(monkeypatch):
    """対話できなかった場合の保険。承認できないのだから断る。"""
    def raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert _confirm_agent_write("jira.create_issues", {"project": "PROJ"}) is False
