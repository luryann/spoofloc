from __future__ import annotations

import json
import subprocess
from typing import Optional

from . import config as cfg_mod


def get_ios_version(udid: str) -> tuple[int, int, int]:
    cfg = cfg_mod.load()
    cache: dict = cfg.get("device", {}).get("ios_version_cache", {})
    if udid in cache:
        parts = str(cache[udid]).split(".")
        return (
            int(parts[0]),
            int(parts[1]) if len(parts) > 1 else 0,
            int(parts[2]) if len(parts) > 2 else 0,
        )

    version_str = "17.0.0"
    try:
        result = subprocess.run(
            [
                "python3", "-m", "pymobiledevice3",
                "lockdown", "info", "--udid", udid,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        info = json.loads(result.stdout)
        version_str = info.get("ProductVersion", "17.0.0")
    except Exception:
        pass

    parts = version_str.split(".")
    version = (
        int(parts[0]),
        int(parts[1]) if len(parts) > 1 else 0,
        int(parts[2]) if len(parts) > 2 else 0,
    )

    cfg.setdefault("device", {}).setdefault("ios_version_cache", {})[udid] = version_str
    cfg_mod.save(cfg)
    return version


def list_paired_devices() -> list[dict]:
    try:
        result = subprocess.run(
            ["python3", "-m", "pymobiledevice3", "usbmux", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return json.loads(result.stdout)
    except Exception:
        return []


def device_udid(device: dict) -> Optional[str]:
    for key in ("Identifier", "UniqueDeviceID", "UDID", "udid", "serial", "SerialNumber"):
        value = device.get(key)
        if value:
            return str(value)
    return None


def device_name(device: dict) -> str:
    return str(device.get("DeviceName") or device.get("name") or "iPhone")


def prefer_usb_device(devices: list[dict]) -> dict:
    for device in devices:
        if str(device.get("ConnectionType", "")).lower() == "usb":
            return device
    return devices[0]


def enable_wifi_connections(udid: Optional[str] = None) -> None:
    base_cmd = ["python3", "-m", "pymobiledevice3", "lockdown", "wifi-connections"]
    udid_args = ["--udid", udid] if udid else []
    cmd = base_cmd + ["--state", "on"] + udid_args
    fallback_cmd = base_cmd + ["on"] + udid_args

    try:
        subprocess.run(cmd, check=True, timeout=15, capture_output=True, text=True)
        return
    except subprocess.CalledProcessError as e:
        output = f"{e.stderr or ''}\n{e.stdout or ''}"
        if "--state" not in output and "No such option" not in output and "unexpected extra argument" not in output:
            raise

    subprocess.run(fallback_cmd, check=True, timeout=15)


def pair_remote_device(name: Optional[str] = None) -> None:
    cmd = ["python3", "-m", "pymobiledevice3", "remote", "pair"]
    if name:
        cmd += ["--name", name]
    subprocess.run(cmd, check=True, timeout=120)


def check_developer_mode_enabled(udid: Optional[str] = None) -> bool:
    cmd = ["python3", "-m", "pymobiledevice3", "amfi", "developer-mode-status"]
    if udid:
        cmd += ["--udid", udid]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True)
        output = (result.stdout + result.stderr).lower()
        return "true" in output or "enabled" in output
    except Exception:
        return False


def reveal_developer_mode(udid: Optional[str] = None) -> None:
    cmd = ["python3", "-m", "pymobiledevice3", "amfi", "reveal-developer-mode"]
    if udid:
        cmd += ["--udid", udid]
    subprocess.run(cmd, check=True, timeout=30, capture_output=True, text=True)
