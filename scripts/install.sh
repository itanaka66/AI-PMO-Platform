#!/bin/bash

# AI-PMO-Platform Installation Script
# Supports Linux / macOS
# WebUI is optional

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Banner
echo -e "${BLUE}"
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║          AI-PMO-Platform Installation Script                  ║"
echo "║                      Linux / macOS                            ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Detect OS
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="Linux"
elif [[ "$OSTYPE" == "darwin"* ]]; then
    OS="macOS"
else
    echo -e "${RED}Unsupported OS: $OSTYPE${NC}"
    exit 1
fi

echo -e "${BLUE}Detected OS: $OS${NC}\n"

# 1. Check Python
echo -e "${YELLOW}[1/5] Checking Python...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python3 is not installed${NC}"
    echo "Please install Python 3.8 or later"
    if [[ "$OS" == "Linux" ]]; then
        echo "  Ubuntu/Debian: sudo apt-get install python3 python3-pip python3-venv"
        echo "  Fedora:        sudo dnf install python3 python3-pip python3-venv"
        echo "  CentOS:        sudo yum install python3 python3-pip python3-devel"
    elif [[ "$OS" == "macOS" ]]; then
        echo "  Homebrew:      brew install python3"
        echo "  MacPorts:      sudo port install python310"
    fi
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo -e "${GREEN}✓ Python $PYTHON_VERSION${NC}"

# 2. Create virtual environment
echo -e "${YELLOW}[2/5] Setting up virtual environment...${NC}"

if [ -d "venv" ]; then
    echo "  Virtual environment already exists"
else
    python3 -m venv venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
fi

source venv/bin/activate
echo -e "${GREEN}✓ Virtual environment activated${NC}"

# 3. Upgrade pip
echo -e "${YELLOW}[3/5] Upgrading pip...${NC}"
pip install --upgrade pip setuptools wheel > /dev/null 2>&1
echo -e "${GREEN}✓ pip upgraded${NC}"

# 4. Install dependencies
echo -e "${YELLOW}[4/5] Installing dependencies...${NC}"

# Check if requirements.txt exists
if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}❌ requirements.txt not found${NC}"
    exit 1
fi

pip install -r requirements.txt > /dev/null 2>&1
echo -e "${GREEN}✓ Dependencies installed${NC}"

# 5. Install WebUI (Optional)
echo -e "${YELLOW}[5/5] WebUI Installation${NC}"
echo "Do you want to install WebUI (FastAPI + React)?"
echo "  1) Yes - Full installation with WebUI"
echo "  2) No  - CLI only"
echo ""
read -p "Select (1 or 2) [default: 1]: " INSTALL_WEBUI
INSTALL_WEBUI=${INSTALL_WEBUI:-1}

if [ "$INSTALL_WEBUI" = "1" ] || [ "$INSTALL_WEBUI" = "yes" ] || [ "$INSTALL_WEBUI" = "y" ]; then
    echo -e "${BLUE}Installing WebUI dependencies...${NC}"
    
    # Check Node.js
    if ! command -v npm &> /dev/null; then
        echo -e "${YELLOW}⚠ Node.js is not installed${NC}"
        echo "WebUI requires Node.js 16 or later"
        if [[ "$OS" == "Linux" ]]; then
            echo "  Install with: curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -"
            echo "                sudo apt-get install -y nodejs"
        elif [[ "$OS" == "macOS" ]]; then
            echo "  Install with: brew install node"
        fi
        echo ""
        read -p "Skip WebUI installation? (y/n) [default: y]: " SKIP_WEBUI
        SKIP_WEBUI=${SKIP_WEBUI:-y}
        
        if [ "$SKIP_WEBUI" = "y" ] || [ "$SKIP_WEBUI" = "yes" ]; then
            echo -e "${YELLOW}⚠ WebUI skipped. CLI only mode.${NC}"
            INSTALL_WEBUI=0
        else
            exit 1
        fi
    else
        NODE_VERSION=$(node --version)
        echo -e "${GREEN}✓ Node.js $NODE_VERSION detected${NC}"
    fi
    
    if [ "$INSTALL_WEBUI" = "1" ]; then
        # Install FastAPI dependencies
        pip install fastapi uvicorn websockets pydantic > /dev/null 2>&1
        echo -e "${GREEN}✓ FastAPI dependencies installed${NC}"
        
        # Install React dependencies
        if [ -d "aipmo/web/frontend" ]; then
            cd aipmo/web/frontend
            npm install > /dev/null 2>&1
            echo -e "${GREEN}✓ React dependencies installed${NC}"
            cd ../../..
        else
            echo -e "${YELLOW}⚠ Frontend directory not found${NC}"
        fi
        
        echo -e "${GREEN}✓ WebUI installation complete${NC}"
    fi
else
    echo -e "${GREEN}✓ CLI mode selected (WebUI skipped)${NC}"
fi

# Final message
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                  Installation Complete!                        ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${BLUE}📋 Next Steps:${NC}"
echo ""

if [ "$INSTALL_WEBUI" = "1" ]; then
    echo "1️⃣  Run CLI mode:"
    echo "    python -m aipmo.engine.maturation.cli"
    echo ""
    echo "2️⃣  Run WebUI (FastAPI backend):"
    echo "    uvicorn aipmo.web.api:app --reload"
    echo ""
    echo "3️⃣  In another terminal, run React frontend:"
    echo "    cd aipmo/web/frontend"
    echo "    npm start"
    echo ""
    echo "    Or with Vite:"
    echo "    npm run dev"
    echo ""
    echo "4️⃣  Access WebUI:"
    echo "    http://localhost:3000 (Vite dev server)"
    echo "    http://localhost:8000 (FastAPI + React build)"
else
    echo "1️⃣  Run CLI:"
    echo "    python -m aipmo.engine.maturation.cli"
    echo ""
    echo "💡 To install WebUI later:"
    echo "    npm install -g npm"
    echo "    cd aipmo/web/frontend && npm install && npm run build"
fi

echo ""
echo -e "${BLUE}📖 Documentation:${NC}"
echo "    Read INSTALL.md for detailed setup instructions"
echo "    Read docs/guide/en.md for usage guide"
echo ""

echo -e "${BLUE}🚀 Activate environment:${NC}"
echo "    source venv/bin/activate"
echo ""

echo -e "${YELLOW}Happy coding! 🎉${NC}"
