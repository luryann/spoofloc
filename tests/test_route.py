import math
import pytest
from spoofloc.route import haversine_m, lerp, RoutePlayer


def test_haversine_same_point():
    assert haversine_m(37.0, -122.0, 37.0, -122.0) == pytest.approx(0.0, abs=1e-3)


def test_haversine_known_distance():
    # SF to LA is roughly 559 km
    d = haversine_m(37.7749, -122.4194, 34.0522, -118.2437)
    assert 550_000 < d < 570_000


def test_lerp_endpoints():
    assert lerp(0.0, 10.0, 0.0) == pytest.approx(0.0)
    assert lerp(0.0, 10.0, 1.0) == pytest.approx(10.0)
    assert lerp(0.0, 10.0, 0.5) == pytest.approx(5.0)


def test_lerp_negative():
    assert lerp(-10.0, 10.0, 0.5) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_route_player_basic():
    player = RoutePlayer()
    waypoints = [(0.0, 0.0), (0.1, 0.0), (0.2, 0.0)]
    ticks = []

    async def on_tick(lat, lng):
        ticks.append((lat, lng))
        if len(ticks) >= 3:
            player.stop()

    await player.play(waypoints, speed_mph=1000.0, loop=False, on_tick=on_tick)
    assert len(ticks) >= 1
    # First tick starts at segment 0
    assert ticks[0][0] == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_route_player_stop():
    player = RoutePlayer()
    waypoints = [(0.0, 0.0), (1.0, 0.0)]
    ticks = []

    async def on_tick(lat, lng):
        ticks.append((lat, lng))
        player.stop()

    await player.play(waypoints, speed_mph=1.0, loop=False, on_tick=on_tick)
    assert len(ticks) == 1
    assert not player.is_running()


@pytest.mark.asyncio
async def test_route_player_emits_final_waypoint_for_short_route():
    player = RoutePlayer()
    waypoints = [(0.0, 0.0), (0.00001, 0.0)]
    ticks = []

    async def on_tick(lat, lng):
        ticks.append((lat, lng))

    await player.play(waypoints, speed_mph=100.0, loop=False, on_tick=on_tick)

    assert ticks[0] == pytest.approx((0.0, 0.0))
    assert ticks[-1] == pytest.approx(waypoints[-1])
    assert len(ticks) == 2
    assert player.get_progress()["fraction"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_route_player_propagates_tick_errors():
    player = RoutePlayer()

    async def on_tick(lat, lng):
        raise RuntimeError("location update failed")

    with pytest.raises(RuntimeError, match="location update failed"):
        await player.play([(0.0, 0.0), (1.0, 0.0)], speed_mph=50.0, loop=False, on_tick=on_tick)

    assert not player.is_running()
    assert player.get_progress()["error"] == "location update failed"


@pytest.mark.asyncio
async def test_route_player_progress_is_current_during_tick():
    player = RoutePlayer()
    seen_progress = []

    async def on_tick(lat, lng):
        seen_progress.append(player.get_progress())
        player.stop()

    await player.play([(0.0, 0.0), (1.0, 0.0)], speed_mph=50.0, loop=False, on_tick=on_tick)

    assert seen_progress[0]["ticks"] == 1
    assert seen_progress[0]["lat"] == pytest.approx(0.0)
    assert seen_progress[0]["lng"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_route_player_rejects_non_positive_speed():
    player = RoutePlayer()

    async def on_tick(lat, lng):
        pass

    from spoofloc.exceptions import RouteError

    with pytest.raises(RouteError, match="speed"):
        await player.play([(0.0, 0.0), (1.0, 0.0)], speed_mph=0.0, loop=False, on_tick=on_tick)


@pytest.mark.asyncio
async def test_route_player_too_few_waypoints():
    player = RoutePlayer()
    from spoofloc.exceptions import RouteError

    async def on_tick(lat, lng):
        pass

    with pytest.raises(RouteError):
        await player.play([(0.0, 0.0)], speed_mph=50.0, loop=False, on_tick=on_tick)
