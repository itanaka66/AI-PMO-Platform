# .exe インストーラのビルド / Build the .exe installer
#
#   installer\build.ps1
#
# 出力 / Output: dist\AI-PMO-Setup-0.1.0.exe
#
# 必要なもの / Requires:
#   - Windows (PyInstaller はクロスコンパイルできない / it cannot cross-compile)
#   - Python 3.10+
#   - Inno Setup 6 (iscc.exe が PATH にあること / iscc.exe on PATH)

[CmdletBinding()]
param([switch]$SkipInno)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    Write-Host "==> ビルド環境を準備しています / Preparing the build environment" -ForegroundColor Cyan
    python -m venv .build-venv
    $py = ".build-venv\Scripts\python.exe"
    & $py -m pip install --upgrade pip --quiet
    & $py -m pip install --quiet ".[cloud,data]" pyinstaller

    Write-Host "==> 実行ファイルを作成しています / Building the executable" -ForegroundColor Cyan
    & $py -m PyInstaller --noconfirm --clean installer\aipmo.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    Write-Host "==> 動作確認 / Smoke test" -ForegroundColor Cyan
    & "dist\aipmo\aipmo.exe" validate templates\examples\meeting_minutes.yaml
    if ($LASTEXITCODE -ne 0) { throw "Smoke test failed" }

    if (-not $SkipInno) {
        Write-Host "==> インストーラを作成しています / Building the installer" -ForegroundColor Cyan
        $iscc = Get-Command iscc -ErrorAction SilentlyContinue
        if (-not $iscc) {
            throw "iscc.exe が見つかりません。Inno Setup 6 を導入してください / install Inno Setup 6"
        }
        & $iscc.Source installer\aipmo.iss
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    }

    Write-Host ""
    Write-Host "完了 / Done: dist\" -ForegroundColor Green
    Get-ChildItem dist\*.exe | ForEach-Object { Write-Host "  $($_.Name)" }
    Write-Host ""
    Write-Host "署名について / On code signing:" -ForegroundColor Yellow
    Write-Host "  未署名の .exe は SmartScreen に警告されます。"
    Write-Host "  An unsigned .exe triggers a SmartScreen warning."
    Write-Host "  配布するならコード署名証明書での署名を強く推奨します。"
    Write-Host "  Sign it with a code-signing certificate before distributing:"
    Write-Host "    signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256 dist\*.exe"
} finally {
    Pop-Location
}
