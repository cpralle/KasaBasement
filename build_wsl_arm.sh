#!/bin/bash
# Build script for Linux ARM using WSL with QEMU ARM emulation
# Run this in WSL (Windows Subsystem for Linux)

set -e  # Exit on error

echo "Building KasaBasementBridge for Linux ARM using WSL..."

# Check if we're in WSL
if [ -z "$WSL_DISTRO_NAME" ] && [ -z "$WSL_INTEROP" ]; then
    echo "Warning: This doesn't appear to be running in WSL."
    echo "This script is designed for WSL with ARM emulation."
fi

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed."
    echo "Install it with: sudo apt update && sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Found Python $PYTHON_VERSION"

# Check if QEMU ARM is available (for cross-compilation if needed)
# Note: PyInstaller will build for the native architecture, so we need ARM Python
if ! command -v qemu-arm-static &> /dev/null && ! command -v qemu-aarch64-static &> /dev/null; then
    echo "Note: QEMU ARM emulation not detected. Installing..."
    echo "We'll use a different approach - building with ARM Python via Docker or native ARM."
    echo ""
    echo "For WSL, you have two options:"
    echo "1. Use Docker with ARM emulation (recommended)"
    echo "2. Install ARM toolchain and use it"
    echo ""
    read -p "Continue with Docker approach? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Check if Docker is available
if command -v docker &> /dev/null; then
    echo "Docker detected. We can use Docker for ARM build."
    USE_DOCKER=true
else
    echo "Docker not found. Will attempt native build (may not work for ARM)."
    USE_DOCKER=false
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Install PyInstaller
echo "Installing PyInstaller..."
pip install pyinstaller

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build dist __pycache__ *.spec

# Build the executable
echo "Building executable with PyInstaller..."
echo "(Note: This will build for the current architecture. For ARM, use Docker method below.)"

if [ "$USE_DOCKER" = true ]; then
    echo ""
    echo "Using Docker for ARM build..."
    echo "Creating Dockerfile for ARM build..."
    
    # Create a temporary Dockerfile
    cat > Dockerfile.arm << 'EOF'
FROM --platform=linux/arm/v7 python:3.13-slim

WORKDIR /build

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libffi-dev \
    libssl-dev \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir pyinstaller

# Copy project files
COPY . .

# Build
RUN pyinstaller --clean \
    --name KasaBasementBridge \
    --onefile \
    --add-data "templates:templates" \
    --add-data "config.json:." \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.loops.asyncio \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.http.h11_impl \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.protocols.websockets.websockets_impl \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import kasa \
    --hidden-import kasa.iot \
    --hidden-import kasa.discover \
    --collect-all kasa \
    --collect-all uvicorn \
    --collect-all fastapi \
    --collect-all jinja2 \
    kasa_bridge.py

# The executable will be in /build/dist/KasaBasementBridge
EOF

    echo "Building Docker image and compiling..."
    docker build --platform linux/arm/v7 -f Dockerfile.arm -t kasabridge-arm-build .
    
    echo "Extracting executable from Docker container..."
    docker create --name kasabridge-temp --platform linux/arm/v7 kasabridge-arm-build
    docker cp kasabridge-temp:/build/dist/KasaBasementBridge ./dist/KasaBasementBridge
    docker rm kasabridge-temp
    
    echo "Cleaning up Dockerfile..."
    rm -f Dockerfile.arm
    
else
    # Native build (will be for x86_64, not ARM - won't work on Pi)
    echo "WARNING: Native build will create x86_64 executable, not ARM!"
    echo "This will NOT work on Raspberry Pi. Use Docker method instead."
    
    pyinstaller --clean \
        --name KasaBasementBridge \
        --onefile \
        --add-data "templates:templates" \
        --add-data "config.json:." \
        --hidden-import uvicorn.loops.auto \
        --hidden-import uvicorn.loops.asyncio \
        --hidden-import uvicorn.protocols.http.auto \
        --hidden-import uvicorn.protocols.http.h11_impl \
        --hidden-import uvicorn.protocols.websockets.auto \
        --hidden-import uvicorn.protocols.websockets.websockets_impl \
        --hidden-import uvicorn.lifespan.on \
        --hidden-import kasa \
        --hidden-import kasa.iot \
        --hidden-import kasa.discover \
        --collect-all kasa \
        --collect-all uvicorn \
        --collect-all fastapi \
        --collect-all jinja2 \
        kasa_bridge.py
fi

# Check if build was successful
if [ -f "dist/KasaBasementBridge" ]; then
    echo ""
    echo "✓ Build successful!"
    echo "Executable location: dist/KasaBasementBridge"
    echo ""
    echo "File info:"
    file dist/KasaBasementBridge
    echo ""
    echo "To transfer to Raspberry Pi:"
    echo "  scp dist/KasaBasementBridge pi@raspberrypi.local:~/"
else
    echo ""
    echo "✗ Build failed!"
    exit 1
fi
