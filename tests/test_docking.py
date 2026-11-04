"""Docking end to end in a tiny simulated world: the robot's moves, the head camera's sighting and the rear
camera's view of the markers are computed from where the simulated robot really is."""

import asyncio
import json
import math
import time

import numpy as np
import pytest

from beambrain import dock
from beambrain.driver import AIDriver
from test_dock import project_sheet

SPEED = 2.0                  # simulated time runs this much faster than real time
SHEET = np.array([0.0, 0.0])  # the sheet's middle; it faces +x. Docked: axle at x = 0.355, facing +x (backed in)


class World:
    def __init__(self, x, y, yaw_deg, rear_cam_left=0.12, rear_cam_ahead=0.05, rear_mount_deg=8.0, rear_noise_px=0.3,
                 head_error=(0.0, 0.0), head_noise_deg=0.0):
        self.x, self.y, self.yaw = x, y, math.radians(yaw_deg)
        self.lin = self.ang = 0.0
        self.odo = 0.0
        # The phone: its camera this far left of the robot's centre line and ahead of the axle, 0.9 m up, tilted
        # down 25 degrees (like ours: docked, the small markers show near the bottom of the picture), looking this
        # many degrees off straight back; its corners a little noisy.
        self.rear_cam = (rear_cam_left, rear_cam_ahead, math.radians(rear_mount_deg))
        self.rear_noise_px = rear_noise_px
        self.head_error = head_error          # the head camera off by (degrees, metres sideways)
        self.head_noise_deg = head_noise_deg  # ... and its reading of the sheet's angle jumping this much look to look
        self.rng = np.random.default_rng(1)

    def step(self, dt):
        self.yaw += self.ang * dt
        self.x += self.lin * dt * math.cos(self.yaw)
        self.y += self.lin * dt * math.sin(self.yaw)
        self.odo += self.lin * dt

    def docked(self):
        # Contacts touch when the axle reaches 0.355 m from the sheet, within 3 cm of its middle line.
        return self.x <= dock.AXLE_TO_SHEET_DOCKED_M and abs(self.y) < 0.03

    def head_sighting(self):
        """What the head camera works out: the sheet's middle and its turn, in the robot's frame."""
        dx, dy = SHEET[0] - self.x, SHEET[1] - self.y
        c, s = math.cos(-self.yaw), math.sin(-self.yaw)
        ahead, left = c * dx - s * dy, s * dx + c * dy
        bearing = math.degrees(math.atan2(left, ahead))
        if ahead < 0.5 or abs(bearing) > 49:                 # out of the picture
            return None
        if abs(bearing) > 35:                                # at its edge: a bad fit, far out (like run 3)
            return dock.Sighting(ahead * 0.6, left * 1.5, -bearing, [20], 42.0, bearing)
        # The sheet faces +x, so the robot faces it square on with yaw = 180 degrees.
        facing = math.degrees((math.pi - self.yaw + math.pi) % (2 * math.pi) - math.pi)
        facing += self.head_error[0] + self.rng.normal(0, self.head_noise_deg)
        return dock.Sighting(ahead, left + self.head_error[1], facing, [20, 21, 22], 0.5, bearing)

    def rear_markers(self):
        """The dock markers the phone's rear camera sees (640 x 480, focal length 450 px), from where it really is."""
        left, ahead, mount = self.rear_cam
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        cam = (self.x + ahead * c - left * s, self.y + ahead * s + left * c)
        found = project_sheet(cam, 0.9, self.yaw + math.pi + mount, pitch_deg=25)
        # Markers under about 14 px across aren't read (the small ones, from further than about 1.6 m).
        return {i: p + self.rng.normal(0, self.rear_noise_px, p.shape) for i, p in found.items()
                if np.linalg.norm(p[1] - p[0]) >= 14}


class FakeBrain:
    def __init__(self, world):
        self.w = world
        self.frames, self.telemetry, self.sent = {}, {}, []
        self.human_took_over = asyncio.Event()

    async def send(self, m):
        self.sent.append(m)
        if m.get("type") == "drive":
            self.w.lin, self.w.ang = m["linear"], m["angular"]

    async def physics(self):
        while True:
            await asyncio.sleep(0.02)
            self.w.step(0.02 * SPEED)
            if self.w.docked() and self.w.lin < 0:       # backed onto the contacts: stopped there
                self.w.lin = self.w.ang = 0.0
            self.telemetry = {"drive": {}, "board": {
                "yaw_deg": math.degrees(self.w.yaw), "odometry_m": self.w.odo,
                "actual_linear": self.w.lin, "actual_angular": self.w.ang, "charging": self.w.docked()}}
            now = time.monotonic()
            self.frames["head"] = ("head", now)
            if any(m.get("type") == "rear_feed" and m["on"] for m in self.sent):
                self.frames["rear"] = ("rear", now)


@pytest.fixture
def fake_cameras(monkeypatch):
    holder = {}
    picture = np.zeros((480, 640, 3), np.uint8)       # only its size is used: the markers come from the world
    monkeypatch.setattr(dock, "decode", lambda jpeg: picture)
    monkeypatch.setattr(dock, "sight_dock", lambda im, cam: holder["w"].head_sighting())
    monkeypatch.setattr(dock, "find_markers", lambda im: holder["w"].rear_markers())
    return holder


async def run_docking(world, holder, tmp_path, teach=False):
    holder["w"] = world
    brain = FakeBrain(world)
    model = type("M", (), {"model": "docking"})()
    d = AIDriver(brain, model, "docking", turn_speed=0.4, drive_speed=0.2)
    docking = dock.Docking(brain, d, tmp_path / "dock.json")
    sim = asyncio.create_task(brain.physics())
    try:
        await asyncio.sleep(0.1)
        return await (docking.teach() if teach else docking.run()), brain
    finally:
        sim.cancel()


@pytest.mark.parametrize("start, phone", [
    ((1.8, 0.4, 170), {}),                                          # off to one side, a little turned
    ((1.6, -0.3, 200), {"rear_cam_left": -0.08, "rear_mount_deg": -10}),  # phone on the other side, turned the other way
    ((1.8, 0.2, 180), {"head_error": (3.0, 0.03)}),                 # head camera a bit off: the rear one fixes it
    ((1.75, -0.45, 165), {"head_noise_deg": 6.0}),                  # well to the side, the sheet's angle jumpy
    ((1.6, 0.0, 180), {"head_error": (6.0, 0.0)}),                  # the docked heading estimate 6 degrees off
    ((1.8, -0.3, 165), {"head_error": (10.0, 0.18)}),               # like run 2: head camera 10 deg and 18 cm out
    ((1.6, 0.1, 220), {}),                                          # turned 40 deg: the dock at the picture's edge
    ((1.9, 0.05, 210), {"head_error": (30.0, 0.0)}),                # like run 3: turned 30, head's angle 30 deg out
    ((1.9, 0.5, 190), {}),                                          # half a metre off the centre line
])
def test_docks_from_in_front_of_the_dock(fake_cameras, tmp_path, start, phone):
    result, _ = asyncio.run(_teach_then(fake_cameras, tmp_path, phone))
    assert result.startswith("taught the docked rear view; backing on again: docked")
    w = World(*start, **phone)
    result, brain = asyncio.run(run_docking(w, fake_cameras, tmp_path))
    assert result == "docked: on the charger"
    # Centred and square on the contacts.
    assert abs(w.y) < 0.03 and abs((math.degrees(w.yaw) + 180) % 360 - 180) < 3
    assert any(m.get("type") == "rear_feed" and m["on"] for m in brain.sent)


async def _teach_then(holder, tmp_path, phone):
    w = World(dock.AXLE_TO_SHEET_DOCKED_M, 0.01, 2, **{k: v for k, v in phone.items() if not k.startswith("head_")})
    return await run_docking(w, holder, tmp_path, teach=True)


def test_wont_dock_untaught(fake_cameras, tmp_path):
    result, _ = asyncio.run(run_docking(World(1.8, 0.0, 180), fake_cameras, tmp_path))
    assert result.startswith("not taught yet")


def test_says_so_when_the_dock_is_out_of_sight(fake_cameras, tmp_path):
    (tmp_path / "dock.json").write_text(json.dumps({"rear_f": 450, "docked_view": [8, 0.12, 0.4]}))
    result, _ = asyncio.run(run_docking(World(1.8, 0.0, 0), fake_cameras, tmp_path))
    assert result.startswith("can't see the dock marker with the head camera")


def test_jumpy_angle_readings_still_end_square(fake_cameras, tmp_path):
    """Run 2 on the real robot docked 10-15 degrees crooked: the sheet's angle read differently on every look."""
    phone = {"head_noise_deg": 8.0}
    asyncio.run(_teach_then(fake_cameras, tmp_path, phone))
    for seed, start in enumerate([(1.75, -0.45, 165), (1.6, 0.4, 200), (1.5, -0.2, 175)]):
        w = World(*start, **phone)
        w.rng = np.random.default_rng(10 + seed)
        result, _ = asyncio.run(run_docking(w, fake_cameras, tmp_path))
        assert result == "docked: on the charger"
        assert abs((math.degrees(w.yaw) + 180) % 360 - 180) < 5, start


@pytest.mark.parametrize("off", [0.17, 0.23])
def test_no_room_in_front_to_line_up_it_backs_in_from_where_it_is(fake_cameras, tmp_path, monkeypatch, off):
    """Real run: turned round 15.8 cm off the line, it tried to drive out onto the line, something was in front,
    and it gave up. Within LINEUP_BLOCKED_M it now backs in angling onto the line."""
    asyncio.run(_teach_then(fake_cameras, tmp_path, {}))
    real_drive = AIDriver.drive

    async def no_room_ahead(self, metres):                          # forward is blocked; backing is not
        if metres > 0 and any(m.get("type") == "rear_feed" for m in self.brain.sent):
            return "blocked", True
        return await real_drive(self, metres)
    monkeypatch.setattr(AIDriver, "drive", no_room_ahead)
    w = World(1.7, off, 180)
    result, _ = asyncio.run(run_docking(w, fake_cameras, tmp_path))
    assert result == "docked: on the charger"
    assert abs(w.y) < 0.03 and abs((math.degrees(w.yaw) + 180) % 360 - 180) < 3
