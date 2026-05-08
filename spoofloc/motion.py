from __future__ import annotations

import asyncio
import math
import random
from typing import Awaitable, Callable

from . import config as cfg_mod
from .route import haversine_m, lerp

TickCallback = Callable[[float, float], Awaitable[None]]

PATTERNS = ("walk", "orbit", "oscillate", "drift")


def move_by(lat: float, lng: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Destination point given start, bearing (degrees), and distance (metres)."""
    R = 6_371_000.0
    lat_r = math.radians(lat)
    lng_r = math.radians(lng)
    b_r = math.radians(bearing_deg)
    d_r = distance_m / R
    new_lat_r = math.asin(
        math.sin(lat_r) * math.cos(d_r)
        + math.cos(lat_r) * math.sin(d_r) * math.cos(b_r)
    )
    new_lng_r = lng_r + math.atan2(
        math.sin(b_r) * math.sin(d_r) * math.cos(lat_r),
        math.cos(d_r) - math.sin(lat_r) * math.sin(new_lat_r),
    )
    return math.degrees(new_lat_r), math.degrees(new_lng_r)


def bearing_to(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Forward bearing in degrees from (lat1, lng1) to (lat2, lng2)."""
    lat1_r, lng1_r = math.radians(lat1), math.radians(lng1)
    lat2_r, lng2_r = math.radians(lat2), math.radians(lng2)
    dlng = lng2_r - lng1_r
    x = math.sin(dlng) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


class MotionPlayer:
    def __init__(self) -> None:
        self._stop: asyncio.Event | None = None
        self._running = False
        self._state: dict = {
            "pattern": None,
            "lat": None,
            "lng": None,
            "error": None,
        }

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def get_state(self) -> dict:
        return dict(self._state)

    def _tick_hz(self) -> float:
        return float(cfg_mod.load()["route"]["tick_hz"])

    async def _emit(self, lat: float, lng: float, on_tick: TickCallback) -> None:
        self._state["lat"] = lat
        self._state["lng"] = lng
        await on_tick(lat, lng)

    # ------------------------------------------------------------------ walk

    async def run_walk(
        self,
        center_lat: float,
        center_lng: float,
        radius_m: float,
        speed_mph: float,
        jitter: float,
        on_tick: TickCallback,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._stop = asyncio.Event()
        self._running = True
        self._state.update({"pattern": "walk", "lat": None, "lng": None, "error": None})
        try:
            await self._walk(center_lat, center_lng, radius_m, speed_mph, jitter, on_tick)
        except Exception as e:
            self._state["error"] = str(e)
            raise
        finally:
            self._running = False
            if on_done:
                on_done()

    async def _walk(
        self,
        center_lat: float,
        center_lng: float,
        radius_m: float,
        speed_mph: float,
        jitter: float,
        on_tick: TickCallback,
    ) -> None:
        tick_hz = self._tick_hz()
        step_m = (speed_mph * 0.44704) / tick_hz
        stop = self._stop
        lat, lng = center_lat, center_lng

        def pick_target() -> tuple[float, float]:
            angle = random.uniform(0, 360)
            dist = random.uniform(radius_m * 0.1, radius_m * 0.9)
            return move_by(center_lat, center_lng, angle, dist)

        tgt_lat, tgt_lng = pick_target()

        while not stop.is_set():
            await self._emit(lat, lng, on_tick)
            if stop.is_set():
                break

            if haversine_m(lat, lng, tgt_lat, tgt_lng) < max(step_m * 2, 2.0):
                tgt_lat, tgt_lng = pick_target()

            toward = bearing_to(lat, lng, tgt_lat, tgt_lng)
            heading = (toward + random.gauss(0, 60.0 * jitter) + 360) % 360

            # Pull back toward center when outside radius
            dist_from_center = haversine_m(lat, lng, center_lat, center_lng)
            if dist_from_center > radius_m:
                back = bearing_to(lat, lng, center_lat, center_lng)
                t = min((dist_from_center - radius_m) / (radius_m * 0.5), 1.0)
                delta = (back - heading + 180) % 360 - 180
                heading = (heading + delta * (0.5 + 0.5 * t) + 360) % 360
                tgt_lat, tgt_lng = pick_target()

            lat, lng = move_by(lat, lng, heading, step_m)
            await asyncio.sleep(1.0 / tick_hz)

    # ----------------------------------------------------------------- orbit

    async def run_orbit(
        self,
        center_lat: float,
        center_lng: float,
        radius_m: float,
        speed_mph: float,
        on_tick: TickCallback,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._stop = asyncio.Event()
        self._running = True
        self._state.update({"pattern": "orbit", "lat": None, "lng": None, "error": None})
        try:
            await self._orbit(center_lat, center_lng, radius_m, speed_mph, on_tick)
        except Exception as e:
            self._state["error"] = str(e)
            raise
        finally:
            self._running = False
            if on_done:
                on_done()

    async def _orbit(
        self,
        center_lat: float,
        center_lng: float,
        radius_m: float,
        speed_mph: float,
        on_tick: TickCallback,
    ) -> None:
        tick_hz = self._tick_hz()
        step_m = (speed_mph * 0.44704) / tick_hz
        stop = self._stop
        angle = 0.0
        angular_step = step_m / max(radius_m, 1.0)

        while not stop.is_set():
            lat, lng = move_by(center_lat, center_lng, math.degrees(angle), radius_m)
            await self._emit(lat, lng, on_tick)
            if stop.is_set():
                break
            angle = (angle + angular_step) % (2 * math.pi)
            await asyncio.sleep(1.0 / tick_hz)

    # -------------------------------------------------------------- oscillate

    async def run_oscillate(
        self,
        lat1: float,
        lng1: float,
        lat2: float,
        lng2: float,
        speed_mph: float,
        on_tick: TickCallback,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._stop = asyncio.Event()
        self._running = True
        self._state.update({"pattern": "oscillate", "lat": None, "lng": None, "error": None})
        try:
            await self._oscillate(lat1, lng1, lat2, lng2, speed_mph, on_tick)
        except Exception as e:
            self._state["error"] = str(e)
            raise
        finally:
            self._running = False
            if on_done:
                on_done()

    async def _oscillate(
        self,
        lat1: float,
        lng1: float,
        lat2: float,
        lng2: float,
        speed_mph: float,
        on_tick: TickCallback,
    ) -> None:
        tick_hz = self._tick_hz()
        step_m = (speed_mph * 0.44704) / tick_hz
        stop = self._stop
        total_dist = max(haversine_m(lat1, lng1, lat2, lng2), 1.0)

        fraction = 0.0
        direction = 1.0

        while not stop.is_set():
            lat = lerp(lat1, lat2, fraction)
            lng = lerp(lng1, lng2, fraction)
            await self._emit(lat, lng, on_tick)
            if stop.is_set():
                break

            fraction += direction * (step_m / total_dist)
            if fraction >= 1.0:
                fraction = 1.0
                direction = -1.0
            elif fraction <= 0.0:
                fraction = 0.0
                direction = 1.0

            await asyncio.sleep(1.0 / tick_hz)

    # ------------------------------------------------------------------ drift

    async def run_drift(
        self,
        start_lat: float,
        start_lng: float,
        speed_mph: float,
        on_tick: TickCallback,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._stop = asyncio.Event()
        self._running = True
        self._state.update({"pattern": "drift", "lat": None, "lng": None, "error": None})
        try:
            await self._drift(start_lat, start_lng, speed_mph, on_tick)
        except Exception as e:
            self._state["error"] = str(e)
            raise
        finally:
            self._running = False
            if on_done:
                on_done()

    async def _drift(
        self,
        start_lat: float,
        start_lng: float,
        speed_mph: float,
        on_tick: TickCallback,
    ) -> None:
        tick_hz = self._tick_hz()
        step_m = (speed_mph * 0.44704) / tick_hz
        stop = self._stop
        lat, lng = start_lat, start_lng
        bearing = random.uniform(0, 360)

        while not stop.is_set():
            await self._emit(lat, lng, on_tick)
            if stop.is_set():
                break
            bearing = (bearing + random.gauss(0, 3.0) + 360) % 360
            lat, lng = move_by(lat, lng, bearing, step_m)
            await asyncio.sleep(1.0 / tick_hz)
