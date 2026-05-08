from __future__ import annotations

import shlex
import subprocess
import time
from enum import Enum, auto
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx

from . import config as cfg_mod
from .exceptions import DeviceNotFoundError, TunnelDownError


class TunnelState(Enum):
    UNKNOWN = auto()
    DOWN = auto()
    UP_NO_DEVICE = auto()
    DEVICE_READY = auto()


class TunnelManager:
    def __init__(self):
        self._pid_file: Path = cfg_mod.cache_dir() / "tunneld.pid"
        self._log_file: Path = cfg_mod.cache_dir() / "tunneld.log"
        self._last_start_error: Optional[str] = None

    def _daemon_url(self) -> str:
        return cfg_mod.load()["tunnel"]["daemon_url"]

    def probe(self) -> dict:
        try:
            r = httpx.get(self._daemon_url(), timeout=2.0)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise TunnelDownError(f"tunneld not reachable: {e}") from e

    def state(self) -> TunnelState:
        try:
            data = self.probe()
        except TunnelDownError:
            return TunnelState.DOWN
        if not data:
            return TunnelState.UP_NO_DEVICE
        for entries in data.values():
            if entries:
                return TunnelState.DEVICE_READY
        return TunnelState.UP_NO_DEVICE

    def get_device_rsd(
        self,
        udid: Optional[str] = None,
        *,
        auto_start: bool = True,
        timeout: Optional[float] = None,
    ) -> tuple[str, str, int]:
        """Return (udid, tunnel-address, tunnel-port) for the requested device."""
        cfg = cfg_mod.load()
        cfg_udid = cfg["device"].get("default_udid") or None
        target_udid = udid or cfg_udid
        try:
            data = self.probe()
        except TunnelDownError as e:
            raise TunnelDownError(
                "tunneld is not running. Start it with: spoofloc tunnel start"
            ) from e

        resolved = self._select_rsd_from_probe(data, udid, cfg_udid, allow_fallback=not target_udid)
        if resolved is not None:
            return resolved

        if auto_start and target_udid:
            requested = self.request_configured_device_tunnel(target_udid, timeout=timeout)
            if requested is not None:
                return requested
            try:
                data = self.probe()
            except TunnelDownError:
                data = {}
            resolved = self._select_rsd_from_probe(data, target_udid, cfg_udid, allow_fallback=False)
            if resolved is not None:
                return resolved

        if not udid and cfg_udid:
            fallback = self._select_rsd_from_probe(data, None, None, allow_fallback=True)
            if fallback is not None:
                return fallback

        if not data:
            raise DeviceNotFoundError(
                "No devices found in tunnel. Is your iPhone unlocked, on the same WiFi network, "
                "and enabled with `spoofloc setup`?"
            )

        if target_udid:
            available = list(data.keys())
            message = f"Device {target_udid} not in tunnel. Available UDIDs: {available}"
        else:
            message = "No devices found in tunnel. Is your iPhone connected and Developer Mode enabled?"
        if self._last_start_error:
            message += f" Last tunnel start attempt: {self._last_start_error}."
        raise DeviceNotFoundError(message)

    def _select_rsd_from_probe(
        self,
        data: dict,
        udid: Optional[str],
        cfg_udid: Optional[str],
        *,
        allow_fallback: bool,
    ) -> Optional[tuple[str, str, int]]:
        if not data:
            return None
        if udid:
            entries = data.get(udid) or []
            if entries:
                return self._format_rsd(udid, entries[0])
            return None
        if cfg_udid:
            entries = data.get(cfg_udid) or []
            if entries:
                return self._format_rsd(cfg_udid, entries[0])
            return None
        if allow_fallback:
            for found_udid, entries in data.items():
                if entries:
                    return self._format_rsd(found_udid, entries[0])
        return None

    def _format_rsd(self, udid: str, entry: dict) -> tuple[str, str, int]:
        return udid, entry["tunnel-address"], int(entry["tunnel-port"])

    def get_device_udid(self, udid: Optional[str] = None) -> str:
        resolved_udid, _, _ = self.get_device_rsd(udid)
        return resolved_udid

    def request_device_tunnel(
        self,
        udid: str,
        connection_type: Optional[str] = None,
        timeout: float = 15.0,
    ) -> Optional[tuple[str, str, int]]:
        """Ask tunneld to actively create a tunnel for a known device UDID."""
        params = {"udid": udid}
        normalized_connection_type = self._normalize_connection_type(connection_type)
        if normalized_connection_type:
            params["connection_type"] = normalized_connection_type

        url = urljoin(self._daemon_url().rstrip("/") + "/", "start-tunnel")
        try:
            r = httpx.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException:
            label = normalized_connection_type or "auto"
            self._last_start_error = (
                f"tunneld did not answer /start-tunnel for {label} within {timeout:.0f}s"
            )
            return None
        except Exception as e:
            raise TunnelDownError(f"tunneld not reachable: {e}") from e

        if r.status_code != 200:
            self._last_start_error = self._format_start_error(r)
            return None

        data = r.json()
        address = data.get("address")
        port = data.get("port")
        if not address or port is None:
            self._last_start_error = f"tunneld returned an incomplete /start-tunnel response: {data}"
            return None
        self._last_start_error = None
        return udid, address, int(port)

    def request_configured_device_tunnel(
        self,
        udid: str,
        timeout: Optional[float] = None,
    ) -> Optional[tuple[str, str, int]]:
        cfg = cfg_mod.load()
        timeout = float(timeout or cfg["tunnel"].get("reconnect_timeout_s", 10))
        deadline = time.monotonic() + timeout
        connection_types = self._connection_attempt_order(
            cfg["tunnel"].get("preferred_connection_type", "auto")
        )

        for connection_type in connection_types:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                requested = self.request_device_tunnel(
                    udid,
                    connection_type=connection_type,
                    timeout=min(10.0, remaining),
                )
            except TunnelDownError:
                raise
            except DeviceNotFoundError:
                requested = None
            if requested is not None:
                return requested
        return None

    def _normalize_connection_type(self, connection_type: Optional[str]) -> Optional[str]:
        if connection_type is None:
            return None
        value = str(connection_type).strip().lower()
        if value in ("", "auto", "any"):
            return None
        if value == "network":
            return "usbmux"
        return value

    def _connection_attempt_order(self, preferred_connection_type: Optional[str]) -> list[Optional[str]]:
        preferred = self._normalize_connection_type(preferred_connection_type)
        attempts: list[Optional[str]] = [preferred]
        if preferred is not None:
            attempts.append(None)
        return attempts

    def _format_start_error(self, response: httpx.Response) -> str:
        try:
            data = response.json()
        except Exception:
            return f"tunneld /start-tunnel returned HTTP {response.status_code}: {response.text}"

        error = data.get("error")
        if isinstance(error, dict):
            exception = error.get("exception")
            if exception:
                return f"tunneld /start-tunnel returned HTTP {response.status_code}: {exception}"
        if error:
            return f"tunneld /start-tunnel returned HTTP {response.status_code}: {error}"
        return f"tunneld /start-tunnel returned HTTP {response.status_code}: {data}"

    def last_start_error(self) -> Optional[str]:
        return self._last_start_error

    def start_daemon(self, daemonize: bool = True) -> bool:
        """Start tunneld daemon. Returns True if newly started, False if already running."""
        try:
            self.probe()
            return False  # already running
        except TunnelDownError:
            pass

        cfg = cfg_mod.load()
        mode = cfg["tunnel"]["mode"]
        if mode == "start-tunnel":
            tunneld_cmd = ["python3", "-m", "pymobiledevice3", "lockdown", "start-tunnel"]
        else:
            tunneld_cmd = ["python3", "-m", "pymobiledevice3", "remote", "tunneld"]

        if daemonize:
            # "sudo sh -c 'nohup ... >> log 2>&1 & echo $!'"
            # The shell inherits the terminal so sudo can prompt for password,
            # then nohup + & backgrounds tunneld immune to SIGHUP. echo $! gives us the PID.
            log_path = shlex.quote(str(self._log_file))
            inner = " ".join(shlex.quote(c) for c in tunneld_cmd)
            shell_cmd = f"nohup {inner} >> {log_path} 2>&1 & echo $!"
            result = subprocess.run(
                ["sudo", "sh", "-c", shell_cmd],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            )
            pid = result.stdout.strip()
            if pid.isdigit():
                self._pid_file.write_text(pid)
        else:
            subprocess.run(["sudo"] + tunneld_cmd, check=True)

        return True

    def stop_daemon(self) -> None:
        if self._pid_file.exists():
            pid_text = self._pid_file.read_text().strip()
            if pid_text:
                subprocess.run(
                    ["sudo", "kill", pid_text],
                    check=False,
                    capture_output=True,
                )
            self._pid_file.unlink(missing_ok=True)
        # Kill any stray tunneld/start-tunnel processes
        subprocess.run(
            ["sudo", "pkill", "-f", "pymobiledevice3.*tunneld"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "pkill", "-f", "pymobiledevice3.*start-tunnel"],
            check=False,
            capture_output=True,
        )

    def wait_for_device(
        self, udid: Optional[str] = None, timeout: float = 30.0
    ) -> tuple[str, str, int]:
        deadline = time.monotonic() + timeout
        cfg = cfg_mod.load()
        target_udid = udid or cfg["device"].get("default_udid") or None
        next_active_attempt = 0.0

        while time.monotonic() < deadline:
            try:
                return self.get_device_rsd(udid, auto_start=False)
            except (TunnelDownError, DeviceNotFoundError):
                now = time.monotonic()
                remaining = deadline - now
                if target_udid and now >= next_active_attempt and remaining > 0:
                    try:
                        requested = self.request_configured_device_tunnel(
                            target_udid,
                            timeout=min(10.0, remaining),
                        )
                        if requested is not None:
                            return requested
                    except (TunnelDownError, DeviceNotFoundError):
                        pass
                    next_active_attempt = now + 5.0
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        message = (
            f"Device not found after {timeout:.0f}s. "
            "Is your iPhone unlocked, on the same WiFi network, and paired for remote tunneling?"
        )
        if self._last_start_error:
            message += f" Last tunnel start attempt: {self._last_start_error}."
            if "task not created" in self._last_start_error:
                message += (
                    " WiFi discovery can see the phone, but pymobiledevice3 could not create a trusted "
                    "tunnel; reconnect USB, trust the Mac if prompted, run `spoofloc setup`, then "
                    "`spoofloc tunnel restart`."
                )
        raise DeviceNotFoundError(message)

    def log_path(self) -> Path:
        return self._log_file
