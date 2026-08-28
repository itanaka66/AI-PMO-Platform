"""ガイドの構成が言語間でずれていないかを見る / guide structure across languages.

内容そのものは機械では検査できないが、**節が抜けている**ことは検査できる。
8言語ぶんの食い違いは、放っておくと誰も気づかない。

The prose itself cannot be checked mechanically, but a missing section can be.
Divergence across eight files is otherwise something nobody notices.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from aipmo.i18n import CATALOG

GUIDE_DIR = Path(__file__).resolve().parents[1] / "docs" / "guide"
SOURCE = "ja"


def headings(path: Path) -> list[int]:
    """見出しの深さの並び。文言は言語で変わるが、構造は変わらないはず。"""
    return [
        len(match.group(1))
        for match in re.finditer(r"^(#{1,3}) ", path.read_text(encoding="utf-8"),
                                 re.M)
    ]


def guide(lang: str) -> Path:
    return GUIDE_DIR / f"{lang}.md"


@pytest.mark.parametrize("lang", sorted(CATALOG))
def test_every_supported_language_has_a_guide(lang):
    """UI が対応しているのにガイドが無い、という状態を作らない。

    A language the interface speaks but the guide does not is a gap the reader
    walks straight into.
    """
    assert guide(lang).exists(), f"docs/guide/{lang}.md がありません"


@pytest.mark.parametrize("lang", sorted(CATALOG))
def test_section_structure_matches_the_source(lang):
    assert headings(guide(lang)) == headings(guide(SOURCE)), (
        f"{lang}.md の見出し構成が {SOURCE}.md と異なります"
    )


@pytest.mark.parametrize("lang", sorted(CATALOG))
def test_guides_point_at_files_that_exist(lang):
    """壊れた案内ほど質の悪いものはない。相対リンクを実際に辿る。"""
    text = guide(lang).read_text(encoding="utf-8")
    for target in re.findall(r"\]\((\.\.?/[^)]+)\)", text):
        resolved = (GUIDE_DIR / target).resolve()
        assert resolved.exists(), f"{lang}.md のリンク切れ: {target}"


@pytest.mark.parametrize("lang", sorted(CATALOG))
def test_wizard_question_count_matches_the_guide(lang):
    """ウィザードの質問数が変わったのにガイドが古いまま、を防ぐ。

    実際に一度これで古い手順を7言語へ展開しかけた。
    This nearly propagated a stale walkthrough into seven languages once.
    """
    text = guide(lang).read_text(encoding="utf-8")
    block = re.search(r"```\n(1\).*?)```", text, re.S)
    assert block, f"{lang}.md に設定手順のブロックがありません"
    assert len(re.findall(r"^\d\)", block.group(1), re.M)) == 5


def test_index_lists_every_language():
    index = (GUIDE_DIR / "README.md").read_text(encoding="utf-8")
    for lang in CATALOG:
        assert f"{lang}.md" in index, f"索引に {lang} がありません"
