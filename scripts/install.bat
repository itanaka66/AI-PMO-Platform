@echo off
REM AI-PMO-Platform Installation Script for Windows

setlocal enabledelayedexpansion

echo.
echo ====================================================================
echo           AI-PMO-Platform Installation Script
echo                          Windows
echo ====================================================================
echo.

REM 1. Check Python
echo [1/5] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed
    echo Please install Python 3.8 or later from https://www.python.org
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [OK] Python %PYTHON_VERSION%

REM 2. Create virtual environment
echo [2/5] Setting up virtual environment...
if not exist "venv" (
    python -m venv venv
    echo [OK] Virtual environment created
) else (
    echo Virtual environment already exists
)

call venv\Scripts\activate.bat
echo [OK] Virtual environment activated

REM 3. Upgrade pip
echo [3/5] Upgrading pip...
python -m pip install --upgrade pip setuptools wheel >nul 2>&1
echo [OK] pip upgraded

REM 4. Install dependencies
echo [4/5] Installing dependencies...
if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found
    pause
    exit /b 1
)

pip install -r requirements.txt >nul 2>&1
echo [OK] Dependencies installed

REM 5. Install WebUI (Optional)
echo [5/5] WebUI Installation
echo.
echo Do you want to install WebUI (FastAPI + React)?
echo   1) Yes - Full installation with WebUI
echo   2) No  - CLI only
echo.
set /p INSTALL_WEBUI="Select (1 or 2) [default: 1]: "
if "%INSTALL_WEBUI%"=="" set INSTALL_WEBUI=1

if "%INSTALL_WEBUI%"=="1" (
    echo.
    echo Installing WebUI dependencies...

    REM Check Node.js
    node --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo WARNING: Node.js is not installed
        echo WebUI requires Node.js 16 or later
        echo Install from: https://nodejs.org/
        echo.
        set /p SKIP_WEBUI="Skip WebUI installation? (y/n) [default: y]: "
        if "%SKIP_WEBUI%"=="" set SKIP_WEBUI=y

        if "%SKIP_WEBUI%"=="y" (
            echo WARNING: WebUI skipped. CLI only mode.
            set INSTALL_WEBUI=0
        ) else (
            pause
            exit /b 1
        )
    ) else (
        for /f "tokens=*" %%i in ('node --version') do set NODE_VERSION=%%i
        echo [OK] Node.js !NODE_VERSION! detected
    )

    if "%INSTALL_WEBUI%"=="1" (
        REM Install FastAPI dependencies
        pip install fastapi uvicorn websockets pydantic >nul 2>&1
        echo [OK] FastAPI dependencies installed

        REM Install React dependencies
        if exist "aipmo\web\frontend" (
            cd aipmo\web\frontend
            npm install >nul 2>&1
            echo [OK] React dependencies installed
            cd ..\..\..\
        ) else (
            echo WARNING: Frontend directory not found
        )

        echo [OK] WebUI installation complete
    )
) else (
    echo [OK] CLI mode selected (WebUI skipped)
)

REM Final message
echo.
echo ====================================================================
echo                   Installation Complete!
echo ====================================================================
echo.

echo Next Steps:
echo.

if "%INSTALL_WEBUI%"=="1" (
    echo 1. Run CLI mode:
    echo    python -m aipmo.engine.maturation.cli
    echo.
    echo 2. Run WebUI (FastAPI backend):
    echo    uvicorn aipmo.web.api:app --reload
    echo.
    echo 3. In another terminal, run React frontend:
    echo    cd aipmo\web\frontend
    echo    npm run dev
    echo.
    echo 4. Access WebUI:
    echo    http://localhost:3000 ^(Vite dev server^)
    echo    http://localhost:8000 ^(FastAPI + React build^)
) else (
    echo 1. Run CLI:
    echo    python -m aipmo.engine.maturation.cli
    echo.
    echo To install WebUI later:
    echo    cd aipmo\web\frontend
    echo    npm install
    echo    npm run build
)

echo.
echo Documentation:
echo    Read INSTALL.md for detailed setup instructions
echo    Read docs\guide\en.md for usage guide
echo.

echo Activate environment:
echo    venv\Scripts\activate
echo.

echo Happy coding!
echo.

pause
