# .exe インストーラのビルド / Build the .exe installer
#
#   installer\build.ps1
#
# 出力 / Output: dist\AI-PMO-Setup-0.1.4.exe
#
# 必要なもの / Requires:
#   - Windows (PyInstaller はクロスコンパイルできない / it cannot cross-compile)
#   - Python 3.10+
#   - Inno Setup 6 (iscc.exe が PATH にあること / iscc.exe on PATH)
#   - signtool.exe（署名する場合のみ。Windows SDK に含まれる）
#     signtool.exe, only if signing ― it ships with the Windows SDK
#
# コード署名 / Code signing:
#   証明書があれば、次のいずれかを設定するだけで自動的に署名される
#   （本体の aipmo.exe と、インストーラ本体の両方）。無ければ、これまでどおり
#   未署名でビルドされる ― 挙動は変わらない。証明書の入手そのものはこの
#   スクリプトの範囲外（購入と組織確認が要る）。詳しくは INSTALL.md。
#
#   With a certificate, setting either of the following signs both the
#   aipmo.exe binary and the installer itself automatically. Without one,
#   the build stays unsigned exactly as before. Obtaining a certificate is
#   outside what this script can do (it requires a purchase and org
#   verification). See INSTALL.md for details.
#
#   PFX ファイル / a PFX file:
#     $env:AIPMO_SIGN_CERT_PATH      証明書ファイルへのパス / path to the .pfx
#     $env:AIPMO_SIGN_CERT_PASSWORD  そのパスワード / its password
#
#   証明書ストア / the certificate store (CI 向け、平文の鍵をコマンド列に
#   出さない / preferred for CI ― keeps the key off the command line):
#     $env:AIPMO_SIGN_CERT_THUMBPRINT  インポート済み証明書の拇印 / thumbprint
#       of a certificate already imported into the current-user store

[CmdletBinding()]
param([switch]$SkipInno)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

# DigiCert の公開タイムスタンプサーバー。特定の証明書発行元に縛られない。
# DigiCert's public timestamp server; not tied to any particular issuer.
$timestampUrl = "http://timestamp.digicert.com"

function Get-SigningArgs {
    # signtool sign に渡す引数と、Inno Setup の /S フラグに渡す
    # コマンド文字列の両方をここで組み立てる。証明書が無ければ $null を返す。
    #
    # Builds both the signtool argument list and the command string Inno Setup's
    # /S flag needs, in one place. Returns $null when no certificate is configured.
    if ($env:AIPMO_SIGN_CERT_THUMBPRINT) {
        $certArgs = @("/sha1", $env:AIPMO_SIGN_CERT_THUMBPRINT)
        $certCmd = "signtool.exe sign /sha1 $($env:AIPMO_SIGN_CERT_THUMBPRINT) /fd SHA256 /tr $timestampUrl /td SHA256 `$f"
    } elseif ($env:AIPMO_SIGN_CERT_PATH -and $env:AIPMO_SIGN_CERT_PASSWORD) {
        $certArgs = @("/f", $env:AIPMO_SIGN_CERT_PATH, "/p", $env:AIPMO_SIGN_CERT_PASSWORD)
        # パスワードは Inno Setup の /S コマンドにもそのまま渡す必要がある。
        # ビルドログに出ないよう、iscc の呼び出し自体を Write-Host しない。
        # The password has to reach Inno Setup's /S command the same way; the
        # iscc invocation itself is never Write-Host'd, so it stays out of logs.
        $certCmd = "signtool.exe sign /f $($env:AIPMO_SIGN_CERT_PATH) /p $($env:AIPMO_SIGN_CERT_PASSWORD) /fd SHA256 /tr $timestampUrl /td SHA256 `$f"
    } else {
        return $null
    }
    return @{ SignToolArgs = $certArgs; InnoCommand = $certCmd }
}

$signing = Get-SigningArgs

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
    # entry.py は実行ファイル自身の場所へ作業ディレクトリを変える
    # （ショートカット起動対策）。この時点では templates\ はまだ
    # dist\aipmo\ にコピーされていない（Inno Setup がそれを行うのは
    # この後）ので、相対パスでは cwd の変更後に解決できない。絶対パスを渡す。
    #
    # entry.py changes its working directory to the executable's own
    # location (to support shortcut launches). At this point templates\
    # has not yet been copied into dist\aipmo\ ― Inno Setup does that
    # later ― so a relative path cannot resolve once the cwd has moved.
    # An absolute path sidesteps this entirely.
    & "dist\aipmo\aipmo.exe" validate "$root\templates\examples\meeting_minutes.yaml"
    if ($LASTEXITCODE -ne 0) { throw "Smoke test failed" }

    if ($signing) {
        Write-Host "==> 実行ファイルに署名しています / Signing the executable" -ForegroundColor Cyan
        $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if (-not $signtool) {
            throw "signtool.exe が見つかりません。Windows SDK を導入してください / install the Windows SDK"
        }
        & $signtool.Source sign @($signing.SignToolArgs) /fd SHA256 /tr $timestampUrl /td SHA256 `
            "dist\aipmo\aipmo.exe"
        if ($LASTEXITCODE -ne 0) { throw "signtool failed on aipmo.exe" }
    }

    if (-not $SkipInno) {
        Write-Host "==> インストーラを作成しています / Building the installer" -ForegroundColor Cyan
        $iscc = Get-Command iscc -ErrorAction SilentlyContinue
        if (-not $iscc) {
            throw "iscc.exe が見つかりません。Inno Setup 6 を導入してください / install Inno Setup 6"
        }
        $isccArgs = @("installer\aipmo.iss")
        if ($signing) {
            # /Sname=command で Inno Setup に署名コマンドを渡す。.iss 側は
            # SignTool=name とだけ書けばよく、鍵の在り処を script に持たせない。
            # /DSIGN が無いと .iss 側の SignTool 行自体が存在しないので、
            # 証明書無しのビルドでは iscc がこのフラグ自体を目にしない。
            #
            # /Sname=command hands Inno Setup the signing command; the .iss file
            # only needs SignTool=name, so the key's location never lives in the
            # script. Without /DSIGN, the .iss file's SignTool line does not even
            # exist, so a build with no certificate never sees this flag at all.
            $isccArgs = @("/DSIGN", "/Saipmosign=$($signing.InnoCommand)") + $isccArgs
        }
        & $iscc.Source @isccArgs
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    }

    Write-Host ""
    Write-Host "完了 / Done: dist\" -ForegroundColor Green
    Get-ChildItem dist\*.exe | ForEach-Object { Write-Host "  $($_.Name)" }
    Write-Host ""
    if ($signing) {
        Write-Host "署名済みビルドです / This build is signed." -ForegroundColor Green
    } else {
        Write-Host "署名について / On code signing:" -ForegroundColor Yellow
        Write-Host "  未署名の .exe は SmartScreen に警告されます。"
        Write-Host "  An unsigned .exe triggers a SmartScreen warning."
        Write-Host "  AIPMO_SIGN_CERT_PATH / AIPMO_SIGN_CERT_PASSWORD か"
        Write-Host "  AIPMO_SIGN_CERT_THUMBPRINT を設定すると自動で署名されます。"
        Write-Host "  Set AIPMO_SIGN_CERT_PATH / AIPMO_SIGN_CERT_PASSWORD, or"
        Write-Host "  AIPMO_SIGN_CERT_THUMBPRINT, to sign automatically. See INSTALL.md."
    }
} finally {
    Pop-Location
}
