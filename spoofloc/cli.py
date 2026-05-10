from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import config as cfg_mod
from . import location as location_mod
from .device import (
    device_name,
    device_udid,
    enable_wifi_connections,
    get_ios_version,
    list_paired_devices,
    pair_remote_device,
    prefer_usb_device,
)
from .exceptions import (
    DeviceNotFoundError,
    GeocodeError,
    LocationError,
    RouteError,
    TunnelDownError,
)
from .geocode import CachedGeocoder
from .location import RouteLocationSession, clear_location, set_location
from . import sleep as sleep_mod
from .motion import MotionPlayer
from .route import RoutePlayer, load_gpx
from .tunnel import TunnelManager, TunnelState

console = Console()
_tunnel = TunnelManager()


def _tunnel_startup_timeout() -> float:
    return float(cfg_mod.load()["tunnel"].get("startup_timeout_s", 90))


def _wait_for_tunnel_device() -> None:
    timeout = _tunnel_startup_timeout()
    console.print(f"  Waiting for device (up to {timeout:.0f}s)…")
    try:
        found_udid, addr, port = _tunnel.wait_for_device(timeout=timeout)
        console.print(f"[green]✓ Device ready ({found_udid[:8]}…) at {escape(addr)}:{port}[/green]")
    except DeviceNotFoundError as e:
        console.print(f"[yellow]⚠ {e}[/yellow]")
        console.print(f"[dim]  Log: {_tunnel.log_path()}[/dim]")


def _get_udid(udid: Optional[str]) -> str:
    return _get_device_rsd(udid)[0]


def _get_device_rsd(udid: Optional[str]) -> tuple[str, str, int]:
    if udid is None:
        udid = cfg_mod.load()["device"]["default_udid"] or None
    try:
        return _tunnel.get_device_rsd(udid)
    except TunnelDownError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)
    except DeviceNotFoundError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="spoofloc")
def main():
    """spoofloc — iOS location spoofing for developer testing."""


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

@main.command()
def setup():
    """One-time USB setup: pair device and enable WiFi connections."""
    console.print("[bold]spoofloc setup[/bold]\n")
    console.print("Step 1: Connect your iPhone via USB cable.")
    console.print("Step 2: On iPhone → Settings → Privacy & Security → Developer Mode → Enable.")
    click.pause("        Press Enter when Developer Mode is enabled…")

    console.print("\nDetecting paired devices…")
    devices = list_paired_devices()
    if not devices:
        console.print("[red]No devices found. Ensure the iPhone is connected and trusted.[/red]")
        sys.exit(1)

    device = prefer_usb_device(devices)
    udid = device_udid(device)
    if not udid:
        console.print(f"[red]No UDID found in device listing: {device}[/red]")
        sys.exit(1)
    name = device_name(device)
    console.print(f"[green]Found:[/green] {name} ({udid[:8]}…)")

    console.print("\nEnabling WiFi connections…")
    try:
        enable_wifi_connections(udid)
        console.print("[green]✓ WiFi connections enabled.[/green]")
    except Exception as e:
        console.print(f"[red]✗ Failed: {e}[/red]")
        sys.exit(1)

    cfg = cfg_mod.load()
    cfg["device"]["default_udid"] = udid
    cfg.setdefault("tunnel", {})["preferred_connection_type"] = "auto"
    cfg_mod.save(cfg)
    console.print(f"[green]✓ Saved {udid[:8]}… as default device.[/green]")

    console.print("\n[bold]Setup complete![/bold]")
    console.print("You can now unplug and run: [cyan]spoofloc tunnel start[/cyan]")
    console.print("(Ensure your Mac and iPhone are on the same WiFi network.)")


# ---------------------------------------------------------------------------
# tunnel group
# ---------------------------------------------------------------------------

@main.group()
def tunnel():
    """Manage the tunneld daemon (required for all spoofing)."""


@tunnel.command("start")
@click.option("--no-daemonize", is_flag=True, default=False, help="Run in foreground (blocks).")
def tunnel_start(no_daemonize: bool):
    """Start the tunneld background daemon (requires sudo)."""
    console.print("[bold]Starting tunneld…[/bold]")
    console.print("[dim]This requires sudo. You will be prompted for your password.[/dim]\n")
    try:
        newly_started = _tunnel.start_daemon(daemonize=not no_daemonize)
        if newly_started:
            if not no_daemonize:
                console.print("[green]✓ tunneld started in background.[/green]")
                console.print(f"  Log: {_tunnel.log_path()}")
                _wait_for_tunnel_device()
        else:
            console.print("[yellow]tunneld is already running.[/yellow]")
            if _tunnel.state() == TunnelState.DEVICE_READY:
                _print_tunnel_status()
            else:
                _wait_for_tunnel_device()
    except Exception as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)


@tunnel.command("stop")
def tunnel_stop():
    """Stop the tunneld daemon."""
    console.print("Stopping tunneld…")
    _tunnel.stop_daemon()
    console.print("[green]✓ Stopped.[/green]")


@tunnel.command("status")
def tunnel_status():
    """Show tunneld and device status."""
    _print_tunnel_status()


@tunnel.command("pair")
@click.option("--name", default=None, help="Device name to match during remote pairing.")
def tunnel_pair(name: Optional[str]):
    """Pair the iPhone for WiFi RemoteXPC tunneling."""
    console.print("[bold]Pairing for WiFi tunneling…[/bold]")
    console.print("[dim]Keep the iPhone unlocked and accept the pairing prompt if one appears.[/dim]\n")
    try:
        pair_remote_device(name)
        console.print("[green]✓ Remote tunnel pairing complete.[/green]")
    except Exception as e:
        console.print(f"[red]✗ Failed: {e}[/red]")
        sys.exit(1)


@tunnel.command("restart")
def tunnel_restart():
    """Stop and restart tunneld."""
    console.print("Restarting tunneld…")
    _tunnel.stop_daemon()
    time.sleep(1)
    _tunnel.start_daemon(daemonize=True)
    console.print("[green]✓ Restarted.[/green]")
    _wait_for_tunnel_device()


def _print_tunnel_status():
    state = _tunnel.state()
    reconnect_error: Optional[str] = None
    if state == TunnelState.UP_NO_DEVICE and cfg_mod.load()["device"].get("default_udid"):
        try:
            _tunnel.get_device_rsd(timeout=float(cfg_mod.load()["tunnel"].get("reconnect_timeout_s", 10)))
            state = _tunnel.state()
        except DeviceNotFoundError as e:
            reconnect_error = str(e)
            state = _tunnel.state()
        except TunnelDownError as e:
            reconnect_error = str(e)
            state = _tunnel.state()
    color = {"DEVICE_READY": "green", "UP_NO_DEVICE": "yellow", "DOWN": "red"}.get(
        state.name, "white"
    )
    console.print(f"Tunnel: [{color}]{state.name}[/{color}]")

    try:
        data = _tunnel.probe()
        if data:
            tbl = Table(show_header=True, header_style="bold dim")
            tbl.add_column("UDID", style="dim", max_width=20)
            tbl.add_column("Tunnel Address")
            tbl.add_column("Port")
            for udid, entries in data.items():
                for entry in entries:
                    tbl.add_row(
                        udid[:16] + "…",
                        entry["tunnel-address"],
                        str(entry["tunnel-port"]),
                    )
            console.print(tbl)
        else:
            console.print("[yellow]No devices connected.[/yellow]")
            if reconnect_error:
                console.print(f"[dim]Active start: {reconnect_error}[/dim]")
                console.print(
                    "[dim]If USB is unplugged, macOS is not seeing the phone over WiFi. "
                    "Keep the iPhone unlocked, verify both devices are on the same network, "
                    "and check `python3 -m pymobiledevice3 usbmux list --network --simple`.[/dim]"
                )
    except TunnelDownError:
        console.print("[red]tunneld is not running.[/red]")
        console.print("  Start it with: [cyan]spoofloc tunnel start[/cyan]")


# ---------------------------------------------------------------------------
# location group
# ---------------------------------------------------------------------------

@main.group()
def location():
    """Set or clear spoofed GPS location."""


@location.command("set")
@click.argument("lat", type=float, required=False)
@click.argument("lng", type=float, required=False)
@click.option("--address", "-a", default=None, help="Address or place name to geocode.")
@click.option("--udid", default=None, help="Device UDID (default: first available).")
def location_set(
    lat: Optional[float],
    lng: Optional[float],
    address: Optional[str],
    udid: Optional[str],
):
    """Set spoofed location by coordinates or address.

    Examples:

    \b
      spoofloc location set 37.7749 -122.4194
      spoofloc location set --address "Eiffel Tower, Paris"
    """
    if address:
        console.print(f"Geocoding: {address!r}…")
        try:
            geocoder = CachedGeocoder()
            lat, lng, display = geocoder.geocode(address)
            console.print(f"  → {display}")
            console.print(f"  → {lat:.6f}, {lng:.6f}")
        except GeocodeError as e:
            console.print(f"[red]✗ {e}[/red]")
            sys.exit(1)
    elif lat is None or lng is None:
        console.print("[red]✗ Provide LAT LNG or --address[/red]")
        sys.exit(1)

    device_udid = _get_udid(udid)
    console.print(f"Setting location → {lat:.6f}, {lng:.6f}…")
    try:
        set_location(lat, lng, device_udid)
        console.print("[green]✓ Location set.[/green]")
        console.print("[dim]Run 'spoofloc location clear' to restore real GPS.[/dim]")
        console.print("[dim]Sleep prevention active.[/dim]")
    except LocationError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)


@location.command("clear")
@click.option("--udid", default=None, help="Device UDID.")
def location_clear(udid: Optional[str]):
    """Clear location spoof and restore real GPS."""
    device_udid = _get_udid(udid)
    console.print("Clearing location spoof…")
    try:
        clear_location(device_udid)
        console.print("[green]✓ Location cleared. iPhone is using real GPS.[/green]")
        console.print("[dim]Sleep prevention released.[/dim]")
    except LocationError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# route group
# ---------------------------------------------------------------------------

@main.group()
def route():
    """Simulate movement along a route."""


@route.command("run")
@click.argument("file", default="-", metavar="FILE")
@click.option("--speed", "speed_mph", type=float, default=None, help="Speed in mph.")
@click.option("--loop/--no-loop", default=None, help="Repeat route indefinitely.")
@click.option("--udid", default=None, help="Device UDID.")
def route_run(
    file: str,
    speed_mph: Optional[float],
    loop: Optional[bool],
    udid: Optional[str],
):
    """Simulate movement along a GPX route file (use - for stdin).

    \b
      spoofloc route run trip.gpx
      spoofloc route run trip.gpx --speed 80 --loop
      cat trip.gpx | spoofloc route run -
    """
    cfg = cfg_mod.load()
    if speed_mph is None:
        speed_mph = cfg["route"]["default_speed_mph"]
    if loop is None:
        loop = cfg["route"]["loop"]

    console.print(f"Loading GPX: {file if file != '-' else 'stdin'}…")
    try:
        waypoints = load_gpx(file)
    except RouteError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]✗ Failed to load file: {e}[/red]")
        sys.exit(1)

    console.print(f"  {len(waypoints)} waypoints loaded")
    device_udid, tunnel_address, tunnel_port = _get_device_rsd(udid)

    async def run():
        player = RoutePlayer()
        last_coords: tuple[float, float] | None = None

        async def on_tick(lat: float, lng: float) -> None:
            nonlocal last_coords
            await route_location.set(lat, lng)
            last_coords = (lat, lng)

        try:
            async with RouteLocationSession(tunnel_address, tunnel_port) as route_location:
                console.print(
                    f"[green]Starting route[/green] at {speed_mph:.0f} mph"
                    + (" (loop)" if loop else "")
                )
                console.print("[dim]Press Ctrl+C to stop.[/dim]\n")
                await player.play(waypoints, speed_mph, loop, on_tick)
        except asyncio.CancelledError:
            pass
        finally:
            if last_coords is not None:
                loop_obj = asyncio.get_running_loop()
                await loop_obj.run_in_executor(
                    None,
                    set_location,
                    last_coords[0],
                    last_coords[1],
                    device_udid,
                )

    try:
        asyncio.run(run())
        console.print("\n[green]✓ Route complete.[/green]")
    except LocationError as e:
        console.print(f"\n[red]✗ {e}[/red]")
        sys.exit(1)
    except RouteError as e:
        console.print(f"\n[red]✗ {e}[/red]")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


# ---------------------------------------------------------------------------
# motion group
# ---------------------------------------------------------------------------

@main.group()
def motion():
    """Simulate continuous movement patterns (walk, orbit, oscillate, drift)."""


def _resolve_center(lat: Optional[float], lng: Optional[float]) -> tuple[float, float]:
    """Return center coords, falling back to current spoofed location."""
    if lat is not None and lng is not None:
        return lat, lng
    coords = location_mod.get_current_coords()
    if coords is None:
        console.print("[red]✗ No current spoofed location. Provide LAT LNG or run 'spoofloc location set' first.[/red]")
        sys.exit(1)
    return coords[0], coords[1]


def _run_motion(
    motion_coro_factory,
    udid: Optional[str],
    label: str,
) -> None:
    """Connect to device, run a motion pattern coroutine, hold final position."""
    device_udid, tunnel_address, tunnel_port = _get_device_rsd(udid)

    async def run() -> None:
        last_coords: tuple[float, float] | None = None

        async def on_tick(lat: float, lng: float) -> None:
            nonlocal last_coords
            await motion_loc.set(lat, lng)
            last_coords = (lat, lng)

        try:
            async with RouteLocationSession(tunnel_address, tunnel_port) as motion_loc:
                console.print(f"[green]Starting {label}[/green]")
                console.print("[dim]Press Ctrl+C to stop.[/dim]\n")
                await motion_coro_factory(on_tick)
        except asyncio.CancelledError:
            pass
        finally:
            if last_coords is not None:
                lo = asyncio.get_running_loop()
                await lo.run_in_executor(
                    None, set_location, last_coords[0], last_coords[1], device_udid
                )

    try:
        asyncio.run(run())
        console.print("\n[green]✓ Stopped.[/green]")
    except LocationError as e:
        console.print(f"\n[red]✗ {e}[/red]")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


@motion.command("walk")
@click.argument("lat", type=float, required=False)
@click.argument("lng", type=float, required=False)
@click.option("--radius", "radius_m", type=float, default=200.0, show_default=True,
              help="Wander radius in metres.")
@click.option("--speed", "speed_mph", type=float, default=3.0, show_default=True,
              help="Walking speed in mph.")
@click.option("--jitter", type=float, default=0.5, show_default=True,
              help="Directional randomness 0–1 (0=direct, 1=chaotic).")
@click.option("--udid", default=None, help="Device UDID.")
def motion_walk(
    lat: Optional[float],
    lng: Optional[float],
    radius_m: float,
    speed_mph: float,
    jitter: float,
    udid: Optional[str],
):
    """Random walk around a center point.

    \b
      spoofloc motion walk                          # use current location
      spoofloc motion walk 37.7749 -122.4194 --radius 500 --speed 4
    """
    center_lat, center_lng = _resolve_center(lat, lng)
    console.print(f"Center: {center_lat:.5f}, {center_lng:.5f} | radius {radius_m:.0f}m | {speed_mph} mph | jitter {jitter}")
    player = MotionPlayer()
    _run_motion(
        lambda on_tick: player.run_walk(center_lat, center_lng, radius_m, speed_mph, jitter, on_tick),
        udid,
        "random walk",
    )


@motion.command("orbit")
@click.argument("lat", type=float, required=False)
@click.argument("lng", type=float, required=False)
@click.option("--radius", "radius_m", type=float, default=200.0, show_default=True,
              help="Orbit radius in metres.")
@click.option("--speed", "speed_mph", type=float, default=20.0, show_default=True,
              help="Orbital speed in mph.")
@click.option("--udid", default=None, help="Device UDID.")
def motion_orbit(
    lat: Optional[float],
    lng: Optional[float],
    radius_m: float,
    speed_mph: float,
    udid: Optional[str],
):
    """Orbit a center point in a circle.

    \b
      spoofloc motion orbit                         # use current location
      spoofloc motion orbit 37.7749 -122.4194 --radius 300 --speed 40
    """
    center_lat, center_lng = _resolve_center(lat, lng)
    circumference = 2 * 3.14159 * radius_m
    lap_s = circumference / (speed_mph * 0.44704)
    console.print(
        f"Center: {center_lat:.5f}, {center_lng:.5f} | radius {radius_m:.0f}m | "
        f"{speed_mph} mph (~{lap_s:.0f}s per lap)"
    )
    player = MotionPlayer()
    _run_motion(
        lambda on_tick: player.run_orbit(center_lat, center_lng, radius_m, speed_mph, on_tick),
        udid,
        "orbit",
    )


@motion.command("oscillate")
@click.argument("lat1", type=float)
@click.argument("lng1", type=float)
@click.argument("lat2", type=float)
@click.argument("lng2", type=float)
@click.option("--speed", "speed_mph", type=float, default=30.0, show_default=True,
              help="Speed in mph.")
@click.option("--udid", default=None, help="Device UDID.")
def motion_oscillate(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
    speed_mph: float,
    udid: Optional[str],
):
    """Ping-pong between two points.

    \b
      spoofloc motion oscillate 37.77 -122.41 37.78 -122.42
      spoofloc motion oscillate 37.77 -122.41 37.78 -122.42 --speed 80
    """
    console.print(
        f"A: {lat1:.5f}, {lng1:.5f} ↔ B: {lat2:.5f}, {lng2:.5f} | {speed_mph} mph"
    )
    player = MotionPlayer()
    _run_motion(
        lambda on_tick: player.run_oscillate(lat1, lng1, lat2, lng2, speed_mph, on_tick),
        udid,
        "oscillate",
    )


@motion.command("drift")
@click.argument("lat", type=float, required=False)
@click.argument("lng", type=float, required=False)
@click.option("--speed", "speed_mph", type=float, default=1.5, show_default=True,
              help="Drift speed in mph.")
@click.option("--udid", default=None, help="Device UDID.")
def motion_drift(
    lat: Optional[float],
    lng: Optional[float],
    speed_mph: float,
    udid: Optional[str],
):
    """Slowly creep from a point in a wandering direction.

    \b
      spoofloc motion drift                         # start from current location
      spoofloc motion drift 37.7749 -122.4194 --speed 3
    """
    start_lat, start_lng = _resolve_center(lat, lng)
    console.print(f"Start: {start_lat:.5f}, {start_lng:.5f} | {speed_mph} mph")
    player = MotionPlayer()
    _run_motion(
        lambda on_tick: player.run_drift(start_lat, start_lng, speed_mph, on_tick),
        udid,
        "drift",
    )


# ---------------------------------------------------------------------------
# map command
# ---------------------------------------------------------------------------

@main.command("map")
@click.option("--host", default=None, help="Bind host (default from config).")
@click.option("--port", type=int, default=None, help="Port (default from config).")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser.")
@click.option("--udid", default=None, help="Device UDID.")
def map_cmd(
    host: Optional[str],
    port: Optional[int],
    no_browser: bool,
    udid: Optional[str],
):
    """Launch the map UI in your browser."""
    from .web.server import run_server

    cfg = cfg_mod.load()
    host = host or cfg["web"]["host"]
    port = port or cfg["web"]["port"]
    open_browser = (not no_browser) and cfg["web"]["auto_open_browser"]

    console.print(f"[bold]spoofloc map UI[/bold]  http://{host}:{port}")
    state = _tunnel.state()
    if state == TunnelState.DOWN:
        console.print("[yellow] tunneld is not running. Start it with: spoofloc tunnel start[/yellow]")
    elif state == TunnelState.UP_NO_DEVICE:
        console.print("[yellow]tunneld running but no device found.[/yellow]")
    else:
        console.print("[green]Device tunnel ready.[/green]")

    sleep_mod.acquire()
    console.print("[dim]Sleep prevention active while map server is running.[/dim]")
    try:
        run_server(host=host, port=port, udid=udid, open_browser=open_browser)
    finally:
        sleep_mod.release()
        console.print("[dim]Sleep prevention released.[/dim]")


# ---------------------------------------------------------------------------
# config group
# ---------------------------------------------------------------------------

@main.group()
def config():
    """Manage spoofloc configuration."""


@config.command("show")
def config_show():
    """Print current effective configuration."""
    import tomli_w
    cfg = cfg_mod.load()
    console.print(tomli_w.dumps(cfg))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set_cmd(key: str, value: str):
    """Set a config value using dot-notation key.

    \b
      spoofloc config set route.default_speed_mph 50
      spoofloc config set web.port 5000
      spoofloc config set tunnel.mode start-tunnel
    """
    try:
        cfg_mod.set_key(key, value)
        console.print(f"[green]✓ {key} = {value}[/green]")
    except (KeyError, ValueError) as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)


@config.command("reset")
def config_reset():
    """Reset configuration to defaults."""
    click.confirm("Reset all config to defaults?", abort=True)
    cfg_mod.save(cfg_mod.DEFAULTS)
    console.print("[green]✓ Config reset.[/green]")


@config.command("path")
def config_path_cmd():
    """Print path to the config file."""
    console.print(str(cfg_mod.config_path()))
