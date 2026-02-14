## KasaBasement

### Install (new machine)
Create a venv (recommended), then install deps:

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux/Mac:
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
python kasa_bridge.py
```

Then open `http://localhost:8000`.

### Deployment to Raspberry Pi

For detailed step-by-step instructions on building, deploying, and setting up the service on a Raspberry Pi, see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

This includes:
- Building the ARM executable using Docker on Windows
- Transferring the executable and templates to the Pi
- Setting up systemd for auto-start
- Troubleshooting common issues

### Building Executables

#### Windows
Build using PyInstaller:
```bash
pip install pyinstaller
pyinstaller --onefile --add-data "templates;templates" --add-data "config.json;." --name KasaBasementBridge kasa_bridge.py
```

#### Raspberry Pi (Linux ARM)

**Option A: Build on Raspberry Pi** (slower, but no Docker needed)
**Option B: Build on Windows/WSL using Docker** (much faster, recommended)

**Option A: Build on Raspberry Pi**

**Prerequisites:**
- Python 3.7 or higher must be installed. Raspberry Pi OS typically comes with Python 3 pre-installed.
- Check if Python 3 is installed: `python3 --version`
- If not installed, install it: `sudo apt update && sudo apt install python3 python3-pip python3-venv`
- **System build dependencies** (required for compiling Python packages):
  ```bash
  sudo apt update
  sudo apt install -y libffi-dev libssl-dev python3-dev build-essential
  ```

1. Transfer the project files to your Raspberry Pi (via SCP, USB, or git)

2. On the Raspberry Pi, run the build script:
```bash
chmod +x build_raspberry_pi.sh
./build_raspberry_pi.sh
```

Or manually:
```bash
# Create and activate venv
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install pyinstaller

# Build using the spec file
pyinstaller --clean KasaBasementBridge.spec

# Or build directly
pyinstaller --onefile --add-data "templates:templates" --add-data "config.json:." --name KasaBasementBridge kasa_bridge.py
```

The executable will be in `dist/KasaBasementBridge`. Run it with:
```bash
./dist/KasaBasementBridge
```

**Note for Raspberry Pi Zero 2 W:** The build process may take 10-20 minutes due to the limited CPU. Make sure you have sufficient disk space (at least 500MB free).

**Option B: Build on Windows using Docker Desktop** (Recommended - Much Faster!)

This method uses Docker Desktop to build an ARM executable on your Windows PC, which is much faster than building on the Pi.

**Prerequisites:**
- Docker Desktop for Windows installed and running
  - Download from: https://www.docker.com/products/docker-desktop
  - Install and make sure Docker Desktop is running (you'll see the Docker icon in the system tray)

**Steps:**

1. **Open PowerShell or Command Prompt** on Windows (you can use WSL, but PowerShell works fine)

2. **Navigate to your project directory:**
   ```powershell
   cd "C:\Users\chadp\OneDrive\python onedrive stuff\KasaBasement"
   ```

3. **Run the build script:**
   
   **Option 1: PowerShell script (easiest for Windows):**
   ```powershell
   .\build_docker_desktop.ps1
   ```
   
   **Option 2: Using WSL or Git Bash:**
   ```bash
   chmod +x build_wsl_docker.sh
   ./build_wsl_docker.sh
   ```
   
   **Option 3: From PowerShell using WSL:**
   ```powershell
   wsl bash build_wsl_docker.sh
   ```

4. **Wait for the build to complete** (typically 5-10 minutes on a modern PC). The script will:
   - Create a Docker container with ARM emulation
   - Install all dependencies
   - Build the ARM executable
   - Extract it to `dist/KasaBasementBridge`

5. **Verify the build:**
   ```powershell
   dir dist\KasaBasementBridge
   ```

6. **Transfer the executable to your Raspberry Pi:**
   ```powershell
   scp dist/KasaBasementBridge cpralle@KasaBasementPi.local:~/KasaBasement/
   ```

7. **On the Pi, make it executable and run:**
   ```bash
   ssh cpralle@KasaBasementPi.local
   cd ~/KasaBasement
   chmod +x KasaBasementBridge
   ./KasaBasementBridge
   ```

**Note:** 
- The Docker build typically takes 5-10 minutes on a modern PC vs 10-20 minutes on a Pi Zero 2 W
- Make sure Docker Desktop is running before starting the build (check the system tray)
- The first build will take longer as Docker downloads the base image

### Optional environment variables
- `SCENE_TRIGGER_TOKEN`: shared token for external triggers (`?token=...` or header `X-Token`)
- `KASA_USERNAME`, `KASA_PASSWORD`, `KASA_INTERFACE`: optional local-auth / interface settings for python-kasa
- `KASA_SCENE_DISCOVERY_TIMEOUT`, `KASA_HOST_CONNECT_TIMEOUT`, `KASA_STARTUP_DISCOVERY_TIMEOUT`, `KASA_PERIODIC_DISCOVERY_INTERVAL`



