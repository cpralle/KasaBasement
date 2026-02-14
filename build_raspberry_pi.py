#!/usr/bin/env python3
"""
Build script for Raspberry Pi Zero 2 W (Linux ARM)
Alternative to the shell script - can be run with: python3 build_raspberry_pi.py
"""

import subprocess
import sys
import os
from pathlib import Path

def run_command(cmd, check=True):
    """Run a shell command and print output"""
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, shell=isinstance(cmd, str), check=check, 
                          capture_output=False, text=True)
    return result.returncode == 0

def main():
    print("Building KasaBasementBridge for Raspberry Pi Zero 2 W...")
    print()
    
    # Check Python version
    if sys.version_info < (3, 7):
        print(f"Error: Python 3.7 or higher is required. Found Python {sys.version}")
        print("Install Python 3 with: sudo apt update && sudo apt install python3 python3-pip python3-venv")
        sys.exit(1)
    
    print(f"Using Python {sys.version}")
    print()
    
    # Check if we're in the right directory
    if not Path("kasa_bridge.py").exists():
        print("Error: kasa_bridge.py not found. Please run this script from the project root.")
        sys.exit(1)
    
    # Check if we're on a Raspberry Pi (optional warning)
    if os.path.exists("/proc/device-tree/model"):
        try:
            with open("/proc/device-tree/model", "r") as f:
                model = f.read()
                if "Raspberry Pi" not in model:
                    print(f"Warning: This doesn't appear to be a Raspberry Pi (detected: {model.strip()})")
        except:
            pass
    
    # Create virtual environment if it doesn't exist
    venv_path = Path("venv")
    if not venv_path.exists():
        print("Creating virtual environment...")
        if not run_command([sys.executable, "-m", "venv", "venv"]):
            print("Failed to create virtual environment")
            sys.exit(1)
    
    # Determine activation script
    if sys.platform == "win32":
        activate_script = venv_path / "Scripts" / "activate.bat"
        pip_path = venv_path / "Scripts" / "pip"
        python_path = venv_path / "Scripts" / "python"
    else:
        activate_script = venv_path / "bin" / "activate"
        pip_path = venv_path / "bin" / "pip"
        python_path = venv_path / "bin" / "python"
    
    # Upgrade pip
    print("Upgrading pip...")
    if not run_command([str(pip_path), "install", "--upgrade", "pip"]):
        print("Failed to upgrade pip")
        sys.exit(1)
    
    # Install dependencies
    print("Installing dependencies...")
    if not run_command([str(pip_path), "install", "-r", "requirements.txt"]):
        print("Failed to install dependencies")
        sys.exit(1)
    
    # Install PyInstaller
    print("Installing PyInstaller...")
    if not run_command([str(pip_path), "install", "pyinstaller"]):
        print("Failed to install PyInstaller")
        sys.exit(1)
    
    # Clean previous builds
    print("Cleaning previous builds...")
    for path in ["build", "dist", "__pycache__"]:
        if Path(path).exists():
            import shutil
            shutil.rmtree(path)
    
    # Remove old spec files (optional)
    for spec in Path(".").glob("*.spec"):
        if spec.name != "KasaBasementBridge.spec":
            spec.unlink()
    
    # Build using PyInstaller
    print("Building executable with PyInstaller...")
    print("(This may take 10-20 minutes on Raspberry Pi Zero 2 W)")
    
    pyinstaller_cmd = [
        str(python_path), "-m", "PyInstaller",
        "--clean",
        "--name", "KasaBasementBridge",
        "--onefile",
        "--add-data", "templates:templates",
        "--add-data", "config.json:.",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.protocols.websockets.websockets_impl",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "kasa",
        "--hidden-import", "kasa.iot",
        "--hidden-import", "kasa.discover",
        "--collect-all", "kasa",
        "--collect-all", "uvicorn",
        "--collect-all", "fastapi",
        "--collect-all", "jinja2",
        "kasa_bridge.py"
    ]
    
    if not run_command(pyinstaller_cmd):
        print("Build failed!")
        sys.exit(1)
    
    # Check if build was successful
    exe_path = Path("dist") / "KasaBasementBridge"
    if exe_path.exists():
        # Make executable on Linux
        if sys.platform != "win32":
            os.chmod(exe_path, 0o755)
        
        print()
        print("✓ Build successful!")
        print(f"Executable location: {exe_path.absolute()}")
        print()
        print("To run:")
        if sys.platform == "win32":
            print(f"  {exe_path}")
        else:
            print(f"  ./{exe_path}")
        print()
    else:
        print()
        print("✗ Build failed - executable not found!")
        sys.exit(1)

if __name__ == "__main__":
    main()
