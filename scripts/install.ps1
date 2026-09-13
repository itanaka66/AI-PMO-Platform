# AI-PMO-Platform Installation Script for Windows PowerShell

Write-Host ""
Write-Host "====================================================================" -ForegroundColor Blue
Write-Host "          AI-PMO-Platform Installation Script                  " -ForegroundColor Blue
Write-Host "                   Windows PowerShell                          " -ForegroundColor Blue
Write-Host "====================================================================" -ForegroundColor Blue
Write-Host ""

# 1. Check Python
Write-Host "[1/5] Checking Python..." -ForegroundColor Yellow
$pythonVersion = & python --version 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python is not installed" -ForegroundColor Red
    Write-Host "Please install Python 3.8 or later from https://www.python.org"
    Write-Host "Make sure to check 'Add Python to PATH' during installation"
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[OK] Python $pythonVersion" -ForegroundColor Green

# 2. Create virtual environment
Write-Host "[2/5] Setting up virtual environment..." -ForegroundColor Yellow
if (-Not (Test-Path "venv")) {
    python -m venv venv
    Write-Host "[OK] Virtual environment created" -ForegroundColor Green
} else {
    Write-Host "Virtual environment already exists"
}

. .\venv\Scripts\Activate.ps1
Write-Host "[OK] Virtual environment activated" -ForegroundColor Green

# 3. Upgrade pip
Write-Host "[3/5] Upgrading pip..." -ForegroundColor Yellow
python -m pip install --upgrade pip setuptools wheel | Out-Null
Write-Host "[OK] pip upgraded" -ForegroundColor Green

# 4. Install dependencies
Write-Host "[4/5] Installing dependencies..." -ForegroundColor Yellow
if (-Not (Test-Path "requirements.txt")) {
    Write-Host "ERROR: requirements.txt not found" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

pip install -r requirements.txt | Out-Null
Write-Host "[OK] Dependencies installed" -ForegroundColor Green

# 5. Install WebUI (Optional)
Write-Host "[5/5] WebUI Installation" -ForegroundColor Yellow
Write-Host ""
Write-Host "Do you want to install WebUI (FastAPI + React)?"
Write-Host "  1) Yes - Full installation with WebUI"
Write-Host "  2) No  - CLI only"
Write-Host ""
$installWebUI = Read-Host "Select (1 or 2) [default: 1]"
if ([string]::IsNullOrWhiteSpace($installWebUI)) { $installWebUI = "1" }

if ($installWebUI -eq "1") {
    Write-Host ""
    Write-Host "Installing WebUI dependencies..." -ForegroundColor Blue

    # Check Node.js
    $nodeVersion = & node --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "WARNING: Node.js is not installed" -ForegroundColor Yellow
        Write-Host "WebUI requires Node.js 16 or later"
        Write-Host "Install from: https://nodejs.org/"
        Write-Host ""
        $skipWebUI = Read-Host "Skip WebUI installation? (y/n) [default: y]"
        if ([string]::IsNullOrWhiteSpace($skipWebUI)) { $skipWebUI = "y" }

        if ($skipWebUI -eq "y") {
            Write-Host "WARNING: WebUI skipped. CLI only mode." -ForegroundColor Yellow
            $installWebUI = "0"
        } else {
            Read-Host "Press Enter to exit"
            exit 1
        }
    } else {
        Write-Host "[OK] Node.js $nodeVersion detected" -ForegroundColor Green
    }

    if ($installWebUI -eq "1") {
        # Install FastAPI dependencies
        pip install fastapi uvicorn websockets pydantic | Out-Null
        Write-Host "[OK] FastAPI dependencies installed" -ForegroundColor Green

        # Install React dependencies
        if (Test-Path "aipmo\web\frontend") {
            Set-Location "aipmo\web\frontend"
            npm install | Out-Null
            Write-Host "[OK] React dependencies installed" -ForegroundColor Green
            Set-Location "..\..\.."
        } else {
            Write-Host "WARNING: Frontend directory not found" -ForegroundColor Yellow
        }

        Write-Host "[OK] WebUI installation complete" -ForegroundColor Green
    }
} else {
    Write-Host "[OK] CLI mode selected (WebUI skipped)" -ForegroundColor Green
}

# Final message
Write-Host ""
Write-Host "====================================================================" -ForegroundColor Green
Write-Host "                  Installation Complete!                        " -ForegroundColor Green
Write-Host "====================================================================" -ForegroundColor Green
Write-Host ""

Write-Host "Next Steps:" -ForegroundColor Blue
Write-Host ""

if ($installWebUI -eq "1") {
    Write-Host "1. Run CLI mode:"
    Write-Host "    python -m aipmo.engine.maturation.cli"
    Write-Host ""
    Write-Host "2. Run WebUI (FastAPI backend):"
    Write-Host "    uvicorn aipmo.web.api:app --reload"
    Write-Host ""
    Write-Host "3. In another terminal, run React frontend:"
    Write-Host "    cd aipmo\web\frontend"
    Write-Host "    npm run dev"
    Write-Host ""
    Write-Host "4. Access WebUI:"
    Write-Host "    http://localhost:3000 (Vite dev server)"
    Write-Host "    http://localhost:8000 (FastAPI + React build)"
} else {
    Write-Host "1. Run CLI:"
    Write-Host "    python -m aipmo.engine.maturation.cli"
    Write-Host ""
    Write-Host "To install WebUI later:"
    Write-Host "    cd aipmo\web\frontend"
    Write-Host "    npm install"
    Write-Host "    npm run build"
}

Write-Host ""
Write-Host "Documentation:" -ForegroundColor Blue
Write-Host "    Read INSTALL.md for detailed setup instructions"
Write-Host "    Read docs\guide\en.md for usage guide"
Write-Host ""

Write-Host "Activate environment:" -ForegroundColor Blue
Write-Host "    .\venv\Scripts\Activate.ps1"
Write-Host ""

Write-Host "Happy coding!" -ForegroundColor Yellow
Write-Host ""

Read-Host "Press Enter to exit"
