import subprocess

import pytest

from spoofloc.device import device_name, device_udid, enable_wifi_connections, prefer_usb_device


def test_device_udid_prefers_current_pymobiledevice_identifier():
    assert (
        device_udid({"Identifier": "identifier-udid", "UniqueDeviceID": "legacy-udid"})
        == "identifier-udid"
    )


def test_device_udid_falls_back_to_unique_device_id():
    assert device_udid({"UniqueDeviceID": "legacy-udid"}) == "legacy-udid"


def test_device_name_defaults_to_iphone():
    assert device_name({}) == "iPhone"


def test_prefer_usb_device_selects_usb_entry():
    network = {"Identifier": "network", "ConnectionType": "Network"}
    usb = {"Identifier": "usb", "ConnectionType": "USB"}

    assert prefer_usb_device([network, usb]) is usb


def test_enable_wifi_connections_uses_state_option(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("spoofloc.device.subprocess.run", fake_run)

    enable_wifi_connections("abc")

    assert calls == [
        (
            [
                "python3",
                "-m",
                "pymobiledevice3",
                "lockdown",
                "wifi-connections",
                "--state",
                "on",
                "--udid",
                "abc",
            ],
            {"check": True, "timeout": 15, "capture_output": True, "text": True},
        )
    ]


def test_enable_wifi_connections_falls_back_to_positional_state(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "--state" in cmd:
            raise subprocess.CalledProcessError(2, cmd, stderr="No such option: --state")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("spoofloc.device.subprocess.run", fake_run)

    enable_wifi_connections("abc")

    assert calls[1][0] == [
        "python3",
        "-m",
        "pymobiledevice3",
        "lockdown",
        "wifi-connections",
        "on",
        "--udid",
        "abc",
    ]


def test_enable_wifi_connections_reraises_device_errors(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="No device found")

    monkeypatch.setattr("spoofloc.device.subprocess.run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        enable_wifi_connections("abc")
