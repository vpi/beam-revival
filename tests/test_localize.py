"""Finding the robot on its map: the simulated flat from test_lidarmap, mapped once, then driven again."""

import math

import numpy as np
import pytest

from beambrain import lidarmap as lm
from beambrain import localize as L

from test_lidarmap import drive, lidar

ROUND = [(2.5, 0), (3.5, 0.0), (5.5, 0.7), (6.0, 2.5), (5.0, 2.8), (5.5, 0.6), (3.0, -0.6), (0.5, -0.8), (0.0, 0.0)]


@pytest.fixture(scope="module")
def flat():
    rows, _ = drive(ROUND, np.random.default_rng(0))
    b = lm.build(rows)
    return b.grid, b.dock


def telemetry(i, r, charging=None):
    return {"board": {"yaw_deg": r["yaw"], "odometry_m": r["odo"], "actual_angular": r["ang"],
                      "charging": r["charging"] if charging is None else charging},
            "scan": {"turn": i, "cm": r["cm"]}}


def run(loc, rows, charging=None, t0=0.0):
    poses = []
    for i, r in enumerate(rows):
        loc.feed(telemetry(i, r, charging), t0 + i * 0.5)
        job = loc.take()
        if job is not None:
            loc.apply(job, loc.match(job))
        poses.append((loc.state, loc.pose))
    return poses


def test_tracks_a_new_drive_from_the_dock(flat):
    grid, dock = flat
    # Another day: a different route, the gyro drifting the other way.
    rows, truth = drive([(1.5, -0.8), (3.6, 0.5), (5.2, 1.0), (6.2, 3.0), (4.6, 0.6), (2.0, 0.3), (0.0, 0.0)],
                        np.random.default_rng(5), odo_scale=0.97, gyro_drift_deg_per_m=-1.5)
    poses = run(L.Localizer(grid, dock), rows)
    errs = [math.hypot(p[0] - t[0], p[1] - t[1]) for (s, p), t in zip(poses, truth)]
    assert all(s == "here" for s, _ in poses)
    assert max(errs) < 0.08 and np.median(errs) < 0.05                # the map is in 5 cm squares


def test_finds_itself_anywhere_and_never_guesses_wrong(flat):
    grid, _ = flat
    rng = np.random.default_rng(3)
    right = 0
    spots = [(0.5, 0.0), (2.5, -0.8), (3.5, 0.3), (5.0, 0.0), (6.0, 2.0), (5.0, 3.0), (1.0, 1.0), (5.5, -1.0)]
    for x, y in spots:
        th = rng.uniform(-math.pi, math.pi)
        found = L.find(grid, lm.scan_points(lidar((x, y, th), rng)))
        if found is None:
            continue                                              # not sure is fine; wrong isn't
        (fx, fy, fth), _ = found
        assert math.hypot(fx - x, fy - y) < 0.15 and abs(L._wrap(fth - th)) < math.radians(4)
        right += 1
    assert right >= 6


def test_carried_somewhere_it_notices_and_finds_itself_again(flat):
    grid, dock = flat
    loc = L.Localizer(grid, dock)
    rows, truth = drive([(5.5, 1.0), (6.0, 2.5)], np.random.default_rng(7))
    # It believes it's 1 m to the side and turned (picked up and put down): the scans stop fitting.
    run(loc, rows[:1])
    loc.pose = (loc.pose[0] + 0.0, loc.pose[1] + 1.0, loc.pose[2] + 0.6)
    poses = run(loc, rows[1:], charging=False, t0=1.0)
    states = [s for s, _ in poses]
    assert any(s != "here" for s in states)                   # it noticed: unsure, or lost
    last_state, last = poses[-1]
    assert last_state == "here" and math.hypot(last[0] - truth[-1][0], last[1] - truth[-1][1]) < 0.08


def test_on_the_charger_it_is_on_the_dock(flat):
    grid, dock = flat
    loc = L.Localizer(grid, dock)
    loc.feed({"board": {"yaw_deg": 123.0, "odometry_m": 4.0, "charging": True}}, 0.0)
    assert loc.state == "here" and loc.pose == dock
    assert loc.snapshot()["x"] == pytest.approx(dock[0], abs=1e-3)


def test_grid_round_trip(flat, tmp_path):
    grid, dock = flat
    b = lm.Built(grid, dock=dock)
    lm.save_grid(b, tmp_path / "map.npz")
    g, d = lm.load_grid(tmp_path / "map.npz")
    assert d == pytest.approx(dock) and np.array_equal(g.lo, grid.lo)


async def test_brain_tells_the_driving_pages_where_it_is(tmp_path):
    import asyncio

    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore

    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    sent = []
    brain.send = lambda m: asyncio.sleep(0, sent.append(m))
    brain.maps_dir = tmp_path
    rows, _ = drive(ROUND[:4], np.random.default_rng(2))
    brain.telemetry = {"board": {"charging": True}, "scan": {"turn": 0, "cm": rows[0]["cm"]}}
    await brain.on_command({"kind": "map_start"})
    for i, r in enumerate(rows):
        brain.mapping.feed(telemetry(i, r))
    await brain.on_command({"kind": "map_stop"})
    assert (tmp_path / "map.npz").exists() and brain.localizer is not None
    for i, r in enumerate(rows[:20]):
        await brain.localize(telemetry(i, r))
        if brain._localizing is not None:
            await brain._localizing
    poses = [s for s in sent if s.get("type") == "map_pose"]
    assert poses and poses[-1]["state"] == "here" and "x" in poses[-1]


def test_a_wrong_start_that_still_fits_a_little_gets_corrected(flat, monkeypatch):
    grid, dock = flat
    monkeypatch.setattr(L, "GOOD_FIT", 0.0)                      # tracking accepts whatever it gets
    loc = L.Localizer(grid, dock)
    rows, truth = drive([(2.5, 0.0), (3.5, 0.3)], np.random.default_rng(9))
    run(loc, rows[:1])
    loc.pose = (loc.pose[0] + 1.0, loc.pose[1] - 0.6, loc.pose[2] + 0.4)    # found itself in the wrong place
    run(loc, rows[1:], charging=False, t0=1.0)
    assert math.hypot(loc.pose[0] - truth[-1][0], loc.pose[1] - truth[-1][1]) < 0.08
