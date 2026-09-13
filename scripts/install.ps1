# AI-PMO Platform ― Windows インストーラ / Windows installer
#
# Python が無ければ導入し、専用の仮想環境を作り、セットアップウィザードを開く。
# Installs Python if absent, creates an isolated virtual environment, and opens
# the setup wizard.
#
# 実行 / Run:
#   install.bat をダブルクリック / double-click install.bat
#
# システムの Python を汚さないため、必ず venv を作る。
# Always builds a venv so the system Python is never modified.

[CmdletBinding()]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\AI-PMO",
    [switch]$NoShortcut,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$MinPythonMajor = 3
$MinPythonMinor = 10
$PythonInstallerUrl =
    "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"

function Write-Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Write-Ok($message) {
    Write-Host "    OK  $message" -ForegroundColor Green
}

function Write-Warn($message) {
    Write-Host "    !   $message" -ForegroundColor Yellow
}

function Fail($message) {
    Write-Host ""
    Write-Host "エラー / Error: $message" -ForegroundColor Red
    Write-Host ""
    if (-not $Quiet) {
        Write-Host "Enter キーで終了 / Press Enter to exit"
        [void](Read-Host)
    }
    exit 1
}

# --- Python を探す / locate a usable Python ------------------------------

function Find-Python {
    $candidates = @()

    # py ランチャは複数バージョンを正しく解決するので最優先
    # The py launcher resolves versions correctly, so try it first.
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates += ,@($launcher.Source, @("-3"))
    }

    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) {
        $candidates += ,@($onPath.Source, @())
    }

    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $prefix = $candidate[1]
        try {
            $args = $prefix + @("-c", "import sys; print(sys.version_info.major, sys.version_info.minor)")
            $output = & $exe @args 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $parts = $output.Trim().Split(" ")
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -gt $MinPythonMajor -or
                ($major -eq $MinPythonMajor -and $minor -ge $MinPythonMinor)) {
                return @{ Exe = $exe; Prefix = $prefix; Version = "$major.$minor" }
            }
            # Microsoft Store のスタブは実行しても何も返さないので上で弾かれる
            # Microsoft Store stubs produce no output and are filtered above.
        } catch {
            continue
        }
    }
    return $null
}

function Install-Python {
    Write-Step "Python を導入します / Installing Python"
    Write-Host "    数分かかります / This takes a few minutes."

    $installer = Join-Path $env:TEMP "python-aipmo-setup.exe"
    try {
        Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $installer -UseBasicParsing
    } catch {
        Fail @"
Python のダウンロードに失敗しました / Could not download Python.
ネットワーク接続を確認するか、手動で導入してください。
Check your network, or install manually from:
  https://www.python.org/downloads/
"@
    }

    # 管理者権限を避けるためユーザー単位で導入する
    # Per-user install so no administrator rights are needed.
    $arguments = @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1",
        "Include_pip=1", "Include_launcher=1", "Include_test=0"
    )
    $process = Start-Process -FilePath $installer -ArgumentList $arguments `
        -Wait -PassThru
    Remove-Item $installer -ErrorAction SilentlyContinue

    if ($process.ExitCode -ne 0) {
        Fail "Python のインストーラが失敗しました (コード $($process.ExitCode)) / Python installer failed"
    }

    # PATH は現在のセッションに反映されないため、自分で読み直す
    # PATH changes do not reach the current session; reload it.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")

    $python = Find-Python
    if (-not $python) {
        Fail @"
Python は入りましたが、このウィンドウからは見つかりません。
Python was installed but is not visible in this session.
PC を再起動してから install.bat をもう一度実行してください。
Restart your PC, then run install.bat again.
"@
    }
    Write-Ok "Python $($python.Version)"
    return $python
}

# --- 本体 / main ---------------------------------------------------------

Write-Host ""
Write-Host "  AI-PMO Platform" -ForegroundColor White
Write-Host "  インストーラ / Installer"
Write-Host "  ---------------------------------------------"

Write-Step "Python を確認しています / Checking for Python"
$python = Find-Python
if ($python) {
    Write-Ok "Python $($python.Version) が見つかりました / found"
} else {
    Write-Warn "対応する Python が見つかりません / no suitable Python found"
    $python = Install-Python
}

Write-Step "インストール先 / Install location"
Write-Host "    $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# ソースをコピーする。スクリプトの親ディレクトリがパッケージのルート。
# Copy the sources. The script's parent directory is the package root.
$sourceRoot = Split-Path -Parent $PSScriptRoot
$payload = @("aipmo", "prompts", "templates", "sql",
             "queries.yaml", "pyproject.toml", "README.md")

foreach ($item in $payload) {
    $source = Join-Path $sourceRoot $item
    if (Test-Path $source) {
        Copy-Item $source -Destination $InstallDir -Recurse -Force
    }
}
Write-Ok "ファイルをコピーしました / files copied"

Write-Step "仮想環境を作成しています / Creating the virtual environment"
$venv = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    $createArgs = $python.Prefix + @("-m", "venv", $venv)
    & $python.Exe @createArgs
    if ($LASTEXITCODE -ne 0) {
        Fail "仮想環境を作成できませんでした / could not create the virtual environment"
    }
}
Write-Ok ".venv"

Write-Step "依存パッケージを導入しています / Installing dependencies"
Write-Host "    数分かかります / This takes a few minutes."

& $venvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "pip の更新に失敗しました / pip upgrade failed" }

Push-Location $InstallDir
try {
    & $venvPython -m pip install --quiet --disable-pip-version-check ".[cloud,data]"
    if ($LASTEXITCODE -ne 0) {
        Fail @"
依存パッケージの導入に失敗しました / dependency installation failed.
プロキシ環境の場合は、管理者に PyPI (pypi.org) への接続許可を確認してください。
Behind a proxy? Ask your administrator to allow access to pypi.org.
"@
    }
} finally {
    Pop-Location
}
Write-Ok "完了 / done"

# --- ショートカット / shortcuts -----------------------------------------

if (-not $NoShortcut) {
    Write-Step "ショートカットを作成しています / Creating shortcuts"

    $launcher = Join-Path $InstallDir "AI-PMO.cmd"
    @"
@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
echo.
echo   AI-PMO Platform
echo   ---------------------------------------------
echo   aipmo setup      初回設定 / first-run setup
echo   aipmo validate   テンプレート検証 / validate a template
echo   aipmo run        テンプレート実行 / run a template
echo   aipmo doctor     接続確認 / connection check
echo   aipmo --help     すべてのコマンド / all commands
echo.
cmd /k
"@ | Set-Content -Path $launcher -Encoding ASCII

    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath("Desktop"),
                       (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
        if (-not (Test-Path $dir)) { continue }
        $link = $shell.CreateShortcut((Join-Path $dir "AI-PMO.lnk"))
        $link.TargetPath = $launcher
        $link.WorkingDirectory = $InstallDir
        $link.Description = "AI-PMO Platform"
        $link.Save()
    }
    Write-Ok "デスクトップとスタートメニュー / desktop and Start menu"
}

# --- セットアップ / setup ------------------------------------------------

Write-Host ""
Write-Host "  インストールが完了しました / Installation complete" -ForegroundColor Green
Write-Host ""

if (-not $Quiet) {
    Write-Host "  続けて初期設定を行います / Continuing to first-run setup."
    Write-Host ""
    Push-Location $InstallDir
    try {
        & $venvPython -m aipmo.cli setup --dir $InstallDir
    } finally {
        Pop-Location
    }

    Write-Host ""
    Write-Host "  デスクトップの AI-PMO から起動できます" -ForegroundColor Cyan
    Write-Host "  Launch it from the AI-PMO shortcut on your desktop." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Enter キーで終了 / Press Enter to exit"
    [void](Read-Host)
}
