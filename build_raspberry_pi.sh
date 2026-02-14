#!/bin/bash
# Build script for Raspberry Pi Zero 2 W (Linux ARM)
# Run this script on your Raspberry Pi

set -e  # Exit on error

echo "Building KasaBasementBridge for Raspberry Pi Zero 2 W..."

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed."
    echo "Install it with: sudo apt update && sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Found Python $PYTHON_VERSION"

# Check if we're on a Raspberry Pi
if [ ! -f /proc/device-tree/model ] || ! grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
    echo "Warning: This doesn't appear to be a Raspberry Pi. Continuing anyway..."
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

# Check if build was successful
if [ -f "dist/KasaBasementBridge" ]; then
    echo ""
    echo "✓ Build successful!"
    echo "Executable location: dist/KasaBasementBridge"
    echo ""
    echo "To run:"
    echo "  ./dist/KasaBasementBridge"
    echo ""
    echo "To make it executable (if needed):"
    echo "  chmod +x dist/KasaBasementBridge"
else
    echo ""
    echo "✗ Build failed!"
    exit 1
fi
