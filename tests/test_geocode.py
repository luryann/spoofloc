import sqlite3
import time
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest

from spoofloc.exceptions import GeocodeError


def make_geocoder(tmp_path, monkeypatch):
    import spoofloc.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cfg_mod, "cache_dir", lambda: tmp_path)
    from spoofloc.geocode import CachedGeocoder
    return CachedGeocoder()


def mock_location(lat, lng, address):
    loc = MagicMock()
    loc.latitude = lat
    loc.longitude = lng
    loc.address = address
    return loc


def test_geocode_cache_hit(tmp_path, monkeypatch):
    geocoder = make_geocoder(tmp_path, monkeypatch)
    # Insert directly into cache
    geocoder._store("paris, france", 48.8566, 2.3522, "Paris, France")
    lat, lng, name = geocoder.geocode("paris, france")
    assert lat == pytest.approx(48.8566)
    assert lng == pytest.approx(2.3522)
    assert "Paris" in name


def test_geocode_cache_miss_calls_nominatim(tmp_path, monkeypatch):
    geocoder = make_geocoder(tmp_path, monkeypatch)
    mock_result = mock_location(51.5074, -0.1278, "London, UK")
    geocoder._geolocator.geocode = MagicMock(return_value=mock_result)

    lat, lng, name = geocoder.geocode("London")
    assert lat == pytest.approx(51.5074)
    geocoder._geolocator.geocode.assert_called_once()


def test_geocode_no_result_raises(tmp_path, monkeypatch):
    geocoder = make_geocoder(tmp_path, monkeypatch)
    geocoder._geolocator.geocode = MagicMock(return_value=None)

    with pytest.raises(GeocodeError, match="No results"):
        geocoder.geocode("xyzzy nonexistent place 12345")


def test_geocode_caches_after_network(tmp_path, monkeypatch):
    geocoder = make_geocoder(tmp_path, monkeypatch)
    mock_result = mock_location(40.7128, -74.0060, "New York, USA")
    geocoder._geolocator.geocode = MagicMock(return_value=mock_result)

    geocoder.geocode("New York")
    geocoder.geocode("New York")  # second call should hit cache

    assert geocoder._geolocator.geocode.call_count == 1
