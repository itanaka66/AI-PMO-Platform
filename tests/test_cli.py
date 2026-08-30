"""CLI のテスト / CLI tests.

`aipmo run` の承認プロンプト（`_confirm_agent_write`）だけを見る。
実際の入出力の見た目ではなく、y/N の判定と EOF の扱いが正しいことが
確かめたい点。

Only `_confirm_agent_write`, the approval prompt `aipmo run` offers, is
covered here. What matters is the y/N decision and EOF handling, not how the
prompt looks.
"""
from __future__ import annotations

import pytest

from aipmo.approval import SlackApprover
from aipmo.cli import ConfigError, _confirm_agent_write, build_engine


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


# --- config.yaml の approval.slack / build_engine の配線 --------------------
# --- config.yaml's approval.slack / build_engine wiring ---------------------

def test_build_engine_wires_slack_approval_when_configured():
    config = {
        "adapters": {"mode": "mock"},
        "approval": {"slack": {"channel": "#approvals", "timeout_seconds": 42,
                               "poll_seconds": 2}},
    }
    engine = build_engine(config)

    assert isinstance(engine.approve, SlackApprover)
    assert engine.approve.channel == "#approvals"
    assert engine.approve.timeout_seconds == 42
    assert engine.approve.poll_seconds == 2


def test_build_engine_slack_approval_overrides_the_passed_in_approve():
    """明示的な運用設定の方が、呼び出し元既定の承認方法より優先する。"""
    config = {
        "adapters": {"mode": "mock"},
        "approval": {"slack": {"channel": "#approvals"}},
    }
    engine = build_engine(config, approve=lambda tool, args: True)

    assert isinstance(engine.approve, SlackApprover)


def test_build_engine_without_approval_config_keeps_the_passed_in_approve():
    passed_in = lambda tool, args: True
    engine = build_engine({"adapters": {"mode": "mock"}}, approve=passed_in)

    assert engine.approve is passed_in


def test_approval_slack_requires_a_slack_adapter():
    config = {
        "adapters": {"mode": "real"},   # slack 未設定
        "approval": {"slack": {"channel": "#approvals"}},
    }
    with pytest.raises(ConfigError, match="adapters.slack"):
        build_engine(config)


def test_approval_slack_requires_a_channel():
    config = {
        "adapters": {"mode": "mock"},
        "approval": {"slack": {}},
    }
    with pytest.raises(ConfigError, match="channel"):
        build_engine(config)
