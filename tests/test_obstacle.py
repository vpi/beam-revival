"""Obstacle stop: forward driving slows and stops for what the lidar and the ultrasonic sensor see ahead."""

import math
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from beamctl.lidar import Lidar
from beamctl.rangefinder import RangeSensor
from beamctl.server import PRIORITY_BRAIN, Config, MotorController, build_app
from beamctl.sim import SimulatedBoard

ME, OTHER, AI = object(), object(), object()


class FakeRange:
    lost = False

    def __init__(self, m, fresh=True):
        self.m, self.fresh = m, fresh

    def snapshot(self):
        return {"m": self.m if self.fresh else None, "fresh": self.fresh}

    def readings(self):
        return [{**self.snapshot(), "angle": 0.0}]

    def start(self):
        pass

    async def stop(self):
        pass


class FakeLidar:
    present, lost = True, False

    def __init__(self, points, fresh=True):
        self.points, self.fresh, self.at = points, fresh, time.monotonic()

    def snapshot(self):
        return {"fresh": self.fresh}


def ctrl(rng=None, lid=None) -> MotorController:
    """The distances these tests were written for (the defaults until 2026-11-10: see the test below)."""
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None,
                               obstacle_stop_m=0.25, obstacle_slow_m=0.80, robot_half_width_m=0.25))
    c.range, c.lidar = rng, lid
    return c


def forward(c, owner=ME, linear=0.6, priority=1) -> float:
    assert c.set_target(linear, 0.0, owner, priority)
    return c._effective_target(time.monotonic())[0]


def test_without_sensors_nothing_changes():
    c = ctrl()
    assert forward(c) == 0.6
    assert c.ahead()["sensors"] is False


def test_slows_then_stops_for_the_sensor():
    assert forward(ctrl(FakeRange(1.5))) == 0.6
    assert forward(ctrl(FakeRange(0.5))) == pytest.approx(0.6 * 0.25 / 0.55, abs=0.01)
    assert forward(ctrl(FakeRange(0.2))) == 0.0
    assert forward(ctrl(FakeRange(0.26))) == 0.0          # a few mm/s would be allowed: that's a stop


def test_backing_away_and_turning_stay_free():
    c = ctrl(FakeRange(0.1))
    assert c.set_target(-0.3, 0.8, ME)
    assert c._effective_target(time.monotonic()) == (-0.3, 0.8)


def test_lidar_counts_only_whats_in_the_path():
    # 0.26 m from the lidar straight ahead is 0.2 m in front of the robot (it sits 6 cm back): stop.
    assert forward(ctrl(lid=FakeLidar([(0.0, 0.26)]))) == 0.0
    # Beside the robot (0.3 m to the right) isn't in its way.
    assert forward(ctrl(lid=FakeLidar([(90.0, 0.3)]))) == 0.6
    # Ahead and a little to the left, within the robot's width: in the way.
    a = math.degrees(math.atan2(-0.2, 0.26))
    assert forward(ctrl(lid=FakeLidar([(a % 360, math.hypot(0.26, 0.2))]))) == 0.0


def test_the_nearest_of_both_sensors_wins():
    c = ctrl(FakeRange(1.5), FakeLidar([(0.0, 0.26)]))
    assert forward(c) == 0.0 and c.ahead()["by"] == "lidar"


class FakeRanges(FakeRange):
    """Several distance sensors (corner ones too)."""

    def __init__(self, *sensors):
        self.sensors = sensors

    def readings(self):
        return [r for s in self.sensors for r in s.readings()]


def test_the_nearest_of_several_distance_sensors_wins():
    assert forward(ctrl(FakeRanges(FakeRange(1.5), FakeRange(0.2), FakeRange(2.0)))) == 0.0
    assert forward(ctrl(FakeRanges(FakeRange(1.5), FakeRange(None)))) == 0.6
    assert forward(ctrl(FakeRanges(FakeRange(1.5), FakeRange(None, fresh=False)))) == pytest.approx(0.10)


def test_a_silent_sensor_means_crawling():
    assert forward(ctrl(FakeRange(None, fresh=False))) == pytest.approx(0.10)
    assert forward(ctrl(lid=FakeLidar([], fresh=False))) == pytest.approx(0.10)


class OpenPort:
    is_open = True


def test_a_lost_sensor_means_crawling_until_it_is_back():
    rng = RangeSensor()                                       # "auto", not found yet: as if there were none
    c = ctrl(rng)
    assert forward(c) == 0.6 and not c.ahead()["sensors"]
    rng.expected = True                                       # it was seen, then unplugged (or a USB reset)
    c.drive.last_input = 0.0
    assert forward(c) == pytest.approx(0.10)
    a = c.ahead()
    assert a["lost"] and a["blind"] and a["sensors"]
    rng.port, rng.mm, rng.at = OpenPort(), 2000, time.monotonic()    # back, and the way is clear
    c.drive.last_input = 0.0
    assert forward(c) == 0.6 and not c.ahead()["lost"]


def test_a_lost_lidar_means_crawling_too():
    lid = Lidar()
    c = ctrl(lid=lid)
    assert forward(c) == 0.6
    lid.expected = True
    c.drive.last_input = 0.0
    assert forward(c) == pytest.approx(0.10) and c.ahead()["lost"]


def test_a_sensor_named_on_the_command_line_is_expected_from_the_start():
    assert forward(ctrl(RangeSensor("/nonexistent/distance"))) == pytest.approx(0.10)
    assert forward(ctrl(lid=Lidar("/nonexistent/lidar"))) == pytest.approx(0.10)


def test_the_obstacle_stop_does_not_redraw_the_lidar_view(monkeypatch):
    lid = Lidar()
    lid.port, lid.spinning, lid.at, lid.points = OpenPort(), True, time.monotonic(), [(0.0, 0.26)]
    monkeypatch.setattr(Lidar, "bins", lambda self: pytest.fail("bins() at 100 Hz"))
    c = ctrl(lid=lid)
    assert forward(c) == 0.0 and c.ahead()["by"] == "lidar"


def test_a_driver_may_turn_it_off_for_themselves_only():
    c = ctrl(FakeRange(0.1))
    c.set_obstacle_stop(False, ME)
    assert forward(c) == 0.6
    c.drive.last_input = 0.0                                   # their dead-man lapses...
    assert forward(c, OTHER) == 0.0                           # ...someone else drives: it holds for them
    c.drive.last_input = 0.0
    assert forward(c, AI, priority=PRIORITY_BRAIN) == 0.0     # and for the AI
    c.drive.last_input = 0.0
    assert forward(c) == 0.6
    c.release(ME)                                             # they leave: back on
    assert c.obstacle_off_for is None
    assert forward(c, OTHER) == 0.0


async def test_switch_from_the_driving_page():
    board = SimulatedBoard()
    c = MotorController(Config(device=await board.start(), range_device=None, lidar_device=None))
    c.range = FakeRange(0.1)
    client = TestClient(TestServer(build_app(c)))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"type": "obstacle_stop", "on": False})
        for _ in range(50):
            m = await ws.receive_json()
            if m.get("type") == "status" and m.get("obstacle_off_by_you"):
                break
        assert m["obstacle_off_by_you"] and m["drive"]["obstacle"]["off"]
        await ws.close()
        for _ in range(40):                                   # leaving the page turns it back on
            if c.obstacle_off_for is None:
                break
            await __import__("asyncio").sleep(0.05)
        assert c.obstacle_off_for is None
    finally:
        await client.close()
        await board.stop()


def test_the_defaults_fit_a_home():
    """Stop 15 cm short, slow from 60, the path 18 cm either side (a Beam+ base is 31 cm wide): 25 / 80 / 25 kept
    the robot out of the laundry corner (a wall it fits beside counted as 0 m ahead)."""
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None))
    c.lidar = FakeLidar([(-74.0, 0.21), (-60.0, 0.23), (0.0, 1.5)])     # the wall beside, 20 cm off the centre
    assert c.ahead()["gap_m"] == pytest.approx(1.44, abs=0.01)
    c.range = FakeRange(0.16)
    assert forward(c, linear=0.3) == 0.0                             # 16 cm: just over the line, crawling: stop
    c.range = FakeRange(0.375)
    top = c.cfg.max_linear                                           # halfway from 15 to 60 cm: half speed
    assert forward(c, linear=top) == pytest.approx(top * 0.225 / 0.45, abs=0.01)


def test_a_lidar_off_the_centre_line():
    """Ours sits 3 cm to the left: the paper towels 9 cm off the left wheel (24.5 cm from the centre line) it saw at
    21.5. Something 20 cm off the centre on the left (4.5 cm from the wheel) it sees at 17: corrected, beside the
    robot; uncorrected, in its path, 0 m ahead."""
    towels = FakeLidar([(-80.0, 0.173), (-74.0, 0.177), (0.0, 1.5)])
    plain = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None))
    plain.lidar = towels
    assert plain.ahead()["gap_m"] < 0.05
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None, matrix_device=None,
                               lidar_side_m=-0.03))
    c.lidar = towels
    assert c.ahead()["gap_m"] == pytest.approx(1.44, abs=0.01)
    # ...and on its right, the same correction brings things 3 cm closer: 19 cm from the lidar is 16 from the centre.
    c.lidar = FakeLidar([(70.0, 0.20), (0.0, 1.5)])
    assert c.ahead()["gap_m"] < 0.05


def test_a_hold_is_logged_with_what_held_it(caplog):
    c = ctrl(rng=FakeRange(0.20))
    c.cfg.deadman_s = 60                                              # a driver holding forward throughout
    t = time.monotonic()
    with caplog.at_level("INFO", logger="beam.server"):
        assert c.set_target(0.3, 0.0, ME)
        c._effective_target(t)
        c._effective_target(t + 0.5)
        assert "forward held" not in caplog.text                     # not every flicker
        c._effective_target(t + 1.2)
        assert "forward held by sensor 0.20 m ahead" in caplog.text
        c.range = FakeRange(1.5)
        c._effective_target(t + 3.0)
        assert "forward free again after 3.0 s" in caplog.text
