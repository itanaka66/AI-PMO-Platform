@echo off
rem AI-PMO Platform - Windows installer entry point
rem
rem ダブルクリックで実行してください / Double-click to run.
rem
rem PowerShell スクリプトは既定の実行ポリシーでブロックされるため、
rem この .bat から -ExecutionPolicy Bypass を付けて起動する。
rem PowerShell scripts are blocked by the default execution policy, so this
rem wrapper launches install.ps1 with -ExecutionPolicy Bypass.

setlocal

echo.
echo   AI-PMO Platform
echo   Installer starting...
echo.

where powershell >nul 2>&1
if errorlevel 1 (
    echo エラー: PowerShell が見つかりません。
    echo Error: PowerShell was not found.
    echo Windows 10 以降が必要です / Windows 10 or later is required.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo インストールに失敗しました / Installation failed. Code: %EXITCODE%
    echo.
    pause
)

endlocal
exit /b %EXITCODE%
