import pytest

from spoofloc import location
from spoofloc.exceptions import LocationError
from spoofloc.location import RouteLocationSession


@pytest.mark.asyncio
async def test_route_location_session_reuses_single_dvt_connection(monkeypatch):
    calls = []

    def fake_release_location_hold():
        calls.append("release")

    class FakeRsd:
        def __init__(self, address):
            calls.append(("rsd", address))

        async def connect(self):
            calls.append("rsd_connect")

        async def close(self):
            calls.append("rsd_close")

    class FakeDvt:
        def __init__(self, rsd):
            calls.append(("dvt", rsd.__class__.__name__))

        async def __aenter__(self):
            calls.append("dvt_enter")
            return self

        async def __aexit__(self, *_):
            calls.append("dvt_exit")

    class FakeLocationSimulation:
        def __init__(self, dvt):
            calls.append(("location", dvt.__class__.__name__))

        async def __aenter__(self):
            calls.append("location_enter")
            return self

        async def __aexit__(self, *_):
            calls.append("location_exit")

        async def set(self, lat, lng):
            calls.append(("set", lat, lng))

    monkeypatch.setattr(location, "release_location_hold", fake_release_location_hold)
    monkeypatch.setattr(location, "RemoteServiceDiscoveryService", FakeRsd)
    monkeypatch.setattr(location, "DvtProvider", FakeDvt)
    monkeypatch.setattr(location, "LocationSimulation", FakeLocationSimulation)

    async with RouteLocationSession("fd00::1", 12345) as session:
        await session.set(1.0, 2.0)
        await session.set(3.0, 4.0)

    assert calls == [
        "release",
        ("rsd", ("fd00::1", 12345)),
        "rsd_connect",
        ("dvt", "FakeRsd"),
        "dvt_enter",
        ("location", "FakeDvt"),
        "location_enter",
        ("set", 1.0, 2.0),
        ("set", 3.0, 4.0),
        "location_exit",
        "dvt_exit",
        "rsd_close",
    ]


@pytest.mark.asyncio
async def test_route_location_session_wraps_set_errors(monkeypatch):
    class FakeRsd:
        def __init__(self, address):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

    class FakeDvt:
        def __init__(self, rsd):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class FakeLocationSimulation:
        def __init__(self, dvt):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def set(self, lat, lng):
            raise RuntimeError("device rejected update")

    monkeypatch.setattr(location, "release_location_hold", lambda: None)
    monkeypatch.setattr(location, "RemoteServiceDiscoveryService", FakeRsd)
    monkeypatch.setattr(location, "DvtProvider", FakeDvt)
    monkeypatch.setattr(location, "LocationSimulation", FakeLocationSimulation)

    async with RouteLocationSession("fd00::1", 12345) as session:
        with pytest.raises(LocationError, match="device rejected update"):
            await session.set(1.0, 2.0)
