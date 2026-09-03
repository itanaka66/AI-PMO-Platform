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


# --- 記述が実装とずれていないか / documentation against the implementation ---

ROOT = GUIDE_DIR.parents[1]


def test_documented_commands_exist():
    """ガイドに載っているコマンドが、実際に存在すること。

    無いコマンドを案内されると、そこで手が止まる。
    A command that does not exist stops the reader dead.
    """
    import re

    from aipmo import cli

    parser_source = Path(cli.__file__).read_text(encoding="utf-8")
    available = set(re.findall(r'sub\.add_parser\(\s*"(\w+)"', parser_source))

    documented = set(re.findall(r"^aipmo (\w+)", guide("ja").read_text(encoding="utf-8"),
                                re.M))
    assert documented, "ガイドにコマンドの記載がありません"
    assert documented <= available, f"存在しないコマンド: {documented - available}"


def test_every_command_is_documented_somewhere():
    """実装したのに案内していないコマンドを残さない。

    serve と schedule が8言語すべてで抜けていたことがある。
    Both `serve` and `schedule` were once missing from all eight guides.
    """
    import re

    from aipmo import cli

    parser_source = Path(cli.__file__).read_text(encoding="utf-8")
    available = set(re.findall(r'sub\.add_parser\(\s*"(\w+)"', parser_source))

    text = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "INSTALL.md")
    ) + guide("ja").read_text(encoding="utf-8")

    missing = {name for name in available if f"aipmo {name}" not in text}
    assert not missing, f"案内されていないコマンド: {sorted(missing)}"


@pytest.mark.parametrize("path", sorted((ROOT / "docs").rglob("*.md"))
                         + [ROOT / "README.md", ROOT / "INSTALL.md"],
                         ids=lambda p: p.stem)
def test_internal_links_resolve(path):
    """壊れた案内ほど質の悪いものはない。"""
    import re

    for target in re.findall(r"\]\(([^)#]+)\)", path.read_text(encoding="utf-8")):
        if target.startswith(("http", "mailto")):
            continue
        assert (path.parent / target).resolve().exists(), \
            f"{path.name} のリンク切れ: {target}"


def test_every_shipped_template_is_listed_in_the_readme():
    """作ったのに案内していないテンプレートを残さない。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for template in (ROOT / "templates").rglob("*.yaml"):
        assert template.stem in readme, f"README に無い: {template.stem}"


def test_every_documentation_file_is_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in (ROOT / "docs").glob("*.md"):
        assert doc.name in readme, f"README から辿れない: {doc.name}"


def test_the_documented_test_count_matches_reality():
    """数字を手で書くと必ずずれる。実際に一度ずれた。

    Hand-written counts drift; this one already had.
    """
    import re

    collected = sum(
        len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.M))
        for path in (ROOT / "tests").glob("test_*.py")
    )

    for name in ("README.md", "MANIFEST.md"):
        for stated in re.findall(r"(\d{3}) ?(?:件|tests)",
                                 (ROOT / name).read_text(encoding="utf-8")):
            # パラメータ化で実数は増えるので、下回っていないことだけ見る。
            # Parametrisation inflates the real number, so only the floor is checked.
            assert int(stated) >= collected * 0.5, (
                f"{name} の記載 {stated} 件が、定義済みテスト {collected} 件と"
                f"大きく食い違っています"
            )


# --- ライセンス / licensing --------------------------------------------------

def test_a_license_file_exists_and_names_mit():
    """OSS 公開にはライセンスが要る。無いと、利用者は法的に使えない。

    Without a licence file the default is "all rights reserved", so nobody can
    legally use it.
    """
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Copyright (c)" in text
    assert "WITHOUT WARRANTY OF ANY KIND" in text


def test_the_license_has_no_placeholder_left_in_it():
    """雛形の <year> や <name> が残ったまま公開されると意味を成さない。"""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    for placeholder in ("<year>", "<name>", "[year]", "[fullname]", "YOUR NAME"):
        assert placeholder not in text, f"雛形が残っています: {placeholder}"


def test_the_package_metadata_declares_the_license():
    """配布物にライセンスが入らないと、受け取った側が条件を確認できない。"""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)

    project = config["project"]
    assert project["license"] == {"file": "LICENSE"}
    assert any("MIT" in c for c in project.get("classifiers", []))


def test_the_license_is_findable_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "LICENSE" in readme
    assert "MIT" in readme


def test_dependency_licensing_is_documented():
    """依存ライブラリには本体のライセンスが及ばない。

    psycopg は LGPL。使う側がそれを知らないまま配布する事態を避ける。
    psycopg is LGPL; nobody should redistribute without knowing that.
    """
    notice = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    for library in ("PyYAML", "openai", "anthropic", "psycopg", "qdrant-client", "FastAPI"):
        assert library in notice, f"NOTICE に記載がありません: {library}"
    assert "LGPL" in notice


def test_the_copyright_names_the_company():
    """権利は法人が保有する。個人名のままだと、後から移す手続きが要る。

    The corporation holds the rights; an individual's name would mean a
    transfer later.
    """
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "agNedia Inc." in licence

    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)
    assert config["project"]["authors"] == [{"name": "agNedia Inc."}]


def test_nothing_in_the_repository_is_advertised_as_paid():
    """このリポジトリにあるものは、すべて無料。
    有償のテンプレートはここに置かない、が方針。

    Everything here is free; paid templates are kept out of the repository.
    Wording that implies otherwise would misrepresent what a reader receives.
    """
    # 「試用版ではない」と否定している文まで拾わないよう、
    # 有償であると読める言い回しだけを見る。
    # Phrased to catch a claim, not its denial: "not a trial" must pass.
    claims = ("は有料", "有料版です", "Pro 版のみ", "購入が必要", "requires purchase",
              "paid edition", "paid tier")
    for name in ("README.md", "NOTICE.md", "MANIFEST.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for claim in claims:
            assert claim not in text, f"{name} に紛らわしい表現: {claim}"


def test_the_free_terms_are_stated_in_every_guide():
    """8言語すべてで、無料であることが分かること。"""
    markers = {"ja": "すべて無料", "en": "All of it is free", "zh": "全部免费",
               "ko": "전부 무료", "es": "Todo es gratuito", "fr": "Tout est gratuit",
               "de": "Alles davon ist kostenlos", "pt": "Tudo é gratuito"}
    for lang, marker in markers.items():
        assert marker in guide(lang).read_text(encoding="utf-8"), lang
