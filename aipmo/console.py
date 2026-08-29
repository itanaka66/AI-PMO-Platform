"""端末に出せる文字を選ぶ / choosing characters the console can print.

日本語版 Windows のコンソールは既定で CP932 (Shift-JIS) です。
`✓` や `✗` は CP932 に存在しないため、そのまま出力すると
**文字化けではなく UnicodeEncodeError で落ちます。**

飾りの記号ひとつでコマンドが異常終了するのは、割に合いません。
出せるかを実際に試し、出せなければ ASCII に落とします。

A Japanese Windows console runs on CP932, where `✓` and `✗` do not exist. They
do not come out garbled — they raise UnicodeEncodeError and take the command
down with them. Losing a command to a decorative glyph is a bad trade, so what
the console can actually encode is tested, and anything else falls back to
ASCII.
"""
from __future__ import annotations

import sys
from functools import lru_cache

# 記号と、出せなかったときの代わり / glyphs and their ASCII stand-ins
MARKS = {
    "success": ("✓", "OK"),
    "failed": ("✗", "NG"),
    "skipped": ("-", "-"),
    "unknown": ("?", "?"),
    "bullet": ("・", "*"),
    "arrow": ("→", "->"),
}


@lru_cache(maxsize=8)
def _can_encode(text: str, encoding: str | None) -> bool:
    if not encoding:
        return False
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def stream_encoding(stream=None) -> str | None:
    stream = stream if stream is not None else sys.stdout
    return getattr(stream, "encoding", None)


def mark(kind: str, stream=None) -> str:
    """状態を表す記号 / the glyph for a status.

    出力先が受け付けるかで選ぶ。UTF-8 の端末なら記号、CP932 なら文字。
    Chosen by what the destination accepts.
    """
    preferred, fallback = MARKS.get(kind, MARKS["unknown"])
    return preferred if _can_encode(preferred, stream_encoding(stream)) else fallback


def soften(text: str, stream=None) -> str:
    """出せない文字を置き換える / swap out anything the console cannot take.

    落とすくらいなら、見た目が少し崩れるほうがよい。
    A slightly plainer line beats a traceback.
    """
    encoding = stream_encoding(stream)
    if _can_encode(text, encoding):
        return text

    for preferred, fallback in MARKS.values():
        if preferred != fallback:
            text = text.replace(preferred, fallback)

    if _can_encode(text, encoding):
        return text

    # まだ残っているものは、その文字だけ落とす。
    # 行ごと失うより、読める部分を残すほうがよい。
    # Anything still unencodable is dropped character by character: keeping the
    # readable part beats losing the line.
    return text.encode(encoding or "ascii", errors="replace").decode(
        encoding or "ascii", errors="replace")


def configure_stdio() -> None:
    """可能なら UTF-8 で出力する / print as UTF-8 where the platform allows.

    Windows でも、コードページを UTF-8 にした端末なら記号が出せます。
    ここで出力側を UTF-8 に揃えておくと、`chcp 65001` した端末で
    そのまま読めるようになります。切り替えられない環境では何もしません。

    On a console switched to UTF-8, the glyphs are printable. Aligning the
    stream here makes that work; where it cannot be changed, nothing happens.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace" が要点。ここを既定のままにすると、
            # 出せない1文字でコマンド全体が落ちる。
            # The errors setting is the point: left at its default, one
            # unprintable character takes the whole command down.
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
