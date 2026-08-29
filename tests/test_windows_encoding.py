"""Windows 向けファイルの文字コード / encodings for the Windows-facing files.

日本語版 Windows で文字化けした、という報告から入った検査です。
原因は3つ別々にあり、どれも「実行してみるまで気づけない」形をしていました。

  1. .bat を cmd は**現在のコードページ**で読む。日本語版なら CP932。
     UTF-8 で置くと化ける。
  2. .ps1 を Windows PowerShell 5.1 は **BOM が無ければ ANSI** として読む。
     UTF-8 で置いても BOM が無ければ化ける。
  3. CP932 に無い文字がある。とくに `—` (U+2014) は CP932 の全角ダッシュ
     `―` (U+2015) と見た目がほぼ同じで、書いた側は気づけない。

Added after a report of garbled text on Japanese Windows. There were three
separate causes, none visible without running it there: cmd reads .bat in the
console code page, PowerShell 5.1 reads a BOM-less .ps1 as ANSI, and `—`
(U+2014) is absent from CP932 while looking almost identical to `―` (U+2015),
which is present.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BATCH = sorted(ROOT.glob("scripts/*.bat")) + sorted(ROOT.glob("**/*.cmd"))
POWERSHELL = sorted(ROOT.glob("scripts/*.ps1")) + sorted(ROOT.glob("installer/*.ps1"))
INNO = sorted(ROOT.glob("installer/*.iss"))
WINDOWS_FILES = BATCH + POWERSHELL + INNO

BOM = b"\xef\xbb\xbf"


def ids(path: Path) -> str:
    return path.name


# --- .bat -------------------------------------------------------------------

@pytest.mark.parametrize("path", BATCH, ids=ids)
def test_batch_files_are_cp932(path):
    """cmd は .bat を現在のコードページで読む。UTF-8 で置くと化ける。"""
    path.read_bytes().decode("cp932")


@pytest.mark.parametrize("path", BATCH, ids=ids)
def test_batch_files_have_no_bom(path):
    """BOM が付くと先頭の @echo off が壊れ、その行がそのまま表示される。

    A BOM corrupts the first line, so `@echo off` is echoed instead of obeyed.
    """
    assert not path.read_bytes().startswith(BOM)


# --- .ps1 -------------------------------------------------------------------

@pytest.mark.parametrize("path", POWERSHELL, ids=ids)
def test_powershell_files_carry_a_utf8_bom(path):
    """Windows PowerShell 5.1 は BOM が無い .ps1 を ANSI として読む。

    PowerShell 7 は UTF-8 が既定なので、CP932 で保存すると今度はそちらが壊れる。
    UTF-8 + BOM だけが両方で正しく読まれる。

    5.1 reads a BOM-less script as ANSI; 7 defaults to UTF-8, so storing CP932
    breaks that one instead. UTF-8 with a BOM is the only form both read.
    """
    assert path.read_bytes().startswith(BOM), f"{path.name} に BOM がありません"


@pytest.mark.parametrize("path", INNO, ids=ids)
def test_inno_setup_scripts_carry_a_utf8_bom(path):
    """Inno Setup 6 も BOM が無いと ANSI 扱いになり、インストーラ画面が化ける。"""
    assert path.read_bytes().startswith(BOM)


# --- 共通 / shared ----------------------------------------------------------

@pytest.mark.parametrize("path", WINDOWS_FILES, ids=ids)
def test_line_endings_are_crlf(path):
    data = path.read_bytes()
    lone_lf = data.count(b"\n") - data.count(b"\r\n")
    assert lone_lf == 0, f"{path.name} に LF だけの行が {lone_lf} 行あります"


@pytest.mark.parametrize("path", WINDOWS_FILES, ids=ids)
def test_every_character_exists_in_cp932(path):
    """日本語版 Windows のコンソールに出せない文字を残さない。

    出せない文字は「?」になる。落ちないぶん、気づかれないまま残りやすい。
    Unprintable characters become "?" — no crash, and therefore no one notices.
    """
    encoding = "cp932" if path.suffix == ".bat" else "utf-8-sig"
    text = path.read_bytes().decode(encoding)

    unprintable = sorted({
        character for character in text
        if not _encodable(character)
    })
    assert not unprintable, (
        f"{path.name} に CP932 で表示できない文字があります: "
        f"{''.join(unprintable)} "
        f"(— は ― に、− は - に置き換えてください)"
    )


def _encodable(character: str) -> bool:
    try:
        character.encode("cp932")
    except UnicodeEncodeError:
        return False
    return True


def test_the_encodings_are_pinned_for_git():
    """Git に再エンコードさせない。CP932 のファイルは text 扱いにしない。

    Without this, Git can re-encode or re-end these files and undo the fix.
    """
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat" in attributes and "-text" in attributes
    assert "*.ps1" in attributes and "eol=crlf" in attributes


# --- Python 側の出力 / what Python prints -----------------------------------

def test_status_marks_fall_back_outside_utf8():
    """`✓` は CP932 に無い。そのまま出すと文字化けではなく例外で落ちる。

    `✓` is absent from CP932: printed as-is it does not garble, it raises and
    takes the command down.
    """
    import io

    from aipmo.console import mark

    cp932 = io.TextIOWrapper(io.BytesIO(), encoding="cp932")
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

    assert mark("success", cp932) == "OK"
    assert mark("failed", cp932) == "NG"
    assert mark("success", utf8) == "✓"


def test_the_fallback_actually_writes_to_a_cp932_stream():
    """置き換えたものが本当に書けること。ここを外すと意味がない。"""
    import io

    from aipmo.console import mark

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp932", errors="strict")
    stream.write(f"{mark('success', stream)} 接続確認\n")
    stream.flush()

    assert "接続確認" in buffer.getvalue().decode("cp932")


def test_soften_replaces_rather_than_raising():
    import io

    from aipmo.console import soften

    cp932 = io.TextIOWrapper(io.BytesIO(), encoding="cp932")
    assert soften("✓ 完了", cp932) == "OK 完了"


def test_an_unknown_stream_encoding_is_survivable():
    """encoding を持たない出力先でも落ちない。"""
    from aipmo.console import mark, soften

    class Bare:
        pass

    assert mark("success", Bare()) == "OK"
    assert soften("✓", Bare()) == "OK"
