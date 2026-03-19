#!/bin/bash
# Docker-based build for ARM using Docker Desktop
# This script works in WSL, Git Bash, or any bash environment on Windows
# Requires Docker Desktop for Windows to be installed and running

set -e

echo "Building KasaBasementBridge for Linux ARM using Docker Desktop..."
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Error: Docker is not installed or not in PATH."
    echo ""
    echo "Please install Docker Desktop for Windows:"
    echo "  https://www.docker.com/products/docker-desktop"
    echo ""
    echo "After installation:"
    echo "  1. Start Docker Desktop (check system tray for Docker icon)"
    echo "  2. Make sure Docker Desktop is running before running this script"
    exit 1
fi

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo "Error: Docker is not running."
    echo ""
    echo "Please start Docker Desktop:"
    echo "  1. Look for Docker icon in Windows system tray"
    echo "  2. If not running, start Docker Desktop from Start menu"
    echo "  3. Wait for Docker to fully start (icon should be steady, not animating)"
    echo "  4. Then run this script again"
    exit 1
fi

echo "✓ Docker is running"
echo ""

echo "Creating Dockerfile for ARM build..."

# Create Dockerfile
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

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir pyinstaller

# Copy project files
COPY kasa_bridge.py .
COPY templates/ templates/
COPY config.json .

# Build executable
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
EOF

echo "Building Docker image with ARM emulation..."
echo "This will take 5-10 minutes on a modern PC (much faster than building on Pi!)"
echo "The first build may take longer as Docker downloads the base image."
echo ""
docker build --platform linux/arm/v7 -f Dockerfile.arm -t kasabridge-arm-build .

echo "Extracting executable..."
# Create dist directory if it doesn't exist
mkdir -p dist

# Create temporary container and copy file
CONTAINER_ID=$(docker create --platform linux/arm/v7 kasabridge-arm-build)
docker cp $CONTAINER_ID:/build/dist/KasaBasementBridge ./dist/KasaBasementBridge
docker rm $CONTAINER_ID

# Clean up
rm -f Dockerfile.arm

# Verify
if [ -f "dist/KasaBasementBridge" ]; then
    echo ""
    echo "✓ Build successful!"
    echo "Executable location: dist/KasaBasementBridge"
    echo ""
    echo "File type:"
    file dist/KasaBasementBridge
    echo ""
    echo "To deploy to Raspberry Pi:"
    echo "  ./deploy_raspberry_pi.ps1 -SkipBuild"
    echo ""
    echo "Or manually transfer:"
    echo "  scp dist/KasaBasementBridge pi@raspberrypi.local:~/KasaBasement/"
    echo ""
    echo "(Configure your Pi host/user in deploy_config.json)"
    echo ""
    echo "Or make it executable and test:"
    echo "  chmod +x dist/KasaBasementBridge"
else
    echo "✗ Build failed - executable not found!"
    exit 1
fi
