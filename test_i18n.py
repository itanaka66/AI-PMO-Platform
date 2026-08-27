"""多言語カタログのテスト / message catalogue tests.

翻訳の抜けは実行時まで気づけない。ここで機械的に潰す。
Missing translations are invisible until runtime; catch them mechanically here.
"""
from __future__ import annotations

import pytest

from aipmo import i18n
from aipmo.setup_wizard import run_interactive

REFERENCE = i18n.CATALOG[i18n.DEFAULT_LANG]


@pytest.mark.parametrize("lang", sorted(i18n.CATALOG))
def test_no_missing_keys(lang):
    missing = set(REFERENCE) - set(i18n.CATALOG[lang])
    assert not missing, f"{lang} に不足: {sorted(missing)}"


@pytest.mark.parametrize("lang", sorted(i18n.CATALOG))
def test_no_extra_keys(lang):
    """余分なキーは、消し忘れか英語側の消し忘れのどちらか。"""
    extra = set(i18n.CATALOG[lang]) - set(REFERENCE)
    assert not extra, f"{lang} に余分: {sorted(extra)}"


@pytest.mark.parametrize("lang", sorted(i18n.CATALOG))
def test_no_blank_values(lang):
    blank = [k for k, v in i18n.CATALOG[lang].items() if not v.strip()]
    assert not blank, f"{lang} が空: {blank}"


@pytest.mark.parametrize("lang", sorted(i18n.CATALOG))
def test_every_language_is_listed(lang):
    """LANGUAGES と CATALOG がずれると、選択肢に出ない言語ができる。"""
    assert lang in i18n.LANGUAGES


# 英語と同一で正しい組み合わせ / legitimately identical to English.
# 除外は個別に列挙する。条件を緩めると本物の翻訳漏れも通ってしまう。
# Listed one by one on purpose: loosening the rule would let real misses through.
ALLOWED_IDENTICAL = {
    ("de", "tenant_prompt"),   # 「Name」はドイツ語でも同じ綴り / same spelling in German
}


@pytest.mark.parametrize("lang", sorted(i18n.CATALOG))
def test_untranslated_strings_are_not_copies(lang):
    """英語のまま貼り付けられた翻訳漏れを検出する。

    URL やコマンドを含む行は言語に依らず同一で正しいので除外する。
    Lines containing a URL or a command are legitimately identical, so they are
    excluded from the comparison.
    """
    if lang == i18n.DEFAULT_LANG:
        pytest.skip("reference language")

    suspicious = [
        key for key, value in i18n.CATALOG[lang].items()
        if value == REFERENCE[key]
        and "http" not in value
        and "[1]" not in value
        and (lang, key) not in ALLOWED_IDENTICAL
    ]
    assert not suspicious, f"{lang} が英語のまま: {suspicious}"


# --- 言語判定 / detection -------------------------------------------------

@pytest.mark.parametrize("tag,expected", [
    ("ja_JP.UTF-8", "ja"),
    ("ja", "ja"),
    ("zh-Hans-CN", "zh"),
    ("zh_CN.UTF-8", "zh"),
    ("ko_KR", "ko"),
    ("pt_BR.UTF-8", "pt"),
    ("es-MX", "es"),
    ("en_US.UTF-8", "en"),
    ("sv_SE", "en"),      # 未対応言語は英語 / unsupported falls back
    ("", "en"),
    (None, "en"),
])
def test_normalize(tag, expected):
    assert i18n.normalize(tag) == expected


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("AIPMO_LANG", "ko")
    assert i18n.detect() == "ko"


def test_c_locale_is_not_treated_as_a_language(monkeypatch):
    """LANG=C を言語コード 'c' と解釈してはいけない。"""
    monkeypatch.delenv("AIPMO_LANG", raising=False)
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert i18n.detect() == "fr"


def test_translator_falls_back_for_unknown_key(monkeypatch):
    """翻訳漏れが KeyError で異常終了してはいけない。"""
    monkeypatch.setitem(i18n.CATALOG, "xx", {})
    monkeypatch.setitem(i18n.LANGUAGES, "xx", "Test")
    t = i18n.translator("xx")
    assert t("done") == REFERENCE["done"]


# --- ウィザードの多言語動作 / wizard behaviour ----------------------------

class Replies:
    def __init__(self, answers):
        self._answers = list(answers)

    def __call__(self, prompt: str = "") -> str:
        return self._answers.pop(0) if self._answers else ""


@pytest.mark.parametrize("lang", sorted(i18n.CATALOG))
def test_wizard_runs_in_every_language(tmp_path, lang):
    """全言語で最後まで通ること。翻訳中の書式崩れをここで検出する。"""
    lines: list[str] = []
    written = run_interactive(
        tmp_path / lang,
        ask=Replies(["1", "1", "acme_corp", "N"]),
        ask_secret=Replies(["sk-test"]),
        out=lines.append,
        lang=lang,
    )

    assert written["config"].exists()
    assert i18n.CATALOG[lang]["done"] in lines


def test_wizard_output_is_in_the_requested_language(tmp_path):
    lines: list[str] = []
    run_interactive(
        tmp_path,
        ask=Replies(["2", "acme_corp", "N"]),
        ask_secret=Replies([]),
        out=lines.append,
        lang="ko",
    )
    joined = "\n".join(lines)
    assert i18n.CATALOG["ko"]["q_mode"] in joined
    assert i18n.CATALOG["ja"]["q_mode"] not in joined


def test_api_key_never_appears_in_wizard_output(tmp_path):
    """どの言語でも、キーが画面に出てはいけない。"""
    lines: list[str] = []
    run_interactive(
        tmp_path,
        ask=Replies(["1", "1", "acme_corp", "N"]),
        ask_secret=Replies(["sk-super-secret"]),
        out=lines.append,
        lang="es",
    )
    assert "sk-super-secret" not in "\n".join(lines)
