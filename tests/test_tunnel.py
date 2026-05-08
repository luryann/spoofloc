import pytest

from spoofloc import config as cfg_mod
from spoofloc.exceptions import DeviceNotFoundError
from spoofloc.tunnel import TunnelManager


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod, "cache_dir", lambda: tmp_path)
    return TunnelManager()


def test_request_device_tunnel_uses_tunneld_start_endpoint_auto(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"daemon_url": "http://127.0.0.1:49151/"},
        },
    )
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"address": "fd00::1", "port": 12345, "interface": "192.168.1.10"}

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return Response()

    monkeypatch.setattr("spoofloc.tunnel.httpx.get", fake_get)

    assert manager.request_device_tunnel("abc", timeout=9.0) == ("abc", "fd00::1", 12345)
    assert calls == [
        (
            "http://127.0.0.1:49151/start-tunnel",
            {"udid": "abc"},
            9.0,
        )
    ]


def test_request_device_tunnel_can_request_specific_connection_type(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"daemon_url": "http://127.0.0.1:49151/"},
        },
    )
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"address": "fd00::1", "port": 12345}

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return Response()

    monkeypatch.setattr("spoofloc.tunnel.httpx.get", fake_get)

    assert manager.request_device_tunnel("abc", connection_type="wifi", timeout=9.0) == (
        "abc",
        "fd00::1",
        12345,
    )
    assert calls == [
        (
            "http://127.0.0.1:49151/start-tunnel",
            {"udid": "abc", "connection_type": "wifi"},
            9.0,
        )
    ]


def test_request_device_tunnel_normalizes_network_to_usbmux(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"daemon_url": "http://127.0.0.1:49151/"},
        },
    )
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"address": "fd00::1", "port": 12345}

    monkeypatch.setattr(
        "spoofloc.tunnel.httpx.get",
        lambda url, params, timeout: calls.append((url, params, timeout)) or Response(),
    )

    assert manager.request_device_tunnel("abc", connection_type="network") == (
        "abc",
        "fd00::1",
        12345,
    )
    assert calls[0][1] == {"udid": "abc", "connection_type": "usbmux"}


def test_wait_for_device_requests_default_udid_when_passive_discovery_lags(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"preferred_connection_type": "wifi"},
        },
    )
    monkeypatch.setattr(
        manager,
        "get_device_rsd",
        lambda udid=None, **kwargs: (_ for _ in ()).throw(DeviceNotFoundError()),
    )

    calls = []

    def fake_request(udid, connection_type, timeout):
        calls.append((udid, connection_type, timeout))
        return ("abc", "fd00::1", 12345)

    monkeypatch.setattr(manager, "request_device_tunnel", fake_request)

    assert manager.wait_for_device(timeout=5.0) == ("abc", "fd00::1", 12345)
    assert calls[0][0] == "abc"
    assert calls[0][1] == "wifi"


def test_wait_for_device_auto_omits_connection_type(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"preferred_connection_type": "auto"},
        },
    )
    monkeypatch.setattr(
        manager,
        "get_device_rsd",
        lambda udid=None, **kwargs: (_ for _ in ()).throw(DeviceNotFoundError()),
    )

    calls = []

    def fake_request(udid, connection_type, timeout):
        calls.append((udid, connection_type, timeout))
        return ("abc", "fd00::1", 12345)

    monkeypatch.setattr(manager, "request_device_tunnel", fake_request)

    assert manager.wait_for_device(timeout=5.0) == ("abc", "fd00::1", 12345)
    assert calls[0][0] == "abc"
    assert calls[0][1] is None


def test_wait_for_device_falls_back_to_auto_after_preferred_failure(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"preferred_connection_type": "wifi"},
        },
    )
    monkeypatch.setattr(
        manager,
        "get_device_rsd",
        lambda udid=None, **kwargs: (_ for _ in ()).throw(DeviceNotFoundError()),
    )

    calls = []

    def fake_request(udid, connection_type, timeout):
        calls.append((udid, connection_type, timeout))
        if connection_type == "wifi":
            return None
        return ("abc", "fd00::1", 12345)

    monkeypatch.setattr(manager, "request_device_tunnel", fake_request)

    assert manager.wait_for_device(timeout=5.0) == ("abc", "fd00::1", 12345)
    assert [call[1] for call in calls[:2]] == ["wifi", None]


def test_wait_for_device_respects_explicit_udid_for_active_start(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "default"},
            "tunnel": {"preferred_connection_type": "wifi"},
        },
    )
    monkeypatch.setattr(
        manager,
        "get_device_rsd",
        lambda udid=None, **kwargs: (_ for _ in ()).throw(DeviceNotFoundError()),
    )
    monkeypatch.setattr(
        manager,
        "request_device_tunnel",
        lambda udid, connection_type, timeout: (udid, "fd00::1", 12345),
    )

    assert manager.wait_for_device(udid="explicit", timeout=5.0) == ("explicit", "fd00::1", 12345)


def test_get_device_rsd_starts_default_udid_when_probe_is_empty(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"preferred_connection_type": "auto", "reconnect_timeout_s": 10},
        },
    )
    monkeypatch.setattr(manager, "probe", lambda: {})

    calls = []

    def fake_request(udid, timeout=None):
        calls.append((udid, timeout))
        return ("abc", "fd00::1", 12345)

    monkeypatch.setattr(manager, "request_configured_device_tunnel", fake_request)

    assert manager.get_device_rsd() == ("abc", "fd00::1", 12345)
    assert calls == [("abc", None)]


def test_get_device_rsd_starts_default_before_falling_back_to_other_device(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"preferred_connection_type": "auto", "reconnect_timeout_s": 10},
        },
    )
    monkeypatch.setattr(
        manager,
        "probe",
        lambda: {"other": [{"tunnel-address": "fd00::2", "tunnel-port": 23456}]},
    )

    calls = []

    def fake_request(udid, timeout=None):
        calls.append((udid, timeout))
        return ("abc", "fd00::1", 12345)

    monkeypatch.setattr(manager, "request_configured_device_tunnel", fake_request)

    assert manager.get_device_rsd() == ("abc", "fd00::1", 12345)
    assert calls == [("abc", None)]


def test_request_device_tunnel_records_tunneld_error(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cfg_mod,
        "load",
        lambda: {
            "device": {"default_udid": "abc"},
            "tunnel": {"daemon_url": "http://127.0.0.1:49151/"},
        },
    )

    class Response:
        status_code = 501
        text = '{"error":"task not created"}'

        def json(self):
            return {"error": "task not created"}

    monkeypatch.setattr("spoofloc.tunnel.httpx.get", lambda *args, **kwargs: Response())

    assert manager.request_device_tunnel("abc") is None
    assert manager.last_start_error() == "tunneld /start-tunnel returned HTTP 501: task not created"
