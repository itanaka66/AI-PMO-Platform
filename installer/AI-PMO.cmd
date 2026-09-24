@echo off
rem Launcher for the AI-PMO desktop/Start Menu shortcuts.
rem
rem A plain shortcut straight to aipmo.exe with no arguments just flashes
rem an error and closes ("the following arguments are required: command")
rem -- argparse requires a subcommand, and a console app's window closes
rem the instant the process exits. This wrapper keeps the window open
rem (cmd /k) and lists the commands, so double-clicking it is useful.

cd /d "%~dp0"
set PATH=%~dp0;%PATH%

echo.
echo   AI-PMO Platform
echo   ---------------------------------------------
echo   aipmo setup      First-time setup
echo   aipmo validate   Validate a template
echo   aipmo run        Run a template
echo   aipmo doctor     Check adapter connections
echo   aipmo --help     All commands
echo.

cmd /k
