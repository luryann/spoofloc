from __future__ import annotations

import asyncio
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import config as cfg_mod
from .exceptions import RouteError


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def lerp(a: float, b: float, t: float) -> float:
    return a + t * (b - a)


def load_gpx(source: str) -> list[tuple[float, float]]:
    if source == "-":
        content = sys.stdin.read()
        root = ET.fromstring(content)
    else:
        tree = ET.parse(source)
        root = tree.getroot()

    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    waypoints: list[tuple[float, float]] = []

    for trkpt in root.findall(".//gpx:trkpt", ns):
        waypoints.append((float(trkpt.get("lat")), float(trkpt.get("lon"))))

    if not waypoints:
        for trkpt in root.findall(".//trkpt"):
            waypoints.append((float(trkpt.get("lat")), float(trkpt.get("lon"))))

    if not waypoints:
        for wpt in root.findall(".//gpx:wpt", ns):
            waypoints.append((float(wpt.get("lat")), float(wpt.get("lon"))))

    if not waypoints:
        raise RouteError("No waypoints found in GPX file")

    return waypoints


class RoutePlayer:
    def __init__(self):
        self._pause_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._state: dict = {
            "segment_index": 0,
            "fraction": 0.0,
            "ticks": 0,
            "total_waypoints": 0,
            "speed_mph": 0.0,
            "lat": None,
            "lng": None,
            "error": None,
        }
        self._running = False
        self._state_path: Path = cfg_mod.cache_dir() / "route_state.json"

    def is_running(self) -> bool:
        return self._running

    def get_progress(self) -> dict:
        return dict(self._state)

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()  # unblock any paused wait

    async def play(
        self,
        waypoints: list[tuple[float, float]],
        speed_mph: float,
        loop: bool,
        on_tick: Callable[[float, float], Awaitable[None]],
        on_done: Optional[Callable[[], None]] = None,
        segment_speeds_mph: Optional[list[float]] = None,
    ) -> None:
        if len(waypoints) < 2:
            raise RouteError("Need at least 2 waypoints to simulate a route")

        cfg = cfg_mod.load()
        tick_hz: float = cfg["route"]["tick_hz"]
        if tick_hz <= 0:
            raise RouteError("route.tick_hz must be greater than 0")
        if segment_speeds_mph is None and speed_mph <= 0:
            raise RouteError("Route speed must be greater than 0 mph")

        segments = [
            max(haversine_m(*waypoints[i], *waypoints[i + 1]), 0.001)
            for i in range(len(waypoints) - 1)
        ]

        initial_speed = segment_speeds_mph[0] if segment_speeds_mph else speed_mph
        self._stop_event.clear()
        self._pause_event.set()
        self._running = True
        self._state.update({
            "total_waypoints": len(waypoints),
            "speed_mph": initial_speed,
            "ticks": 0,
            "lat": None,
            "lng": None,
            "error": None,
        })

        seg_idx = 0
        fraction = 0.0

        async def emit_tick(lat: float, lng: float, state_seg_idx: int, state_fraction: float) -> None:
            tick_num = self._state["ticks"] + 1
            self._state.update({
                "segment_index": state_seg_idx,
                "fraction": state_fraction,
                "ticks": tick_num,
                "lat": lat,
                "lng": lng,
                "error": None,
            })

            try:
                await on_tick(lat, lng)
            except Exception as e:
                self._state["error"] = str(e)
                raise

            if tick_num % 10 == 0:
                try:
                    self._state_path.write_text(json.dumps(self._state))
                except Exception:
                    pass

        try:
            while not self._stop_event.is_set():
                await self._pause_event.wait()
                if self._stop_event.is_set():
                    break

                current_speed_mph = segment_speeds_mph[seg_idx] if segment_speeds_mph else speed_mph
                step_m = (current_speed_mph * 0.44704) / tick_hz
                self._state["speed_mph"] = current_speed_mph

                lat = lerp(waypoints[seg_idx][0], waypoints[seg_idx + 1][0], fraction)
                lng = lerp(waypoints[seg_idx][1], waypoints[seg_idx + 1][1], fraction)
                await emit_tick(lat, lng, seg_idx, fraction)
                if self._stop_event.is_set():
                    break

                remaining = step_m
                completed = False
                while remaining > 0 and not self._stop_event.is_set():
                    seg_remaining = segments[seg_idx] * (1.0 - fraction)
                    if remaining <= seg_remaining:
                        fraction += remaining / segments[seg_idx]
                        remaining = 0.0
                        if fraction >= 1.0:
                            if seg_idx >= len(segments) - 1:
                                if loop:
                                    seg_idx = 0
                                    fraction = 0.0
                                else:
                                    completed = True
                            else:
                                seg_idx += 1
                                fraction = 0.0
                    else:
                        remaining -= seg_remaining
                        seg_idx += 1
                        fraction = 0.0
                        if seg_idx >= len(segments):
                            if loop:
                                seg_idx = 0
                            else:
                                completed = True
                                break

                if completed and not self._stop_event.is_set():
                    final_seg_idx = len(segments) - 1
                    final_lat, final_lng = waypoints[-1]
                    await emit_tick(final_lat, final_lng, final_seg_idx, 1.0)
                    self._stop_event.set()
                    break

                await asyncio.sleep(1.0 / tick_hz)
        finally:
            self._running = False
            if on_done:
                on_done()
