#!/usr/bin/env bash
# AI-PMO Platform — macOS / Linux インストーラ / installer
#
#   curl -fsSL <url>/install.sh | bash
#   ./scripts/install.sh
#
# システムの Python を汚さないため、必ず venv を作る。
# Always builds a venv so the system Python is never modified.
#
# WebUI（スマホ向け画面）は既定では入れない。CLI だけで完結するため。
# 入れる場合は --web を渡すか、対話端末なら質問に答える。
#
# The WebUI (the mobile-friendly screen) is not installed by default — the
# CLI is complete on its own. Pass --web to include it, or answer the
# interactive prompt when one is available.
#
# 使い方 / Usage:
#   ./scripts/install.sh [--web|--no-web] [-h|--help]

set -euo pipefail

INSTALL_DIR="${AIPMO_HOME:-$HOME/.local/share/ai-pmo}"
MIN_MAJOR=3
MIN_MINOR=10
INSTALL_WEB=""

step()  { printf '\n==> %s\n' "$1"; }
ok()    { printf '    OK  %s\n' "$1"; }
warn()  { printf '    !   %s\n' "$1"; }
fail()  { printf '\nエラー / Error: %s\n\n' "$1" >&2; exit 1; }

usage() {
  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --web) INSTALL_WEB=1; shift ;;
    --no-web) INSTALL_WEB=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "不明な引数です / unknown argument: $1 (--help を参照 / see --help)" ;;
  esac
done

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR,$MIN_MINOR) else 1)" 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

printf '\n  AI-PMO Platform\n  インストーラ / Installer\n'
printf '  ---------------------------------------------\n'

step "Python を確認しています / Checking for Python"
if ! PYTHON="$(find_python)"; then
  warn "Python ${MIN_MAJOR}.${MIN_MINOR} 以降が見つかりません / not found"
  case "$(uname -s)" in
    Darwin)
      fail "Homebrew で導入してください / install via Homebrew:
  brew install python@3.12" ;;
    Linux)
      fail "パッケージマネージャで導入してください / install via your package manager:
  Debian/Ubuntu:  sudo apt install python3 python3-venv
  Fedora/RHEL:    sudo dnf install python3" ;;
    *)
      fail "https://www.python.org/downloads/ から導入してください / install from python.org" ;;
  esac
fi
ok "$($PYTHON --version)"

if [ -z "$INSTALL_WEB" ]; then
  if [ -t 0 ]; then
    step "WebUI（スマホ向け画面）/ WebUI (mobile-friendly screen)"
    printf '    CLI だけで完結します。スマホから使う・進捗を見せたい場合だけ要ります。\n'
    printf '    The CLI is complete on its own; this only matters for phone access or\n'
    printf '    showing progress to someone else.\n'
    read -r -p "    WebUI も入れますか？ / Install it too? (y/N): " choice || choice=""
    case "$choice" in
      y|Y|yes|YES) INSTALL_WEB=1 ;;
      *) INSTALL_WEB=0 ;;
    esac
  else
    INSTALL_WEB=0
  fi
fi

step "インストール先 / Install location"
printf '    %s\n' "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for item in aipmo prompts templates sql queries.yaml pyproject.toml README.md; do
  [ -e "$SOURCE_ROOT/$item" ] && cp -R "$SOURCE_ROOT/$item" "$INSTALL_DIR/"
done
ok "ファイルをコピーしました / files copied"

step "仮想環境を作成しています / Creating the virtual environment"
VENV="$INSTALL_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV" \
    || fail "仮想環境を作成できませんでした。python3-venv が必要かもしれません
/ could not create the venv; you may need the python3-venv package"
fi
ok ".venv"

EXTRAS="cloud,data"
[ "$INSTALL_WEB" = "1" ] && EXTRAS="$EXTRAS,web"

step "依存パッケージを導入しています / Installing dependencies"
printf '    数分かかります / This takes a few minutes.\n'
"$VENV/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check
(cd "$INSTALL_DIR" && "$VENV/bin/python" -m pip install --quiet \
  --disable-pip-version-check ".[$EXTRAS]") \
  || fail "依存パッケージの導入に失敗しました / dependency installation failed"
ok "完了 / done"

step "コマンドを登録しています / Registering the command"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/aipmo" <<LAUNCHER
#!/usr/bin/env bash
cd "$INSTALL_DIR"
exec "$VENV/bin/aipmo" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/aipmo"
ok "$BIN_DIR/aipmo"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "PATH に $BIN_DIR がありません / not on your PATH. シェル設定に追加してください:
        export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

printf '\n  インストールが完了しました / Installation complete\n\n'

if [ -t 0 ]; then
  "$VENV/bin/python" -m aipmo.cli setup --dir "$INSTALL_DIR"
else
  printf '  次に実行してください / Next, run:\n    aipmo setup\n\n'
fi

if [ "$INSTALL_WEB" = "1" ]; then
  printf '  WebUI を入れました。起動するには / WebUI installed. Start it with:\n'
  printf '    aipmo serve --host 0.0.0.0\n\n'
else
  printf '  WebUI は入れていません。後から入れる場合は次を実行してください:\n'
  printf '  WebUI was not installed. To add it later, run:\n'
  printf '    %s --web\n\n' "$0"
fi
