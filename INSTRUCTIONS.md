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
3. **Static IPs recommended**: For reliability, consider assigning static IPs to your bulbs via your router's DHCP settings

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
