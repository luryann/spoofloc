from types import SimpleNamespace

import pytest

from spoofloc.web import server


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class _FakeTunnel:
    def state(self):
        return SimpleNamespace(name="DEVICE_READY")

    def get_device_rsd(self, udid=None):
        return "fake-udid", "fd00::1", 12345


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(server.threading, "Thread", _NoopThread)
    monkeypatch.setattr(server, "_tunnel", _FakeTunnel())
    with server._location_lock:
        server._current_location.update({"lat": None, "lng": None, "spoof_active": False})
    return server.create_app()


def test_location_set_uses_requested_coordinates(app, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "set_location",
        lambda lat, lng, udid: calls.append((lat, lng, udid)),
    )

    response = app.test_client().post(
        "/api/location/set",
        json={"lat": "48.8566", "lng": "2.3522"},
    )

    assert response.status_code == 200
    assert response.get_json()["applied"] is True
    assert calls == [(48.8566, 2.3522, "fake-udid")]
    with server._location_lock:
        assert server._current_location == {
            "lat": 48.8566,
            "lng": 2.3522,
            "spoof_active": True,
        }


@pytest.mark.parametrize(
    ("lat", "lng", "message"),
    [
        (91, 2.3522, "Latitude"),
        (48.8566, -181, "Longitude"),
        ("nan", 2.3522, "finite"),
        ("not-a-number", 2.3522, "numeric"),
    ],
)
def test_location_set_rejects_invalid_coordinates(app, monkeypatch, lat, lng, message):
    calls = []
    monkeypatch.setattr(
        server,
        "set_location",
        lambda *args: calls.append(args),
    )

    response = app.test_client().post(
        "/api/location/set",
        json={"lat": lat, "lng": lng},
    )

    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert calls == []
