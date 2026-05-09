# spoofloc

iOS location spoofing CLI with a web map UI, built for developer testing on physical iPhones.

## Requirements

- **macOS** (tested on macOS 14+)
- **Python 3.11+**
- **iPhone with Developer Mode enabled** (Settings → Privacy & Security → Developer Mode)
- Both Mac and iPhone on the **same WiFi network** (no client isolation)

## Install

```bash
cd /path/to/spoofloc
pip install -e .
```

## One-time setup (USB required once)

1. Plug your iPhone into your Mac via USB
2. Trust the Mac on your iPhone if prompted
3. Run the setup wizard:

```bash
spoofloc setup
```

This enables WiFi connections on your device and saves your UDID. **After this, USB is not required.**

## Daily usage

### Start the tunnel (required for all spoofing)

```bash
spoofloc tunnel start
```

This launches `tunneld` as a background daemon with sudo. Your password will be prompted once.
When a default device is configured, spoofloc also asks `tunneld` to actively create a tunnel for that UDID instead of waiting only for passive discovery.

```bash
spoofloc tunnel status    # check device connection
spoofloc tunnel stop      # stop the daemon
spoofloc setup            # reconnect USB and refresh WiFi trust if pairing stalls
spoofloc tunnel pair      # manual RemoteXPC pairing for devices that advertise it
```

### Set a static location

```bash
# By coordinates
spoofloc location set 37.7749 -122.4194

# By address (uses OpenStreetMap geocoding, no API key needed)
spoofloc location set --address "Eiffel Tower, Paris"

# Restore real GPS
spoofloc location clear
```

> **Note:** The spoofed location persists until you run `clear` or reboot your iPhone. It survives process exit.

### Simulate a route from a GPX file

```bash
spoofloc route run trip.gpx
spoofloc route run trip.gpx --speed 50   # 50 mph
spoofloc route run trip.gpx --loop       # repeat forever
cat trip.gpx | spoofloc route run -      # from stdin
```

Press `Ctrl+C` to stop the route.

### Simulate continuous motion patterns

```bash
# Wander randomly within a radius around a point
spoofloc motion walk 37.7749 -122.4194 --radius 300 --speed 3

# Circle a point at a fixed radius
spoofloc motion orbit 37.7749 -122.4194 --radius 200 --speed 20

# Bounce back and forth between two coordinates
spoofloc motion oscillate 37.77 -122.41 37.78 -122.42 --speed 30

# Drift in a slowly-changing random direction
spoofloc motion drift 37.7749 -122.4194 --speed 3
```

Omit coordinates to start from the current spoofed location. All speeds are in mph. Press `Ctrl+C` to stop.

### Map UI (visual location picker)

```bash
spoofloc map
```

Opens a browser with a MapLibre GL map (OpenFreeMap tiles — no API key required):
- **Click** anywhere to set your iPhone's location instantly
- **Search** by address using the search bar
- **Route mode**: click waypoints on the map → set speed → Play Route, or use A→B road-snapped routing via OSRM
- **Motion mode**: run walk/orbit/oscillate/drift patterns directly from the UI
- SSE-driven status bar shows tunnel state and live coordinates

## Configuration

Config is stored at `~/Library/Application Support/spoofloc/config.toml`.

```bash
spoofloc config show                         # view all settings
spoofloc config set route.default_speed_mph 60
spoofloc config set web.port 5000
spoofloc config set tunnel.mode start-tunnel # for iOS 17.0–17.3.1
spoofloc config path                         # show config file path
spoofloc config reset                        # restore defaults
```

### Key settings

| Key | Default | Description |
|-----|---------|-------------|
| `device.default_udid` | `""` | UDID to use (auto-set by `setup`) |
| `tunnel.mode` | `"tunneld"` | `"tunneld"` for iOS 17.4+, `"start-tunnel"` for earlier |
| `tunnel.startup_timeout_s` | `90` | Seconds to wait for WiFi tunnel discovery/startup |
| `tunnel.preferred_connection_type` | `"auto"` | Connection type requested from `tunneld` during startup |
| `route.default_speed_mph` | `30.0` | Route playback speed in mph |
| `route.tick_hz` | `2.0` | Location updates per second during routes |
| `route.loop` | `false` | Loop routes by default |
| `web.port` | `4780` | Map UI port |
| `web.auto_open_browser` | `true` | Auto-open browser with `spoofloc map` |

### Named locations (favorites)

Add named locations to your config file manually:

```toml
[favorites]
home = { lat = 37.3318, lng = -122.0312, label = "Home" }
office = { lat = 37.7749, lng = -122.4194, label = "SF Office" }
```

## How it works

spoofloc uses [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) to communicate with your iPhone over WiFi after an initial USB pair. The `tunneld` daemon creates an encrypted tunnel to the device's developer services. Location spoofing goes through Apple's DVT (Developer Tools) framework, the same mechanism Xcode uses for GPX simulation.

**No jailbreak required.** Developer Mode must be enabled on the device.

## Troubleshooting

**"tunneld not reachable"**  
Run `spoofloc tunnel start`. If it fails, check the log: `spoofloc tunnel status`

**"No devices found"**  
- Ensure your iPhone and Mac are on the same WiFi network
- Unlock your iPhone (Bonjour discovery fails when locked on some iOS versions)
- After unplugging USB, `python3 -m pymobiledevice3 usbmux list --network --simple` should show your UDID. If it prints `[]`, macOS cannot currently see the phone over WiFi; check WiFi, disable client/AP isolation, wake/unlock the phone, then retry `spoofloc tunnel status`
- If the error says `task not created` or `PAIRED: False`, reconnect USB, trust the Mac if prompted, run `spoofloc setup`, then `spoofloc tunnel restart`
- Try running `spoofloc setup` again with USB connected

**"Failed to set location"**  
Ensure Developer Mode is enabled on iPhone and tunneld shows the device as ready.

**iOS 17.0–17.3.1**  
Change the tunnel mode: `spoofloc config set tunnel.mode start-tunnel`

## Running tests

```bash
pip install -e ".[dev]"
pytest
```
