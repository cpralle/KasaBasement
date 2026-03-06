# KasaBasementBridge - Deployment Instructions

This document contains step-by-step instructions for building and deploying KasaBasementBridge to a Raspberry Pi.

## Prerequisites

- Windows PC with Docker Desktop installed and running
- Raspberry Pi with SSH access
- Project files on Windows PC

## Quick Deploy Script (Recommended)

If you want build + deploy in one command from Windows PowerShell:

```powershell
.\deploy_raspberry_pi.ps1
```

Common options:

```powershell
# Do not build, only deploy local dist/KasaBasementBridge
.\deploy_raspberry_pi.ps1 -SkipBuild

# Preview what would run
.\deploy_raspberry_pi.ps1 -DryRun

# Custom target
.\deploy_raspberry_pi.ps1 -PiHost KasaBasementPi.local -PiUser cpralle -RemoteDir ~/KasaBasement -ServiceName kasabasement

# Explicit SSH key (recommended for passwordless deploy)
.\deploy_raspberry_pi.ps1 -PiHost 192.168.1.37 -PiUser cpralle -SshKeyPath "$env:USERPROFILE\secrets\kasabridge_deployKey"
```

The script performs:
1. Local build via `build_docker_desktop.ps1` (unless `-SkipBuild`)
2. Remote service stop
3. Upload executable (+ templates by default)
4. `chmod +x`, service start, status output

## Building the Executable (Windows)

1. **Open PowerShell** and navigate to the project directory:
   ```powershell
   cd "C:\Users\chadp\OneDrive\python onedrive stuff\KasaBasement"
   ```
   or

   ```powershell
   cd "D:\OneDrive\python onedrive stuff\KasaBasement"
   ```

2. **Make sure Docker Desktop is running** (check system tray for Docker icon)

3. **Run the build script:**
   ```powershell
   .\build_docker_desktop.ps1
   ```
   
   This will:
   - Build an ARM executable using Docker
   - Take 5-10 minutes (longer on first build)
   - Create `dist\KasaBasementBridge`

4. **Verify the build:**
   ```powershell
   dir dist\KasaBasementBridge
   ```

## Transferring to Raspberry Pi

1. **Stop the service** (if already running — the OS won't let you overwrite a running executable):
   ```powershell
   ssh cpralle@KasaBasementPi.local "sudo systemctl stop kasabasement"
   ```

2. **Transfer the executable:**
   ```powershell
   scp dist\KasaBasementBridge cpralle@KasaBasementPi.local:~/KasaBasement/
   ```

   Enter your Pi password when prompted.

3. **Transfer the templates folder:**
   ```powershell
   scp -r templates cpralle@KasaBasementPi.local:~/KasaBasement/
   ```

   This copies all HTML template files needed for the web interface. The application will look for templates in `~/KasaBasement/templates/` if they aren't bundled in the executable.

4. **SSH into the Pi:**
   ```powershell
   ssh cpralle@KasaBasementPi.local
   ```

5. **Make it executable and restart the service:**
   ```bash
   cd ~/KasaBasement
   chmod +x KasaBasementBridge
   sudo systemctl start kasabasement
   ```

## Setting Up Auto-Start (systemd Service)

### First Time Setup

1. **Create the systemd service file:**
   ```bash
   sudo nano /etc/systemd/system/kasabasement.service
   ```

2. **Paste this content:**
   ```ini
   [Unit]
   Description=KasaBasementBridge
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=cpralle
   WorkingDirectory=/home/cpralle/KasaBasement
   ExecStart=/home/cpralle/KasaBasement/KasaBasementBridge
   Restart=on-failure
   RestartSec=2
   Environment=PYTHONUNBUFFERED=1

   # Optional: if you need environment variables (SCENE_TRIGGER_TOKEN, etc.)
   # EnvironmentFile=-/home/cpralle/KasaBasement/.env

   [Install]
   WantedBy=multi-user.target
   ```

3. **Save and exit nano:**
   - Press `Ctrl+X`
   - Press `Y` to confirm
   - Press `Enter` to confirm filename

4. **Enable and start the service:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable kasabasement
   sudo systemctl start kasabasement
   ```

5. **Verify it's running:**
   ```bash
   sudo systemctl status kasabasement
   ```

### Optional: Environment Variables

If you need to set environment variables (like `SCENE_TRIGGER_TOKEN`):

1. **Create .env file:**
   ```bash
   nano /home/cpralle/KasaBasement/.env
   ```

2. **Add your variables:**
   ```bash
   SCENE_TRIGGER_TOKEN=your_token_here
   KASA_USERNAME=your_local_kasa_user
   KASA_PASSWORD=your_local_kasa_pass
   KASA_INTERFACE=
   ```

3. **Uncomment the EnvironmentFile line in the service file:**
   ```bash
   sudo nano /etc/systemd/system/kasabasement.service
   ```
   
   Change:
   ```ini
   # EnvironmentFile=-/home/cpralle/KasaBasement/.env
   ```
   
   To:
   ```ini
   EnvironmentFile=-/home/cpralle/KasaBasement/.env
   ```

4. **Reload and restart:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart kasabasement
   ```

## Managing the Service

### Check Status
```bash
sudo systemctl status kasabasement
```

### View Logs
```bash
# Follow logs in real-time
journalctl -u kasabasement -f

# View recent logs
journalctl -u kasabasement -n 50
```

### Restart Service
```bash
sudo systemctl restart kasabasement
```

### Stop Service
```bash
sudo systemctl stop kasabasement
```

### Start Service
```bash
sudo systemctl start kasabasement
```

### Disable Auto-Start (if needed)
```bash
sudo systemctl disable kasabasement
```

### Enable Auto-Start Again
```bash
sudo systemctl enable kasabasement
```

## Testing

1. **Check if service is running:**
   ```bash
   sudo systemctl status kasabasement
   ```

2. **Test web interface:**
   - From browser: `http://[PI_IP_ADDRESS]:8000`
   - Or from Pi: `http://localhost:8000`

3. **Test Flic triggers** (if configured)

4. **Reboot Pi to verify auto-start:**
   ```bash
   sudo reboot
   ```
   
   After reboot, check:
   ```bash
   sudo systemctl status kasabasement
   ```

## Troubleshooting

### Service Won't Start

1. **Check logs:**
   ```bash
   journalctl -u kasabasement -n 50
   ```

2. **Verify executable exists and is executable:**
   ```bash
   ls -l ~/KasaBasement/KasaBasementBridge
   chmod +x ~/KasaBasement/KasaBasementBridge
   ```

3. **Check if port 8000 is already in use:**
   ```bash
   sudo lsof -i :8000
   sudo netstat -tlnp | grep 8000
   ```

### "TemplateNotFound: index.html" Error

If you see `jinja2.exceptions.TemplateNotFound: index.html` in the logs:

1. **Check if templates folder exists:**
   ```bash
   ls -la ~/KasaBasement/templates/
   ```

2. **If missing, copy templates from Windows:**
   ```powershell
   # From Windows PowerShell
   scp -r templates cpralle@KasaBasementPi.local:~/KasaBasement/
   ```

3. **Verify all template files are present:**
   ```bash
   ls ~/KasaBasement/templates/
   # Should show: index.html, settings.html, scenes.html, map.html,
   #              room_map.html, rooms.html, routines.html, diagnostics.html
   ```

4. **Restart the service:**
   ```bash
   sudo systemctl restart kasabasement
   ```

### Permission Denied

If you get "Permission denied" when running the executable:

1. **Make sure it's executable:**
   ```bash
   chmod +x ~/KasaBasement/KasaBasementBridge
   ```

2. **Check if filesystem is mounted with noexec:**
   ```bash
   mount | grep /home
   ```
   
   If you see `noexec`, move the executable to a different location:
   ```bash
   mkdir -p ~/bin
   cp ~/KasaBasement/KasaBasementBridge ~/bin/
   chmod +x ~/bin/KasaBasementBridge
   ```
   
   Then update the service file `ExecStart` path.

### Build Issues on Windows

1. **Make sure Docker Desktop is running**

2. **Check Docker is accessible:**
   ```powershell
   docker --version
   docker info
   ```

3. **If buildx issues, set it up:**
   ```powershell
   docker buildx create --name arm-builder --driver docker-container --platform linux/arm/v7,linux/amd64 --use
   docker buildx inspect --bootstrap
   ```

## Quick Reference

### Full Deployment Workflow

```powershell
# On Windows - Build and deploy
cd "C:\Users\chadp\OneDrive\python onedrive stuff\KasaBasement"
.\build_docker_desktop.ps1
ssh cpralle@KasaBasementPi.local "sudo systemctl stop kasabasement"
scp dist\KasaBasementBridge cpralle@KasaBasementPi.local:~/KasaBasement/
scp -r templates cpralle@KasaBasementPi.local:~/KasaBasement/
```

```bash
# On Pi - Make executable and start
ssh cpralle@KasaBasementPi.local
cd ~/KasaBasement
chmod +x KasaBasementBridge
sudo systemctl start kasabasement
sudo systemctl status kasabasement
```

### Service Management Commands

```bash
# Status
sudo systemctl status kasabasement

# Logs
journalctl -u kasabasement -f

# Restart
sudo systemctl restart kasabasement

# Stop
sudo systemctl stop kasabasement

# Start
sudo systemctl start kasabasement
```
