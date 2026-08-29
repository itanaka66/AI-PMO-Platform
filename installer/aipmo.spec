# PyInstaller spec — AI-PMO Platform
#
# 一枚岩の onefile ではなく onedir にしている。
# onefile は起動のたびに展開するため初回起動が遅く、
# 企業のウイルス対策製品に誤検知されやすい。
#
# Deliberately onedir rather than onefile: onefile unpacks on every launch,
# which makes startup slow and is a common false-positive trigger for
# corporate antivirus products.

block_cipher = None

hidden = [
    # 遅延 import しているため、静的解析では見つからない
    # These are imported lazily, so static analysis misses them.
    "psycopg",
    "qdrant_client",
    "openai",
    "aipmo.adapters.postgres",
    "aipmo.adapters.qdrant",
    "aipmo.adapters.mock",
    # pgvector / chroma / milvus / weaviate 自体（chromadb 等の重い依存）は
    # 既定のインストーラに含めない。このモジュール自体は依存無しで import
    # できるので、ここに載せても .exe が壊れることはない — 使うには
    # 別途 pip install が要る。
    #
    # pgvector / chroma / milvus / weaviate's own heavy dependencies (e.g.
    # chromadb) are not bundled in the default installer. These modules
    # import cleanly without them, so listing them here does not break the
    # .exe — actually using one still needs a separate pip install.
    "aipmo.adapters.vector_store",
    "aipmo.adapters.pgvector",
    "aipmo.adapters.chroma",
    "aipmo.adapters.milvus",
    "aipmo.adapters.weaviate",
]

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.testing", "pytest"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="aipmo",
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="aipmo",
)
