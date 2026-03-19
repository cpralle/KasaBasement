# PowerShell script for building ARM executable using Docker Desktop
# Run this in PowerShell on Windows

$ErrorActionPreference = "Stop"

Write-Host "Building KasaBasementBridge for Linux ARM using Docker Desktop..." -ForegroundColor Cyan
Write-Host ""

# Check if Docker is available
try {
    $dockerVersion = docker --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed"
    }
    Write-Host "[OK] Docker found: $dockerVersion" -ForegroundColor Green
} catch {
    Write-Host "Error: Docker is not installed or not in PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Docker Desktop for Windows:" -ForegroundColor Yellow
    Write-Host "  https://www.docker.com/products/docker-desktop" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "After installation:" -ForegroundColor Yellow
    Write-Host "  1. Start Docker Desktop (check system tray for Docker icon)" -ForegroundColor Yellow
    Write-Host "  2. Make sure Docker Desktop is running before running this script" -ForegroundColor Yellow
    exit 1
}

# Check if Docker is running
try {
    docker info | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker not running"
    }
    Write-Host "[OK] Docker is running" -ForegroundColor Green
} catch {
    Write-Host "Error: Docker is not running." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please start Docker Desktop:" -ForegroundColor Yellow
    Write-Host "  1. Look for Docker icon in Windows system tray" -ForegroundColor Yellow
    Write-Host "  2. If not running, start Docker Desktop from Start menu" -ForegroundColor Yellow
    Write-Host "  3. Wait for Docker to fully start (icon should be steady, not animating)" -ForegroundColor Yellow
    Write-Host "  4. Then run this script again" -ForegroundColor Yellow
    exit 1
}

# Check if buildx is available
Write-Host "Checking Docker buildx support..." -ForegroundColor Cyan
try {
    docker buildx version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "buildx not available"
    }
    Write-Host "[OK] Docker buildx is available" -ForegroundColor Green
} catch {
    Write-Host "Warning: Docker buildx may not be fully configured for ARM emulation" -ForegroundColor Yellow
    Write-Host "This may cause issues. Continuing anyway..." -ForegroundColor Yellow
}

Write-Host ""

# Create Dockerfile
Write-Host "Creating Dockerfile for ARM build..." -ForegroundColor Cyan
$dockerfile = @"
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
"@

$dockerfile | Out-File -FilePath "Dockerfile.arm" -Encoding UTF8

# Set up buildx builder for ARM emulation
Write-Host ""
Write-Host "Setting up Docker buildx for ARM emulation..." -ForegroundColor Cyan
try {
    # Create builder with ARM platform support (this is what worked for you)
    docker buildx create --name arm-builder --driver docker-container --platform linux/arm/v7,linux/amd64 --use 2>$null | Out-Null
    docker buildx inspect --bootstrap | Out-Null
    Write-Host "[OK] Buildx builder configured for ARM emulation" -ForegroundColor Green
} catch {
    Write-Host "Note: Builder may already exist, trying to use it..." -ForegroundColor Yellow
    # Try to use existing builder
    docker buildx use arm-builder 2>$null | Out-Null
    docker buildx inspect --bootstrap 2>$null | Out-Null
}

# Build Docker image
Write-Host ""
Write-Host "Building Docker image with ARM emulation..." -ForegroundColor Cyan
Write-Host "This will take 5-10 minutes on a modern PC (much faster than building on Pi!)" -ForegroundColor Yellow
Write-Host "The first build may take longer as Docker downloads the base image." -ForegroundColor Yellow
Write-Host ""

docker buildx build --platform linux/arm/v7 -f Dockerfile.arm -t kasabridge-arm-build --load .

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[ERROR] Docker build failed!" -ForegroundColor Red
    Remove-Item -Path "Dockerfile.arm" -ErrorAction SilentlyContinue
    exit 1
}

# Create dist directory
if (-not (Test-Path "dist")) {
    New-Item -ItemType Directory -Path "dist" | Out-Null
}

# Extract executable
Write-Host ""
Write-Host "Extracting executable from Docker container..." -ForegroundColor Cyan
$containerId = docker create --platform linux/arm/v7 kasabridge-arm-build
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to create container. Trying alternative method..." -ForegroundColor Yellow
    # Alternative: run the container and copy
    docker run --platform linux/arm/v7 --name kasabridge-temp kasabridge-arm-build /bin/true
    docker cp "kasabridge-temp:/build/dist/KasaBasementBridge" "./dist/KasaBasementBridge"
    docker rm kasabridge-temp | Out-Null
} else {
    docker cp "${containerId}:/build/dist/KasaBasementBridge" "./dist/KasaBasementBridge"
    docker rm $containerId | Out-Null
}

# Clean up
Remove-Item -Path "Dockerfile.arm" -ErrorAction SilentlyContinue

# Verify
if (Test-Path "dist/KasaBasementBridge") {
    Write-Host ""
    Write-Host "[OK] Build successful!" -ForegroundColor Green
    Write-Host "Executable location: dist\KasaBasementBridge" -ForegroundColor Cyan
    Write-Host ""
    
    $fileInfo = Get-Item "dist/KasaBasementBridge"
    Write-Host "File size: $([math]::Round($fileInfo.Length / 1MB, 2)) MB" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "To transfer to Raspberry Pi:" -ForegroundColor Yellow
    Write-Host "  scp dist\KasaBasementBridge pi@raspberrypi.local:~/KasaBasement/" -ForegroundColor White
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "[ERROR] Build failed - executable not found!" -ForegroundColor Red
    exit 1
}
