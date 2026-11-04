"""Driving by the floor map, in the simulated two-room flat (test_lidarmap) with the docking world's dock in it
(test_docking): the robot's lidar, gyro and wheels are simulated, the localizer follows it, the AI driver drives."""

import asyncio
import math
import time

import numpy as np
import pytest

from beambrain import dock
from beambrain import lidarmap as lm
from beambrain.driver import AIDriver
from beambrain.localize import Localizer
from beambrain.navigate import CLEAR_M, NEAR_ENDS_CLEAR_M, Planner, go_to
from test_docking import FakeBrain, World, _teach_then, fake_cameras  # noqa: F401  (fake_cameras is a fixture)
from test_lidarmap import SEGS, drive, lidar
from test_localize import ROUND

SPEED = 3.0                         # simulated time runs this much faster than real time
DOCK_X = dock.AXLE_TO_SHEET_DOCKED_M   # the docking world has the sheet at x = 0: the map's x + this


@pytest.fixture(scope="module")
def flat():
    rows, _ = drive(ROUND, np.random.default_rng(0))
    b = lm.build(rows)
    return b.grid, b.dock


def _dist(p, segs):
    a, b = segs[:, 0], segs[:, 1]
    t = np.clip(((p[0] - a[:, 0]) * (b[:, 0] - a[:, 0]) + (p[1] - a[:, 1]) * (b[:, 1] - a[:, 1]))
                / ((b - a) ** 2).sum(1), 0, 1)
    return float(np.hypot(a[:, 0] + t * (b[:, 0] - a[:, 0]) - p[0], a[:, 1] + t * (b[:, 1] - a[:, 1]) - p[1]).min())


def _crosses(p, q, segs):
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return any(side(p, q, a) * side(p, q, b) < 0 and side(a, b, p) * side(a, b, q) < 0 for a, b in segs)


def box(x, y, half):
    c = [(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half)]
    return [(c[k], c[(k + 1) % 4]) for k in range(4)]


class FlatWorld(World):
    """The docking world, placed in the flat: map coordinates in, the dock's sheet behind the map's origin."""

    def __init__(self, x, y, yaw_deg, extra=(), **kw):
        super().__init__(x + DOCK_X, y, yaw_deg, **kw)
        self.segs = np.concatenate([SEGS, np.array(extra, float)]) if extra else SEGS
        self.closest = math.inf
        self.track = []

    def map_pose(self):
        return self.x - DOCK_X, self.y, self.yaw

    def head_sighting(self):
        x, y, _ = self.map_pose()
        if math.hypot(x + DOCK_X, y) > 4 or _crosses((x, y), (-DOCK_X, 0.0), self.segs):
            return None                                       # behind a wall, or too far to read
        return super().head_sighting()


class NavBrain(FakeBrain):
    """The fake brain from the docking tests, plus the lidar, the server's obstacle stop, a gyro reading turns
    3.5 % short, and the localizer fed like the real brain feeds it."""

    def __init__(self, world, localizer):
        super().__init__(world)
        self.localizer = localizer
        self.rng = np.random.default_rng(4)
        self.cmd = (0.0, 0.0)
        self.turn = 0
        self.t = 0.0

    async def send(self, m):
        self.sent.append(m)
        if m.get("type") == "drive":
            self.cmd = (m["linear"], m["angular"])

    async def physics(self):
        w, loc = self.w, self.localizer
        while True:
            await asyncio.sleep(0.02)
            pose = w.map_pose()
            cm = lidar(pose, self.rng, segs=w.segs)
            pts = lm.scan_points(cm)
            ahead = (pts[:, 0] > 0.095) & (pts[:, 0] < 0.4) & (np.abs(pts[:, 1]) < 0.25)
            w.lin, w.ang = (0.0 if self.cmd[0] > 0 and ahead.any() else self.cmd[0]), self.cmd[1]
            w.step(0.02 * SPEED)
            self.t += 0.02 * SPEED
            if w.docked() and w.lin < 0:
                w.lin = w.ang = 0.0
            w.closest = min(w.closest, _dist(w.map_pose()[:2], w.segs))
            w.track.append(w.map_pose())
            self.turn += 1
            self.telemetry = {"drive": {}, "board": {
                "yaw_deg": math.degrees(w.yaw) * 0.965 % 360, "odometry_m": w.odo,
                "actual_linear": w.lin, "actual_angular": w.ang, "charging": w.docked()},
                "scan": {"turn": self.turn, "cm": cm}}
            loc.feed(self.telemetry, self.t)
            job = loc.take()
            if job is not None:
                loc.apply(job, loc.match(job))
            now = time.monotonic()
            self.frames["head"] = ("head", now)
            if any(m.get("type") == "rear_feed" and m["on"] for m in self.sent):
                self.frames["rear"] = ("rear", now)


async def run(world, flat, job):
    grid, dock_pose = flat
    loc = Localizer(grid, dock_pose)
    loc.pose, loc.state = world.map_pose(), "here"              # tracked there (test_localize covers that)
    brain = NavBrain(world, loc)
    d = AIDriver(brain, type("M", (), {"model": "test"})(), "test", turn_speed=0.4, drive_speed=0.2)
    sim = asyncio.create_task(brain.physics())
    try:
        await asyncio.sleep(0.1)
        return await job(brain, d), brain
    finally:
        sim.cancel()


def test_plans_through_the_doorway_away_from_walls(flat):
    grid, _ = flat
    p = Planner(grid)
    route = p.plan((6.0, 2.5), (1.0, -0.5))
    assert not isinstance(route, str)
    assert any(abs(x - 4.0) < 0.1 and 0.2 < y < 1.2 for x, y in route)      # through the door
    middle = [pt for pt in route if math.dist(pt, route[0]) > 0.6 and math.dist(pt, route[-1]) > 0.6]
    assert min(p.clearance(*pt) for pt in middle) >= CLEAR_M - 0.05
    assert min(p.clearance(*pt) for pt in route) >= NEAR_ENDS_CLEAR_M
    assert isinstance(p.plan((6.0, 2.5), (20.0, 0.0)), str)                  # off the map: says so


def test_drives_to_the_other_room(flat):
    w = FlatWorld(0.8, -0.5, 90)
    planner = Planner(flat[0])
    result, _ = asyncio.run(run(w, flat, lambda brain, d: go_to(brain, d, planner, (5.5, 2.0, math.pi / 2))))
    x, y, th = w.map_pose()
    assert result is None
    assert math.hypot(x - 5.5, y - 2.0) < 0.12 and abs(math.degrees(th) % 360 - 90) < 6
    assert w.closest > 0.22


def test_goes_round_something_not_on_the_map(flat):
    planner = Planner(flat[0])
    route = planner.plan((0.8, -0.5), (5.5, 2.0))
    bx, by = route[len(route) // 4]                                          # a box on the way
    w = FlatWorld(0.8, -0.5, 90, extra=box(bx, by, 0.15))
    result, _ = asyncio.run(run(w, flat, lambda brain, d: go_to(brain, d, planner, (5.5, 2.0, None))))
    assert result is None
    assert math.hypot(w.map_pose()[0] - 5.5, w.map_pose()[1] - 2.0) < 0.12
    assert w.closest > 0.15


def test_docks_from_the_other_room(fake_cameras, flat, tmp_path):
    result, _ = asyncio.run(_teach_then(fake_cameras, tmp_path, {}))
    assert "docked" in result
    w = FlatWorld(5.5, 2.5, 90)                                              # in the far room, facing away
    fake_cameras["w"] = w
    assert w.head_sighting() is None

    async def docking(brain, d):
        return await dock.Docking(brain, d, tmp_path / "dock.json").run()
    result, brain = asyncio.run(run(w, flat, docking))
    assert result == "docked: on the charger"
    assert abs(w.y) < 0.03 and abs((math.degrees(w.yaw) + 180) % 360 - 180) < 4
    assert any("by the map" in str(m) for m in brain.sent)                  # labelled: what the AI is doing


def test_from_the_charger_it_drives_straight_off_before_turning(flat):
    w = FlatWorld(0.0, 0.0, 0)                                               # docked
    assert w.docked()
    planner = Planner(flat[0])
    result, _ = asyncio.run(run(w, flat, lambda brain, d: go_to(brain, d, planner, (0.8, -1.0, None))))
    assert result is None
    turned_at = next(x for x, y, th in w.track if abs(th) > math.radians(5))
    assert turned_at > 0.3                                                   # 30+ cm off the contacts first


def test_held_short_by_a_counter_it_still_turns_to_the_saved_way(flat):
    """A spot by a counter: the obstacle stop holds it a few cm short. It used to re-plan three times and give
    up without turning round; now it finishes from there."""
    planner = Planner(flat[0])
    route = planner.plan((0.8, -0.5), (5.5, 2.0))
    (ax, ay), (gx, gy) = route[-6], route[-1]
    ux, uy = (gx - ax) / math.hypot(gx - ax, gy - ay), (gy - ay) / math.hypot(gx - ax, gy - ay)
    counter = box(gx + 0.45 * ux, gy + 0.45 * uy, 0.15)                      # just past the spot, the way it comes
    w = FlatWorld(0.8, -0.5, 90, extra=counter)
    back = math.atan2(-uy, -ux)                                              # saved facing back the way it came
    result, _ = asyncio.run(run(w, flat, lambda brain, d: go_to(brain, d, planner, (5.5, 2.0, back))))
    x, y, th = w.map_pose()
    assert result is None
    assert math.hypot(x - 5.5, y - 2.0) < 0.12
    assert abs((math.degrees(th - back) + 180) % 360 - 180) < 10        # the fake gyro reads turns 3.5 % short
