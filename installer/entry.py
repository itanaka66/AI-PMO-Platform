"""PyInstaller のエントリポイント / PyInstaller entry point.

作業ディレクトリを実行ファイルの場所に合わせる。
ショートカット経由の起動では作業ディレクトリが不定になり、
prompts/ や queries.yaml の相対パスが解決できなくなるため。

Anchors the working directory to the executable's location. When launched from
a shortcut the working directory is unpredictable, which breaks the relative
paths to prompts/ and queries.yaml.
"""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).resolve().parent)

from aipmo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
