from __future__ import annotations

import sqlite3
import ssl
import threading
import time
from pathlib import Path

import certifi
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim

from . import config as cfg_mod
from .exceptions import GeocodeError


class CachedGeocoder:
    def __init__(self):
        cfg = cfg_mod.load()
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._geolocator = Nominatim(
            user_agent=cfg["geocoding"]["user_agent"],
            timeout=10,
            ssl_context=ssl_context,
        )
        cache_path = cfg_mod.cache_dir() / "geocode_cache.db"
        self._db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self._max_entries: int = cfg["geocoding"]["max_cache_entries"]
        self._last_request = 0.0
        self._rate_lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS geocode_cache (
                query TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                display_name TEXT NOT NULL,
                cached_at INTEGER NOT NULL
            )
        """)
        self._db.commit()

    def _get_cached(self, query: str) -> tuple[float, float, str] | None:
        row = self._db.execute(
            "SELECT lat, lng, display_name FROM geocode_cache WHERE query = ?",
            (query.lower(),),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def _store(self, query: str, lat: float, lng: float, display_name: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO geocode_cache (query, lat, lng, display_name, cached_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (query.lower(), lat, lng, display_name, int(time.time())),
        )
        count = self._db.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0]
        if count > self._max_entries:
            self._db.execute(
                "DELETE FROM geocode_cache WHERE query IN "
                "(SELECT query FROM geocode_cache ORDER BY cached_at ASC LIMIT ?)",
                (count - self._max_entries,),
            )
        self._db.commit()

    def _rate_limit(self) -> None:
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            self._last_request = time.monotonic()

    def geocode(self, address: str) -> tuple[float, float, str]:
        cached = self._get_cached(address)
        if cached:
            return cached

        self._rate_limit()
        try:
            result = self._geolocator.geocode(address)
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            raise GeocodeError(f"Geocoding service error: {e}") from e

        if not result:
            raise GeocodeError(f"No results found for: {address!r}")

        self._store(address, result.latitude, result.longitude, result.address)
        return result.latitude, result.longitude, result.address

    def suggest(self, query: str, limit: int = 5, viewbox=None) -> list[tuple[float, float, str]]:
        if not query.strip():
            return []
        self._rate_limit()
        try:
            kwargs: dict = {"exactly_one": False, "limit": limit}
            if viewbox is not None:
                kwargs["viewbox"] = viewbox
            results = self._geolocator.geocode(query, **kwargs)
        except (GeocoderTimedOut, GeocoderServiceError):
            return []
        if not results:
            return []
        return [(r.latitude, r.longitude, r.address) for r in results]

    def reverse(self, lat: float, lng: float) -> str:
        self._rate_limit()
        try:
            result = self._geolocator.reverse(f"{lat}, {lng}")
            return result.address if result else f"{lat:.5f}, {lng:.5f}"
        except Exception:
            return f"{lat:.5f}, {lng:.5f}"
