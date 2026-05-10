from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

from . import config as cfg_mod
from . import sleep as sleep_mod
from .exceptions import LocationError

_proc_lock = threading.Lock()
_active_proc: Optional[subprocess.Popen] = None

# Serializes all set/clear calls so the health monitor can't race with user calls.
_set_lock = threading.Lock()

# State for the health monitor.
_last_coords: Optional[tuple[float, float, str]] = None  # (lat, lng, udid)
_holding = False   # True while we intend to hold a location
_monitor_started = False


def _pid_file() -> Path:
    return cfg_mod.cache_dir() / "location_proc.pid"


def _loc_log() -> Path:
    return cfg_mod.cache_dir() / "location.log"


def _loc_state_file() -> Path:
    return cfg_mod.cache_dir() / "location_state.json"


def get_current_coords() -> tuple[float, float, str] | None:
    """Return (lat, lng, udid) of the active spoofed location, or None.

    Reads in-process state first, then falls back to the on-disk state written
    by set_location so that other CLI invocations can discover the current location.
    """
    if _last_coords is not None:
        return _last_coords
    f = _loc_state_file()
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
        return float(d["lat"]), float(d["lng"]), str(d["udid"])
    except Exception:
        return None


def _save_loc_state(lat: float, lng: float, udid: str) -> None:
    try:
        _loc_state_file().write_text(json.dumps({"lat": lat, "lng": lng, "udid": udid}))
    except Exception:
        pass


def _clear_loc_state() -> None:
    _loc_state_file().unlink(missing_ok=True)


def _log_event(msg: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with _loc_log().open("a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _kill_active() -> None:
    """Kill the running location-simulation process, if any. Caller need not hold any lock."""
    global _active_proc
    with _proc_lock:
        proc = _active_proc
        _active_proc = None

    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass

    # Also clean up any PID from a previous CLI invocation.
    pf = _pid_file()
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        pf.unlink(missing_ok=True)


def _spawn(lat: float, lng: float, udid: str) -> subprocess.Popen:
    """Spawn the simulate-location subprocess. Caller must hold _set_lock."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "pymobiledevice3",
            "developer", "dvt", "simulate-location", "set",
            "--tunnel", udid,
            "--", str(lat), str(lng),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _pid_file().write_text(str(proc.pid))
    with _proc_lock:
        global _active_proc
        _active_proc = proc
    return proc


def release_location_hold() -> None:
    """Stop the persistent CLI holder without sending a clear command."""
    global _holding, _last_coords
    with _set_lock:
        _holding = False
        _last_coords = None
        _kill_active()


class RouteLocationSession:
    """Reuse one DVT location-simulation connection for route playback."""

    def __init__(self, address: str, port: int):
        self.address = address
        self.port = int(port)
        self._rsd: RemoteServiceDiscoveryService | None = None
        self._dvt: DvtProvider | None = None
        self._location: LocationSimulation | None = None

    async def __aenter__(self) -> "RouteLocationSession":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._location is not None:
            return

        # A previous static location subprocess can keep forcing its own
        # coordinate. Route playback owns simulation while this session lives.
        release_location_hold()

        try:
            self._rsd = RemoteServiceDiscoveryService((self.address, self.port))
            await self._rsd.connect()
            self._dvt = DvtProvider(self._rsd)
            await self._dvt.__aenter__()
            self._location = LocationSimulation(self._dvt)
            await self._location.__aenter__()
        except Exception as e:
            await self.close()
            raise LocationError(f"Failed to open route location session: {e}") from e

    async def set(self, lat: float, lng: float) -> None:
        if self._location is None:
            await self.connect()
        try:
            await self._location.set(lat, lng)
        except Exception as e:
            raise LocationError(f"Failed to set route location: {e}") from e

    async def close(self) -> None:
        if self._location is not None:
            with suppress(Exception):
                await self._location.__aexit__(None, None, None)
        self._location = None

        if self._dvt is not None:
            with suppress(Exception):
                await self._dvt.__aexit__(None, None, None)
        self._dvt = None

        if self._rsd is not None:
            with suppress(Exception):
                await self._rsd.close()
        self._rsd = None


def _start_health_monitor() -> None:
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    t = threading.Thread(target=_health_monitor, daemon=True, name="loc-health")
    t.start()


def _health_monitor() -> None:
    """
    Background thread: if the simulate-location subprocess dies while we still
    want to hold a location, restart it. This handles tunnel drops and other
    unexpected exits that would otherwise silently revert the spoofed position.
    """
    last_restart = 0.0
    consecutive_failures = 0

    while True:
        time.sleep(3.0)

        if not _holding or _last_coords is None:
            consecutive_failures = 0
            continue

        # GIL makes this pointer read atomic; no lock needed for a liveness check.
        proc = _active_proc
        if proc is not None and proc.poll() is None:
            consecutive_failures = 0
            continue

        # Process is dead while we wanted to hold a location.
        now = time.monotonic()

        if now - last_restart < 15.0:
            continue  # cooldown: at most one restart every 15 s

        if consecutive_failures >= 5:
            _log_event(
                f"ERROR health monitor: {consecutive_failures} consecutive failures, "
                "stopping auto-restart — call set_location again to retry"
            )
            consecutive_failures = 0  # reset so a fresh user call re-arms it
            continue

        # Don't interfere if a user call is already in progress.
        if not _set_lock.acquire(blocking=False):
            continue

        try:
            # Re-check under the lock in case clear_location just ran.
            if not _holding or _last_coords is None:
                continue

            lat, lng, udid = _last_coords
            exit_code = proc.poll() if proc is not None else "none"
            _log_event(
                f"INFO health monitor: subprocess died (exit={exit_code}), "
                f"restarting at {lat:.6f},{lng:.6f}"
            )

            _kill_active()
            new_proc = _spawn(lat, lng, udid)

            try:
                new_proc.wait(timeout=3)
                if new_proc.returncode == 0:
                    _log_event("INFO health monitor: restart subprocess exited cleanly (rc=0)")
                    consecutive_failures = 0
                else:
                    stderr = b""
                    try:
                        stderr = new_proc.stderr.read() or b""
                    except Exception:
                        pass
                    _log_event(
                        f"WARN health monitor: restart failed (rc={new_proc.returncode}): "
                        f"{stderr.decode(errors='replace').strip()}"
                    )
                    consecutive_failures += 1
            except subprocess.TimeoutExpired:
                _log_event(
                    f"INFO health monitor: subprocess restarted and running (pid={new_proc.pid})"
                )
                consecutive_failures = 0

            last_restart = time.monotonic()

        except Exception as e:
            _log_event(f"WARN health monitor: exception during restart: {e}")
            consecutive_failures += 1
        finally:
            _set_lock.release()


def set_location(lat: float, lng: float, udid: str) -> None:
    global _holding, _last_coords
    _start_health_monitor()

    with _set_lock:
        _holding = True
        _last_coords = (lat, lng, udid)
        _save_loc_state(lat, lng, udid)
        sleep_mod.acquire()

        # Capture old proc and any orphaned PID from a previous CLI session
        # BEFORE _spawn() overwrites the PID file.
        with _proc_lock:
            old_proc = _active_proc
        pf = _pid_file()
        stale_pid: int | None = None
        if pf.exists():
            try:
                stale_pid = int(pf.read_text().strip())
                if old_proc is not None and stale_pid == old_proc.pid:
                    stale_pid = None  # same process; handled below
            except (ValueError, OSError):
                stale_pid = None

        # Spawn the new process BEFORE killing the old one so the device
        # has no gap where it reverts to its real GPS position.
        proc = _spawn(lat, lng, udid)

        # Kill the old in-process subprocess now that the new one is starting.
        if old_proc is not None and old_proc.poll() is None:
            try:
                old_proc.terminate()
                old_proc.wait(timeout=5)
            except Exception:
                pass

        # Kill any leftover process from a previous CLI invocation.
        if stale_pid is not None:
            try:
                os.kill(stale_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return  # Still running — location is being held.

        if proc.returncode == 0:
            proc.stderr.close()
            _log_event(f"INFO set_location: subprocess exited cleanly (rc=0) for {lat:.6f},{lng:.6f}")
            return  # Exited cleanly; location was set.

        stderr = b""
        try:
            stderr = proc.stderr.read() or b""
        except Exception:
            pass
        msg = stderr.decode(errors="replace").strip() or "command exited unexpectedly"
        _log_event(f"ERROR set_location: rc={proc.returncode}: {msg}")
        raise LocationError(f"Failed to set location: {msg}")


def clear_location(udid: str) -> None:
    global _holding, _last_coords
    with _set_lock:
        _holding = False
        _last_coords = None
        _clear_loc_state()
        sleep_mod.release()
        _kill_active()

    try:
        subprocess.run(
            [
                sys.executable, "-m", "pymobiledevice3",
                "developer", "dvt", "simulate-location", "clear",
                "--tunnel", udid,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        raise LocationError(f"Failed to clear location: {stderr or e}") from e
    except subprocess.TimeoutExpired as e:
        raise LocationError("Location clear command timed out") from e
