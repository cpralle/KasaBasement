# Smart Home Setup Guide

This guide explains how to set up TP-Link Kasa smart bulbs for use with KasaBasement.

## Compatible Devices

KasaBasement works with **TP-Link Kasa smart bulbs** that support local network control. Tested models include:

- **KL125** - Kasa Smart Wi-Fi Light Bulb, Multicolor
- **KL130** - Kasa Smart Wi-Fi Light Bulb, Multicolor
- **KL135** - Kasa Smart Wi-Fi Light Bulb, Multicolor
- **LB130** - Kasa Smart Wi-Fi Light Bulb, Multicolor

Other Kasa bulbs with color/color-temperature support should also work. The app uses the [python-kasa](https://github.com/python-kasa/python-kasa) library for device communication.

**Note:** This app controls bulbs over your **local network** - it does not use TP-Link's cloud service. Your bulbs must be on the same network as the machine running KasaBasement.

## Network Requirements

1. **Same network**: The computer/Raspberry Pi running KasaBasement must be on the same local network (subnet) as your Kasa bulbs
2. **UDP broadcast**: Device discovery uses UDP broadcast on port 9999. Ensure your router/firewall allows this
3. **Static IP for host**: The device running KasaBasement should have a static IP so integrations (Home Assistant, physical buttons) can reliably reach it
4. **Static IPs for bulbs**: For reliability, assign static IPs to your bulbs via your router's DHCP settings

## Setting Up a Static IP for the Host Device

The device running KasaBasement (your computer, Raspberry Pi, or server) needs a static IP address. Without one, your router may assign it a different IP after a reboot, breaking any external integrations.

### Option 1: Router DHCP Reservation (Recommended)

This is the easiest method - your router always assigns the same IP to a specific device.

1. **Find your device's MAC address**:
   - **Windows**: Open Command Prompt, run `ipconfig /all`, look for "Physical Address"
   - **Linux/Raspberry Pi**: Run `ip link show` or `cat /sys/class/net/eth0/address`
   - **Mac**: System Preferences > Network > Advanced > Hardware

2. **Log into your router** (usually http://192.168.1.1 or http://192.168.0.1)

3. **Find DHCP Reservation settings** (location varies by router):
   - Look under LAN Settings, DHCP, or Address Reservation
   - Common locations: Advanced > LAN > DHCP Server

4. **Add a reservation**:
   - Enter your device's MAC address
   - Assign a static IP (e.g., `192.168.1.50`)
   - Choose an IP outside your router's DHCP pool, or within the reserved range

5. **Reboot your device** to pick up the new IP assignment

### Option 2: Static IP on Raspberry Pi

Configure the Pi itself to use a static IP.

**Using NetworkManager (Raspberry Pi OS Bookworm and newer):**

```bash
# List connections
nmcli con show

# Set static IP for wired connection (replace "Wired connection 1" with your connection name)
sudo nmcli con mod "Wired connection 1" ipv4.addresses 192.168.1.50/24
sudo nmcli con mod "Wired connection 1" ipv4.gateway 192.168.1.1
sudo nmcli con mod "Wired connection 1" ipv4.dns "8.8.8.8 8.8.4.4"
sudo nmcli con mod "Wired connection 1" ipv4.method manual

# Apply changes
sudo nmcli con up "Wired connection 1"
```

**Using dhcpcd (older Raspberry Pi OS):**

Edit `/etc/dhcpcd.conf`:

```bash
sudo nano /etc/dhcpcd.conf
```

Add at the end (adjust for your network):

```
# Static IP for eth0 (wired)
interface eth0
static ip_address=192.168.1.50/24
static routers=192.168.1.1
static domain_name_servers=8.8.8.8 8.8.4.4

# Or for Wi-Fi (wlan0)
interface wlan0
static ip_address=192.168.1.50/24
static routers=192.168.1.1
static domain_name_servers=8.8.8.8 8.8.4.4
```

Then reboot:

```bash
sudo reboot
```

### Option 3: Static IP on Windows

1. Open **Settings > Network & Internet > Ethernet** (or Wi-Fi)
2. Click on your network connection
3. Under "IP assignment", click **Edit**
4. Change from "Automatic (DHCP)" to **Manual**
5. Enable **IPv4** and enter:
   - IP address: `192.168.1.50` (choose one not in use)
   - Subnet mask: `255.255.255.0`
   - Gateway: `192.168.1.1` (your router's IP)
   - Preferred DNS: `8.8.8.8`
6. Click **Save**

### Option 4: Static IP on Linux (systemd-networkd)

Create a network configuration file:

```bash
sudo nano /etc/systemd/network/10-static-eth0.network
```

Add:

```ini
[Match]
Name=eth0

[Network]
Address=192.168.1.50/24
Gateway=192.168.1.1
DNS=8.8.8.8
```

Enable and restart:

```bash
sudo systemctl enable systemd-networkd
sudo systemctl restart systemd-networkd
```

### Verifying Your Static IP

After configuration, verify your IP:

```bash
# Windows
ipconfig

# Linux/Mac/Raspberry Pi
ip addr show
# or
hostname -I
```

Your device should now always have the same IP address, making it reliable for integrations and bookmarks.

## Initial Bulb Setup

Before using KasaBasement, set up your bulbs using the official Kasa app:

1. **Download the Kasa app** on your phone (iOS/Android)
2. **Create a TP-Link account** (required for initial setup)
3. **Add each bulb** following the app's instructions:
   - Put the bulb in pairing mode (usually by turning it on/off 3 times)
   - Connect to the bulb's temporary Wi-Fi network
   - Enter your home Wi-Fi credentials
   - Name the bulb (this becomes the "alias" in KasaBasement)
4. **Verify the bulb works** via the Kasa app before proceeding

Once bulbs are on your Wi-Fi network, KasaBasement can discover and control them locally.

## Setting Up KasaBasement

### 1. Install and Run

```bash
# Clone the repository
git clone https://github.com/cpralle/KasaBasement.git
cd KasaBasement

# Create virtual environment
python -m venv .venv

# Activate (Windows PowerShell)
.\.venv\Scripts\Activate.ps1
# Or Linux/Mac
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the app
python kasa_bridge.py
```

Open http://localhost:8000 in your browser.

### 2. Discover Your Devices

1. Go to the **Settings** page (link in the header)
2. Click **Rescan Network** to discover all Kasa devices on your network
3. The "Discovered Devices" section shows all found devices with their:
   - Alias (name you set in Kasa app)
   - IP address
   - MAC address
   - Device type

### 3. Add Devices to Config

From the Settings page:

1. Check the boxes next to devices you want to add
2. Click **Add Selected Devices**
3. The devices are now saved to your `config.json`

Alternatively, manually create `config.json` from the template:

```bash
cp config.example.json config.json
```

Then edit `config.json` to add your devices:

```json
{
    "devices": [
        {
            "alias": "Living Room Bulb 01",
            "mac": "AA:BB:CC:DD:EE:01",
            "host": "192.168.1.100",
            "type": "Bulb"
        }
    ],
    "scenes": [],
    "rooms": [],
    "routines": []
}
```

## Creating Scenes

Scenes let you set multiple bulbs to specific colors/brightness with one click.

### Via Web UI

1. Go to the **Scenes** page
2. Click **Create New Scene**
3. Enter a scene name
4. For each device, set:
   - **Action**: on/off
   - **Brightness**: 1-100%
   - **Color**: hex color (e.g., `#ff0000` for red) OR color temperature (2500-9000K)
5. Click **Save**

### Scene Options

- **Dim Profile**: How brightness scales when dimming
  - `linear`: Even brightness steps
  - `aggressive`: Faster drop to low brightness (good for ambient lighting)
- **Room Index**: Associate scene with a room for toggle/cycle behavior

## Creating Rooms

Rooms group devices for easier control and enable the room grid visualization.

### Via Web UI

1. Go to the **Map** page
2. Click **Create Room**
3. Enter the room name and grid dimensions (rows x columns)
4. Drag devices to their positions on the grid

### Room Features

- **Toggle**: Turn all room lights on/off, remembering the last active scene
- **Cycle**: Cycle through scenes associated with the room
- **Dimming**: Apply brightness presets (d1-d4) to all room lights

## Creating Routines

Routines run scenes automatically at scheduled times.

### Via Web UI

1. Go to the **Routines** page
2. Click **Create Routine**
3. Set:
   - **Name**: Descriptive name
   - **Time**: 24-hour format (e.g., `07:30`)
   - **Actions**: Select scene(s) to activate or room group actions
   - **Enabled**: Toggle routine on/off
4. Click **Save**

Routines run once per day at the specified time (won't re-trigger if already run that day).

## External Integrations

### API Endpoints

KasaBasement exposes REST endpoints for integration with other systems:

```bash
# Trigger a scene by name
GET /api/trigger/scene/BasementOn
POST /api/trigger/scene/BasementOn

# Toggle room lights
GET /api/Basement/toggle
POST /api/Basement/toggle

# Cycle through room scenes
GET /api/Basement/cycle

# Set dimming level (d1-d4)
GET /api/Basement/dimming_d2
```

### Secure Triggers

For external access, set a token via environment variable:

```bash
export SCENE_TRIGGER_TOKEN=your-secret-token
python kasa_bridge.py
```

Then include the token in requests:

```bash
curl "http://your-host:8000/api/trigger/scene/BasementOn?token=your-secret-token"
# Or via header
curl -H "X-Token: your-secret-token" http://your-host:8000/api/trigger/scene/BasementOn
```

### Home Assistant Integration

You can integrate with Home Assistant using REST commands:

```yaml
# configuration.yaml
rest_command:
  basement_on:
    url: "http://192.168.1.50:8000/api/trigger/scene/BasementOn"
    method: GET
  basement_toggle:
    url: "http://192.168.1.50:8000/api/Basement/toggle"
    method: POST
```

### Physical Button Integration

For physical buttons (e.g., Flic, Shelly), configure them to call the HTTP endpoints above.

## Troubleshooting

### Devices Not Discovered

1. **Check network**: Ensure the machine running KasaBasement is on the same network as the bulbs
2. **Firewall**: Allow UDP port 9999 for device discovery
3. **Bulb firmware**: Some newer firmware may require authentication. Set environment variables:
   ```bash
   export KASA_USERNAME=your-tplink-email
   export KASA_PASSWORD=your-tplink-password
   ```
4. **Increase timeout**: Set `KASA_STARTUP_DISCOVERY_TIMEOUT=15` for slower networks

### Commands Fail or Are Slow

1. **Network congestion**: Reduce the number of simultaneous commands
2. **IP changes**: If bulbs have dynamic IPs, update `config.json` after discovery
3. **Use static IPs**: Configure your router to assign fixed IPs to bulb MAC addresses

### Bulbs Unresponsive

1. **Power cycle**: Turn the bulb's physical switch off/on
2. **Re-pair**: If persistent, re-add the bulb via the Kasa app
3. **Check Wi-Fi**: Ensure bulbs have good signal strength

## Tips for Reliable Operation

1. **Assign static IPs** to all bulbs via your router
2. **Run on a dedicated device** like a Raspberry Pi for 24/7 operation
3. **Use systemd** on Linux to auto-start the service (see [DEPLOYMENT.md](DEPLOYMENT.md))
4. **Monitor logs** for connection issues or timeouts
5. **Group bulbs by circuit** if you have physical switches that cut power

## Config File Reference

See [config.example.json](config.example.json) for the complete structure. Key sections:

| Section | Purpose |
|---------|---------|
| `devices` | List of Kasa bulbs with alias, MAC, IP, type |
| `scenes` | Named lighting presets with per-device settings |
| `rooms` | Room definitions with grid layout for visualization |
| `routines` | Scheduled automation rules |

## Getting Help

- Check the [README.md](README.md) for installation and deployment options
- See [DEPLOYMENT.md](DEPLOYMENT.md) for Raspberry Pi setup
- Open an issue on GitHub for bugs or feature requests
