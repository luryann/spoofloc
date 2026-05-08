from __future__ import annotations

import asyncio
import json
import math
import queue
import threading
import time
import webbrowser
from pathlib import Path
from typing import Generator, Optional

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from .. import config as cfg_mod
from ..exceptions import DeviceNotFoundError, GeocodeError, LocationError, RoutingError, TunnelDownError
from .. import routing as routing_mod
from ..geocode import CachedGeocoder
from ..location import RouteLocationSession, clear_location, set_location
from ..motion import MotionPlayer
from ..route import RoutePlayer
from ..tunnel import TunnelManager

# Singletons shared across requests
_tunnel = TunnelManager()
_route_player = RoutePlayer()
_motion_player = MotionPlayer()
_geocoder: Optional[CachedGeocoder] = None
_geocoder_lock = threading.Lock()

_current_location: dict = {"lat": None, "lng": None, "spoof_active": False}
_location_lock = threading.Lock()

_sse_listeners: list[queue.Queue] = []
_sse_lock = threading.Lock()

# Acquired while either a route or a motion pattern is running.
_playback_lock = threading.Lock()

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None

_active_udid: Optional[str] = None
_route_hold_final = True
_route_stop_requested = False


def _parse_lat_lng(payload: dict, *, label: str = "Coordinates") -> tuple[float, float]:
    try:
        lat = float(payload["lat"])
        lng = float(payload["lng"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{label} must include numeric lat/lng values") from e

    if not math.isfinite(lat) or not math.isfinite(lng):
        raise ValueError(f"{label} must include finite lat/lng values")
    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be between -90 and 90")
    if not -180 <= lng <= 180:
        raise ValueError("Longitude must be between -180 and 180")
    return lat, lng


def _start_async_loop() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _get_geocoder() -> CachedGeocoder:
    global _geocoder
    with _geocoder_lock:
        if _geocoder is None:
            _geocoder = CachedGeocoder()
        return _geocoder


def _push_sse(event_type: str, data: dict) -> None:
    with _sse_lock:
        listeners = list(_sse_listeners)
    for q in listeners:
        try:
            q.put_nowait((event_type, data))
        except queue.Full:
            pass


def _format_sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _status_poller() -> None:
    prev_state: Optional[str] = None
    while True:
        try:
            state_name = _tunnel.state().name
            if state_name != prev_state:
                prev_state = state_name
                _push_sse("status", {
                    "tunnel": state_name,
                    "route_running": _route_player.is_running(),
                    "motion_running": _motion_player.is_running(),
                })
        except Exception:
            pass
        time.sleep(0.5)


def create_app(udid: Optional[str] = None) -> Flask:
    global _active_udid, _loop_thread

    _active_udid = udid

    _loop_thread = threading.Thread(target=_start_async_loop, daemon=True)
    _loop_thread.start()

    poller = threading.Thread(target=_status_poller, daemon=True)
    poller.start()

    static_dir = str(Path(__file__).parent / "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/api/status")
    def api_status():
        tunnel_state = _tunnel.state().name
        with _location_lock:
            loc = dict(_current_location)
        return jsonify({
            "tunnel": tunnel_state,
            "spoof_active": loc["spoof_active"],
            "lat": loc["lat"],
            "lng": loc["lng"],
            "route_running": _route_player.is_running(),
            "route_progress": _route_player.get_progress() if _route_player.is_running() else None,
            "motion_running": _motion_player.is_running(),
            "motion_state": _motion_player.get_state() if _motion_player.is_running() else None,
        })

    @app.route("/api/devices")
    def api_devices():
        try:
            data = _tunnel.probe()
            devices = [
                {"udid": udid, "tunnel_address": e["tunnel-address"], "tunnel_port": e["tunnel-port"]}
                for udid, entries in data.items()
                for e in entries
            ]
            return jsonify(devices)
        except TunnelDownError as e:
            return jsonify({"error": str(e)}), 503

    @app.route("/api/location/set", methods=["POST"])
    def api_location_set():
        body = request.get_json(force=True) or {}
        try:
            if "address" in body:
                lat, lng, display_name = _get_geocoder().geocode(body["address"])
            elif "lat" in body and "lng" in body:
                lat, lng = _parse_lat_lng(body)
                display_name = None
            else:
                return jsonify({"error": "Provide lat/lng or address"}), 400

            device_udid, _, _ = _tunnel.get_device_rsd(_active_udid)
            set_location(lat, lng, device_udid)

            with _location_lock:
                _current_location.update({"lat": lat, "lng": lng, "spoof_active": True})

            _push_sse("status", {"lat": lat, "lng": lng, "spoof_active": True})
            return jsonify({"lat": lat, "lng": lng, "display_name": display_name, "applied": True})

        except GeocodeError as e:
            return jsonify({"error": str(e)}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except (TunnelDownError, DeviceNotFoundError) as e:
            return jsonify({"error": str(e)}), 503
        except LocationError as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/location/clear", methods=["POST"])
    def api_location_clear():
        try:
            device_udid, _, _ = _tunnel.get_device_rsd(_active_udid)
            clear_location(device_udid)
            with _location_lock:
                _current_location.update({"lat": None, "lng": None, "spoof_active": False})
            _push_sse("status", {"spoof_active": False, "lat": None, "lng": None})
            return jsonify({"ok": True})
        except (TunnelDownError, DeviceNotFoundError, LocationError) as e:
            return jsonify({"error": str(e)}), 503

    def _resolve_location(value: object, label: str) -> tuple[float, float]:
        if value is None:
            raise ValueError(f"Missing {label}")
        if isinstance(value, str):
            lat, lng, _ = _get_geocoder().geocode(value)
            return lat, lng
        if isinstance(value, dict):
            return _parse_lat_lng(value, label=label)
        raise ValueError(f"{label} must be an address string or {{lat, lng}} object")

    @app.route("/api/route/plan", methods=["POST"])
    def api_route_plan():
        body = request.get_json(force=True) or {}
        try:
            lat_a, lng_a = _resolve_location(body.get("origin"), "origin")
            lat_b, lng_b = _resolve_location(body.get("destination"), "destination")
        except GeocodeError as e:
            return jsonify({"error": str(e)}), 503
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        try:
            routes = routing_mod.get_routes(lat_a, lng_a, lat_b, lng_b)
        except RoutingError as e:
            return jsonify({"error": str(e)}), 502

        return jsonify({"routes": [r.to_dict() for r in routes]})

    @app.route("/api/route/start", methods=["POST"])
    def api_route_start():
        global _route_hold_final, _route_stop_requested

        if not _playback_lock.acquire(blocking=False):
            return jsonify({"error": "A route or motion is already running. Stop it first."}), 409

        try:
            body = request.get_json(force=True) or {}
            wps_raw = body.get("waypoints", [])
            if len(wps_raw) < 2:
                _playback_lock.release()
                return jsonify({"error": "Need at least 2 waypoints"}), 400

            try:
                waypoints = [_parse_lat_lng(w, label="Waypoints") for w in wps_raw]
                speed_mph = float(body.get("speed_mph", cfg_mod.load()["route"]["default_speed_mph"]))
            except (KeyError, TypeError, ValueError):
                _playback_lock.release()
                return jsonify({"error": "Waypoints must include numeric lat/lng values"}), 400
            loop_flag = bool(body.get("loop", False))

            # Optional per-segment speeds from a planned route (one per segment)
            seg_speeds_raw = body.get("segment_speeds_mph")
            segment_speeds_mph = None
            if isinstance(seg_speeds_raw, list) and len(seg_speeds_raw) == len(waypoints) - 1:
                try:
                    segment_speeds_mph = [float(v) for v in seg_speeds_raw]
                except (TypeError, ValueError):
                    segment_speeds_mph = None

            if speed_mph <= 0 and segment_speeds_mph is None:
                _playback_lock.release()
                return jsonify({"error": "Route speed must be greater than 0 mph"}), 400

            try:
                device_udid, tunnel_address, tunnel_port = _tunnel.get_device_rsd(_active_udid)
            except (TunnelDownError, DeviceNotFoundError) as e:
                _playback_lock.release()
                return jsonify({"error": str(e)}), 503

            event_loop = _loop
            if event_loop is None:
                _playback_lock.release()
                return jsonify({"error": "Route event loop is not ready yet. Try again."}), 503

            _route_hold_final = bool(body.get("hold_final", True))
            _route_stop_requested = False

            async def run_route() -> None:
                last_coords: tuple[float, float] | None = None

                async def on_tick(lat: float, lng: float) -> None:
                    nonlocal last_coords
                    await route_location.set(lat, lng)
                    last_coords = (lat, lng)
                    with _location_lock:
                        _current_location.update({"lat": lat, "lng": lng, "spoof_active": True})
                    progress = _route_player.get_progress()
                    progress.update({"lat": lat, "lng": lng})
                    _push_sse("route_progress", progress)

                try:
                    _push_sse("status", {"route_running": True})
                    async with RouteLocationSession(tunnel_address, tunnel_port) as route_location:
                        if not _route_stop_requested:
                            await _route_player.play(
                                waypoints, speed_mph, loop_flag, on_tick,
                                segment_speeds_mph=segment_speeds_mph,
                            )
                except Exception as e:
                    _push_sse("route_error", {"error": str(e)})
                finally:
                    if last_coords is not None and _route_hold_final:
                        try:
                            await event_loop.run_in_executor(
                                None,
                                set_location,
                                last_coords[0],
                                last_coords[1],
                                device_udid,
                            )
                        except Exception as e:
                            _push_sse("route_error", {"error": str(e)})
                    try:
                        _playback_lock.release()
                    except RuntimeError:
                        pass
                    _push_sse("status", {"route_running": False})

            asyncio.run_coroutine_threadsafe(
                run_route(),
                event_loop,
            )
            return jsonify({"ok": True, "waypoints": len(waypoints), "speed_mph": speed_mph})

        except Exception as e:
            try:
                _playback_lock.release()
            except RuntimeError:
                pass
            return jsonify({"error": str(e)}), 500

    @app.route("/api/route/pause", methods=["POST"])
    def api_route_pause():
        # asyncio.Event is not thread-safe; dispatch via the event loop
        if _loop:
            _loop.call_soon_threadsafe(_route_player.pause)
        return jsonify({"ok": True})

    @app.route("/api/route/resume", methods=["POST"])
    def api_route_resume():
        if _loop:
            _loop.call_soon_threadsafe(_route_player.resume)
        return jsonify({"ok": True})

    @app.route("/api/route/stop", methods=["POST"])
    def api_route_stop():
        global _route_hold_final, _route_stop_requested

        body = request.get_json(force=True) or {}
        _route_stop_requested = True
        if body.get("clear_location"):
            _route_hold_final = False
        if _loop:
            _loop.call_soon_threadsafe(_route_player.stop)
        if body.get("clear_location"):
            try:
                device_udid, _, _ = _tunnel.get_device_rsd(_active_udid)
                clear_location(device_udid)
                with _location_lock:
                    _current_location.update({"lat": None, "lng": None, "spoof_active": False})
                _push_sse("status", {"spoof_active": False, "lat": None, "lng": None})
            except Exception:
                pass
        return jsonify({"ok": True})

    @app.route("/api/route/progress")
    def api_route_progress():
        return jsonify(_route_player.get_progress())

    # ------------------------------------------------------------------ motion

    @app.route("/api/motion/start", methods=["POST"])
    def api_motion_start():
        if not _playback_lock.acquire(blocking=False):
            return jsonify({"error": "A route or motion is already running. Stop it first."}), 409

        body = request.get_json(force=True) or {}
        pattern = body.get("pattern", "")
        if pattern not in ("walk", "orbit", "oscillate", "drift"):
            _playback_lock.release()
            return jsonify({"error": "pattern must be one of: walk, orbit, oscillate, drift"}), 400

        try:
            # Resolve center / endpoints
            if pattern in ("walk", "orbit", "drift"):
                center_data = body.get("center")
                if center_data:
                    try:
                        clat, clng = _parse_lat_lng(center_data, label="center")
                    except ValueError as e:
                        _playback_lock.release()
                        return jsonify({"error": str(e)}), 400
                else:
                    with _location_lock:
                        clat = _current_location.get("lat")
                        clng = _current_location.get("lng")
                    if clat is None or clng is None:
                        _playback_lock.release()
                        return jsonify({"error": "No current location; provide center coordinates"}), 400
            else:  # oscillate
                try:
                    lat1, lng1 = _parse_lat_lng(body.get("point_a") or {}, label="point_a")
                    lat2, lng2 = _parse_lat_lng(body.get("point_b") or {}, label="point_b")
                except ValueError as e:
                    _playback_lock.release()
                    return jsonify({"error": str(e)}), 400

            try:
                radius_m = float(body.get("radius_m", 200.0))
                speed_mph = float(body.get("speed_mph", 3.0))
                jitter = float(body.get("jitter", 0.5))
            except (TypeError, ValueError) as e:
                _playback_lock.release()
                return jsonify({"error": str(e)}), 400

            if speed_mph <= 0:
                _playback_lock.release()
                return jsonify({"error": "speed_mph must be > 0"}), 400
            if pattern in ("walk", "orbit") and radius_m <= 0:
                _playback_lock.release()
                return jsonify({"error": "radius_m must be > 0"}), 400

            try:
                device_udid, tunnel_address, tunnel_port = _tunnel.get_device_rsd(_active_udid)
            except (TunnelDownError, DeviceNotFoundError) as e:
                _playback_lock.release()
                return jsonify({"error": str(e)}), 503

            event_loop = _loop
            if event_loop is None:
                _playback_lock.release()
                return jsonify({"error": "Event loop not ready. Try again."}), 503

            async def run_motion() -> None:
                last_coords: tuple[float, float] | None = None

                async def on_tick(lat: float, lng: float) -> None:
                    nonlocal last_coords
                    await motion_loc.set(lat, lng)
                    last_coords = (lat, lng)
                    with _location_lock:
                        _current_location.update({"lat": lat, "lng": lng, "spoof_active": True})
                    state = _motion_player.get_state()
                    state.update({"lat": lat, "lng": lng})
                    _push_sse("motion_tick", state)

                try:
                    _push_sse("status", {"motion_running": True})
                    async with RouteLocationSession(tunnel_address, tunnel_port) as motion_loc:
                        if pattern == "walk":
                            await _motion_player.run_walk(
                                clat, clng, radius_m, speed_mph, jitter, on_tick
                            )
                        elif pattern == "orbit":
                            await _motion_player.run_orbit(
                                clat, clng, radius_m, speed_mph, on_tick
                            )
                        elif pattern == "oscillate":
                            await _motion_player.run_oscillate(
                                lat1, lng1, lat2, lng2, speed_mph, on_tick
                            )
                        elif pattern == "drift":
                            await _motion_player.run_drift(clat, clng, speed_mph, on_tick)
                except Exception as e:
                    _push_sse("motion_error", {"error": str(e)})
                finally:
                    if last_coords is not None:
                        try:
                            await event_loop.run_in_executor(
                                None, set_location, last_coords[0], last_coords[1], device_udid
                            )
                        except Exception:
                            pass
                    try:
                        _playback_lock.release()
                    except RuntimeError:
                        pass
                    _push_sse("status", {"motion_running": False})

            asyncio.run_coroutine_threadsafe(run_motion(), event_loop)
            return jsonify({"ok": True, "pattern": pattern})

        except Exception as e:
            try:
                _playback_lock.release()
            except RuntimeError:
                pass
            return jsonify({"error": str(e)}), 500

    @app.route("/api/motion/stop", methods=["POST"])
    def api_motion_stop():
        if _loop:
            _loop.call_soon_threadsafe(_motion_player.stop)
        return jsonify({"ok": True})

    @app.route("/api/motion/status")
    def api_motion_status():
        return jsonify({
            "running": _motion_player.is_running(),
            "state": _motion_player.get_state(),
        })

    @app.route("/api/geocode")
    def api_geocode():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"error": "Missing query parameter q"}), 400
        try:
            lat, lng, display_name = _get_geocoder().geocode(q)
            return jsonify({"lat": lat, "lng": lng, "display_name": display_name})
        except GeocodeError as e:
            return jsonify({"error": str(e)}), 404

    @app.route("/api/geocode/suggest")
    def api_geocode_suggest():
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify([])
        viewbox = None
        try:
            from geopy.point import Point as GeoPoint
            prox_lat = float(request.args["lat"])
            prox_lng = float(request.args["lng"])
            delta = 0.5
            viewbox = [
                GeoPoint(prox_lat + delta, prox_lng - delta),
                GeoPoint(prox_lat - delta, prox_lng + delta),
            ]
        except (KeyError, ValueError, TypeError):
            pass
        results = _get_geocoder().suggest(q, viewbox=viewbox)
        return jsonify([{"lat": lat, "lng": lng, "display_name": dn} for lat, lng, dn in results])

    @app.route("/events")
    def sse_events():
        def generate() -> Generator[str, None, None]:
            local_q: queue.Queue = queue.Queue(maxsize=50)
            with _sse_lock:
                _sse_listeners.append(local_q)
            try:
                yield _format_sse("status", {"connected": True})
                while True:
                    try:
                        event_type, data = local_q.get(timeout=15)
                        yield _format_sse(event_type, data)
                    except queue.Empty:
                        yield ": heartbeat\n\n"
            finally:
                with _sse_lock:
                    try:
                        _sse_listeners.remove(local_q)
                    except ValueError:
                        pass

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def run_server(
    host: str,
    port: int,
    udid: Optional[str],
    open_browser: bool,
) -> None:
    import logging

    app = create_app(udid)
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host=host, port=port, threaded=True, use_reloader=False)
