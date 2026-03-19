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

## Overview: How KasaBasement Works

KasaBasement provides a web-based control center for your Kasa smart bulbs. Here's the core concept:

### The Building Blocks

1. **Devices** - Your individual smart bulbs, discovered automatically on your network
2. **Scenes** - Saved lighting presets that set multiple bulbs to specific colors/brightness
3. **Rooms** - Groups of devices with a visual grid layout representing their physical positions
4. **Routines** - Scheduled automations that trigger scenes at specific times

### Typical Workflow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Devices   │ ──► │   Scenes    │ ──► │   Rooms     │
│ (discover)  │     │ (create)    │     │ (organize)  │
└─────────────┘     └─────────────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  Routines   │
                    │ (automate)  │
                    └─────────────┘
```

### Example: Setting Up a Basement with 15 Bulbs

**Step 1: Discover and add devices**
- Open Settings, click "Rescan Network"
- Select all 15 basement bulbs, click "Add Selected"

**Step 2: Create scenes for different moods**
- **"BasementOn"** - All bulbs at 100% warm white (2700K) for working
- **"MovieTime"** - Dim ambient lighting, bulbs near TV at 5%, others off
- **"Party"** - Colorful mix: some red, some blue, some purple at various brightness

**Step 3: Create a room and map bulb positions**
- Create room "Basement" with an 8x11 grid
- Place each bulb on the grid matching their physical ceiling positions
- Now the Map page shows a live visualization of your lights

**Step 4: Set up routines**
- **"Morning Reset"** at 04:30 - Activate "BasementOn" scene so lights are ready
- **"Evening Dim"** at 22:00 - Switch to "MovieTime" automatically

### Daily Usage Examples

**Manual control via Dashboard:**
- Click a scene card to activate it (all bulbs change instantly)
- Click an active scene again to toggle the room off
- Use per-device controls to fine-tune individual bulbs

**Voice/button control via API:**
```bash
# "Hey Google, turn on basement lights" triggers:
curl http://192.168.1.50:8000/api/trigger/scene/BasementOn

# Physical button by the stairs triggers:
curl http://192.168.1.50:8000/api/Basement/toggle
```

**Dimming for ambiance:**
```bash
# Reduce current scene to 25% brightness
curl http://192.168.1.50:8000/api/Basement/dimming_d2
```

### The Map Visualization

The Map page shows your room as a grid with colored tiles representing each bulb:
- **Tile color** = bulb's current color
- **Tile brightness** = bulb's current brightness (darker = dimmer)
- **Yellow ring** = command in progress
- **Red ring** = command failed

This gives you a birds-eye view of your entire lighting setup, updating in real-time as scenes run.

### The Core Use Case: Physical Dimmer with Multi-Scene Support

The primary motivation for KasaBasement was to enable a **single physical controller** to manage multiple scenes with persistent dimming - something the Kasa app doesn't support natively.

**The Problem**: You have a room with several lighting scenes (bright work light, dim movie mode, colorful party mode). You want a physical dial or button to:
- Turn lights on/off
- Switch between scenes
- Dim the current scene

But traditional smart home setups treat dimming and scenes as separate - when you switch scenes, the dimmer resets.

**The Solution**: KasaBasement maintains dimming state independently of the active scene. When you dim to 50% and then cycle to a different scene, the new scene also applies at 50% brightness.

### Example: Flic Twist Setup

The [Flic Twist](https://flic.io/) is a smart button with a rotating dial - perfect for this use case. Here's how to configure it:

**Flic Twist Actions:**

| Gesture | Action | API Endpoint |
|---------|--------|--------------|
| Single tap | Toggle room on/off | `GET /api/Basement/toggle` |
| Double tap | Cycle to next scene | `GET /api/Basement/cycle` |
| Twist clockwise | Increase brightness | `GET /api/Basement/dimming_d1` (or d2, d3) |
| Twist counter-clockwise | Decrease brightness | `GET /api/Basement/dimming_d4` (or d3, d2) |

**How it works in practice:**

1. **Room is off.** Single tap → room turns on with last active scene ("BasementOn" at 100%)
2. **Too bright.** Twist counter-clockwise → brightness drops to 50% (d2 level)
3. **Want movie mode.** Double tap → cycles to "MovieTime" scene, *still at 50% brightness*
4. **Done watching.** Single tap → room turns off
5. **Next day.** Single tap → room turns on with "MovieTime" at 50% (remembers both scene and dim level)

**The key insight**: The room remembers:
- Which scene was last active
- What dim level was applied
- Whether the room was on or off

This gives you intuitive physical control without needing to think about which scene is active or what the brightness was.

**Flic App Configuration:**

In the Flic app, set up HTTP requests for each gesture:

```
Single Click:
  URL: http://192.168.1.50:8000/api/Basement/toggle
  Method: GET

Double Click:
  URL: http://192.168.1.50:8000/api/Basement/cycle
  Method: GET

Rotate Right (or use multiple positions):
  URL: http://192.168.1.50:8000/api/Basement/dimming_d1
  Method: GET

Rotate Left:
  URL: http://192.168.1.50:8000/api/Basement/dimming_d4
  Method: GET
```

For the twist dial, you can map different rotation amounts to different dim levels (d1 = brightest, d4 = dimmest), or use the twist positions to directly set brightness.

## Network Requirements

1. **Same network**: The computer/Raspberry Pi running KasaBasement must be on the same local network (subnet) as your Kasa bulbs
2. **UDP broadcast**: Device discovery uses UDP broadcast on port 9999. Ensure your router/firewall allows this
3. **Static IP for host**: The device running KasaBasement should have a static IP so integrations (Home Assistant, physical buttons) can reliably reach it
4. **Static IPs for bulbs**: For reliability, assign static IPs to your bulbs via your router's DHCP settings

## Choosing Your Host Device: PC vs Raspberry Pi

KasaBasement can run on any device with Python 3.7+. The two most common choices are a regular PC/laptop or a Raspberry Pi. Here's how to decide:

### Raspberry Pi

A small, dedicated single-board computer that runs 24/7.

| Pros | Cons |
|------|------|
| Low power (~3-5W) - costs pennies per month to run | Initial setup requires more technical knowledge |
| Silent, no fans (most models) | Slower than a PC - build times are longer |
| Small form factor - tuck it anywhere | Need to purchase separately ($35-75 + SD card + power supply) |
| Dedicated device - won't be rebooted for Windows updates | SD cards can wear out over time (mitigate with USB boot) |
| Always on - routines run reliably | Remote access requires SSH setup |
| Headless operation - no monitor needed | Limited RAM may matter for very large configurations |
| Great for permanent "set and forget" deployment | |

**Best Raspberry Pi models for KasaBasement:**
- **Raspberry Pi 4 (2GB+)** - Best performance, recommended
- **Raspberry Pi 3B+** - Good balance of price and performance
- **Raspberry Pi Zero 2 W** - Cheapest option, adequate for small setups (10-20 bulbs)

### PC or Laptop (Windows/Mac/Linux)

Use an existing computer you already have.

| Pros | Cons |
|------|------|
| No additional hardware purchase | Higher power consumption (50-200W+) |
| Familiar environment - easy to set up and debug | May not be on 24/7 - routines won't run when off |
| Faster performance | Reboots for updates interrupt service |
| Easy access to logs and web UI | Takes up desk/floor space |
| Good for testing and development | Fan noise |
| Can run alongside other applications | Computer going to sleep breaks the service |

**When a PC makes sense:**
- Testing KasaBasement before committing to a dedicated device
- You have an always-on home server already
- You only need manual scene control (no scheduled routines)
- Development and debugging

### Recommendation

For **permanent, reliable operation**: Use a Raspberry Pi. The low power consumption and always-on nature make it ideal for home automation. A Pi 4 with 2GB RAM is more than enough for even large setups.

For **testing or occasional use**: Start with your PC. Once you're happy with your configuration, consider migrating to a Pi for 24/7 operation.

### Hybrid Approach

Many users do both:
1. **Develop and configure on PC** - easier to edit config, test scenes, debug issues
2. **Deploy to Raspberry Pi** - use the included deploy script for production

See [DEPLOYMENT.md](DEPLOYMENT.md) for instructions on deploying to a Raspberry Pi.

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

## Creating Rooms and Using the Map

Rooms group devices for easier control and enable a visual grid representation of your physical light layout. The Map feature is one of the most powerful parts of KasaBasement - it lets you see and control your lights based on where they actually are in your space.

### Why Use Rooms?

- **Visual feedback**: See all your lights as a grid matching their ceiling/wall positions
- **Real-time updates**: Watch colors change as scenes run
- **Group control**: Toggle, cycle, and dim entire rooms with one command
- **Scene association**: Link scenes to rooms for smart toggle behavior

### Creating a Room

1. Go to the **Map** page (link in the navigation bar)
2. Click **Create Room**
3. Enter a room name (e.g., "Basement", "Living Room")
4. The room is created with a default 8x8 grid

### Mapping Your Lights to the Grid

The grid represents a top-down view of your room. Each cell can hold one light.

1. From the **Map** page, click on a room name to open the room editor
2. You'll see an empty grid of cells
3. **Click any cell** to open the tile editor
4. Select a device from the dropdown
5. Set the default action, brightness, and color for that position
6. Click **Save**

**Tips for mapping:**
- Walk through your room and note where each bulb is physically located
- Map bulbs to grid positions that match their real-world layout
- Leave empty cells for areas without lights (walkways, furniture, etc.)
- The grid doesn't have to be perfectly to scale - approximate positions work fine

### Resizing the Grid

If the default 8x8 grid doesn't fit your room:

1. Open the room editor
2. Find the "Grid size" controls in the top-right panel
3. Enter new row and column counts (1-20 each)
4. Click **Apply**

The grid will resize, preserving existing device placements where possible.

### Rearranging Lights

You can drag and drop lights to new positions:

1. **Drag** a tile with a light to an empty cell to move it
2. **Drag** onto another light to swap positions
3. Changes save automatically

### Understanding the Live View

The room map updates in real-time to show your lights' current state:

| Visual | Meaning |
|--------|---------|
| Colored tile | Light is on, tile color = light color |
| Dark/black tile | Light is off |
| Bright tile | High brightness |
| Dim tile | Low brightness |
| Yellow ring | Command pending (waiting for confirmation) |
| Red ring | Command failed |
| Device label | Shows which bulb is in each position |

### Render Mode Options

The room editor has two render modes (top-right panel):

- **Max brightness**: Shows colors at full saturation, ignoring brightness level. Good for seeing what color each light is set to.
- **Current brightness**: Shows colors dimmed to match actual brightness. More realistic representation of what you see in person.

### Data Source Options

- **Live status**: Tiles update based on actual device state (polls the bulbs)
- **Map config**: Shows the saved default colors from your grid configuration

### Room Features

Once a room is set up, you get these controls:

**Toggle** (`/api/{room}/toggle`)
- If room is on: turns all lights off
- If room is off: restores the last active scene
- Remembers which scene was active, so toggling back on returns to the same look

**Cycle** (`/api/{room}/cycle`)
- Cycles through all scenes associated with this room
- Great for a physical button: press to cycle through Movie → Party → Bright → Movie...

**Dimming** (`/api/{room}/dimming_d1` through `d4`)
- Applies brightness presets to the current scene
- d1 = brightest, d4 = dimmest
- Uses the scene's dim profile (linear or aggressive) for natural-feeling dimming

### Associating Scenes with Rooms

When you create or edit a scene, you can set its "Room Index":

1. Go to **Scenes** page
2. Edit a scene
3. Set the **Room** dropdown to your room
4. Save

Now that scene:
- Appears in room toggle/cycle rotation
- Updates the room's "active scene" when triggered
- Shows as active (green) on the dashboard when the room is on with that scene

### Example: Complete Room Setup

**Scenario**: Basement with 15 ceiling lights in a roughly 4x5 pattern

1. **Create the room**:
   - Map page → Create Room → "Basement"
   - Resize to 5 rows × 6 columns (a bit of margin)

2. **Map the lights**:
   - Click cell (0,0) → select "Basement LED 01" → set white, 100% → Save
   - Click cell (0,2) → select "Basement LED 02" → set white, 100% → Save
   - Continue for all 15 bulbs, matching their ceiling positions

3. **Create scenes**:
   - "BasementOn": All bulbs warm white at 100%, Room = Basement
   - "Dim": All bulbs warm white at 20%, Room = Basement
   - "Movie": Back bulbs off, front bulbs at 5% warm, Room = Basement

4. **Test it**:
   - Dashboard: Click "BasementOn" → all lights turn on → map shows all white
   - Dashboard: Click "BasementOn" again → room toggles off → map shows all dark
   - API: `curl .../api/Basement/cycle` → cycles through your three scenes

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
