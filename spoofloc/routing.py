from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import certifi

from .exceptions import RoutingError

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
_MIN_SPEED_MPH = 6.0
_MAX_SPEED_MPH = 80.0


@dataclass
class RouteOption:
    waypoints: list[tuple[float, float]]
    segment_speeds_mph: list[float]
    distance_m: float
    duration_s: float

    def to_dict(self) -> dict:
        return {
            "waypoints": [[lat, lng] for lat, lng in self.waypoints],
            "segment_speeds_mph": self.segment_speeds_mph,
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
        }


def get_routes(
    lat_a: float,
    lng_a: float,
    lat_b: float,
    lng_b: float,
) -> list[RouteOption]:
    """Return up to 3 road-snapped driving routes via OSRM (no API key required)."""
    coords = f"{lng_a},{lat_a};{lng_b},{lat_b}"
    params = urllib.parse.urlencode({
        "alternatives": "true",
        "steps": "true",
        "geometries": "geojson",
        "overview": "false",
    })
    url = f"{OSRM_BASE}/{coords}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "spoofloc/0.1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RoutingError(f"OSRM request failed: {e}") from e

    if data.get("code") != "Ok":
        msg = data.get("message") or data.get("code") or "unknown"
        raise RoutingError(f"OSRM error: {msg}")

    options: list[RouteOption] = []
    for route in data.get("routes", []):
        try:
            waypoints, speeds = _extract_waypoints_and_speeds(route)
        except Exception:
            continue
        if len(waypoints) >= 2:
            options.append(RouteOption(
                waypoints=waypoints,
                segment_speeds_mph=speeds,
                distance_m=float(route.get("distance", 0.0)),
                duration_s=float(route.get("duration", 0.0)),
            ))

    if not options:
        raise RoutingError("No valid routes returned from OSRM")

    return options[:3]


def _extract_waypoints_and_speeds(
    route: dict,
) -> tuple[list[tuple[float, float]], list[float]]:
    waypoints: list[tuple[float, float]] = []
    speeds: list[float] = []

    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            duration = step.get("duration", 0.0)
            distance = step.get("distance", 0.0)

            if duration > 0 and distance > 0:
                speed_mph = (distance / duration) * 2.23694
                speed_mph = max(_MIN_SPEED_MPH, min(_MAX_SPEED_MPH, speed_mph))
            else:
                speed_mph = 30.0

            coords = step.get("geometry", {}).get("coordinates", [])
            for lng, lat in coords:
                pt = (float(lat), float(lng))
                if not waypoints or pt[0] != waypoints[-1][0] or pt[1] != waypoints[-1][1]:
                    waypoints.append(pt)
                    if len(waypoints) > 1:
                        speeds.append(speed_mph)

    return waypoints, speeds
