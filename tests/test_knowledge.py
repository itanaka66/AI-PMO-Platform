"""公開可能性スコアのテスト。

最終判断は人間が行う仕組みなので、ここでは「数値がどう動くか」
「根拠が人に読める形で残るか」だけを見る。承認・却下の判定は対象外。

The final call is a human's, so these only check how the number moves and
that the reasons stay human-readable — never an approve/reject verdict.
"""
from __future__ import annotations

from aipmo.knowledge import score_publicability


def test_consent_a_forces_zero_regardless_of_level_or_content():
    result = score_publicability(
        {"text": "一般化された安全な内容"},
        knowledge_level=6, consent_level="A",
    )
    assert result.value == 0.0
    assert any("A" in reason for reason in result.reasons)


def test_higher_knowledge_level_scores_higher_all_else_equal():
    low = score_publicability({"text": "内容"}, knowledge_level=3, consent_level="B")
    high = score_publicability({"text": "内容"}, knowledge_level=6, consent_level="B")
    assert high.value > low.value


def test_consent_c_adds_a_bonus_over_b():
    b = score_publicability({"text": "内容"}, knowledge_level=5, consent_level="B")
    c = score_publicability({"text": "内容"}, knowledge_level=5, consent_level="C")
    assert c.value == b.value + 10


def test_unknown_consent_is_halved_relative_to_b():
    b = score_publicability({"text": "内容"}, knowledge_level=5, consent_level="B")
    unknown = score_publicability({"text": "内容"}, knowledge_level=5, consent_level=None)
    assert unknown.value == b.value / 2


def test_an_email_address_is_penalized():
    clean = score_publicability({"text": "主要担当者への依存はリスクになる"},
                                knowledge_level=5, consent_level="B")
    leaky = score_publicability({"text": "連絡は taro@example.com まで"},
                                knowledge_level=5, consent_level="B")
    assert leaky.value < clean.value
    assert any("メールアドレス" in reason for reason in leaky.reasons)


def test_the_tenant_name_appearing_in_text_is_penalized():
    result = score_publicability(
        {"text": "acme_corp 社内での事例"},
        knowledge_level=5, consent_level="B", tenant="acme_corp",
    )
    clean = score_publicability(
        {"text": "ある企業での事例"},
        knowledge_level=5, consent_level="B", tenant="acme_corp",
    )
    assert result.value < clean.value
    assert any("テナント名" in reason for reason in result.reasons)


def test_an_issue_key_is_penalized():
    result = score_publicability(
        {"text": "PROJ-482 の対応から得た知見"},
        knowledge_level=5, consent_level="B",
    )
    assert any("課題番号" in reason for reason in result.reasons)


def test_the_score_never_goes_below_zero():
    result = score_publicability(
        {"text": "acme_corp の担当者 taro@example.com、課題 PROJ-1"},
        knowledge_level=3, consent_level=None, tenant="acme_corp",
    )
    assert result.value == 0.0


def test_the_score_never_exceeds_one_hundred():
    result = score_publicability(
        {"text": "十分に一般化された、業界横断で通用する知見"},
        knowledge_level=6, consent_level="C",
    )
    assert result.value <= 100.0


def test_reasons_are_always_present_and_readable():
    result = score_publicability({"text": "内容"}, knowledge_level=4, consent_level="B")
    assert result.reasons
    assert all(isinstance(r, str) and r for r in result.reasons)


def test_nested_knowledge_fields_are_all_scanned_for_leaks():
    """text 以外のキーに書かれていても見逃さない。"""
    result = score_publicability(
        {"pattern": "risk", "details": {"contact": "taro@example.com"}},
        knowledge_level=5, consent_level="B",
    )
    assert any("メールアドレス" in reason for reason in result.reasons)
