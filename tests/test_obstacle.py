"""Obstacle stop: forward driving slows and stops for what the lidar and the ultrasonic sensor see ahead."""

import math
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from beamctl.server import PRIORITY_BRAIN, Config, MotorController, build_app
from beamctl.sim import SimulatedBoard

ME, OTHER, AI = object(), object(), object()


class FakeRange:
    def __init__(self, m, fresh=True):
        self.m, self.fresh = m, fresh

    def snapshot(self):
        return {"m": self.m if self.fresh else None, "fresh": self.fresh}

    def start(self):
        pass

    async def stop(self):
        pass


class FakeLidar:
    def __init__(self, points, fresh=True):
        self.points, self.fresh, self.at = points, fresh, time.monotonic()

    def snapshot(self):
        return {"fresh": self.fresh}


def ctrl(rng=None, lid=None) -> MotorController:
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None))
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
    # 0.4 m from the lidar straight ahead is 0.2 m in front of the robot: stop.
    assert forward(ctrl(lid=FakeLidar([(0.0, 0.4)]))) == 0.0
    # Beside the robot (0.3 m to the right) isn't in its way.
    assert forward(ctrl(lid=FakeLidar([(90.0, 0.3)]))) == 0.6
    # Ahead and a little to the left, within the robot's width: in the way.
    a = math.degrees(math.atan2(-0.2, 0.4))
    assert forward(ctrl(lid=FakeLidar([(a % 360, math.hypot(0.4, 0.2))]))) == 0.0


def test_the_nearest_of_both_sensors_wins():
    c = ctrl(FakeRange(1.5), FakeLidar([(0.0, 0.4)]))
    assert forward(c) == 0.0 and c.ahead()["by"] == "lidar"


def test_a_silent_sensor_means_crawling():
    assert forward(ctrl(FakeRange(None, fresh=False))) == pytest.approx(0.10)
    assert forward(ctrl(lid=FakeLidar([], fresh=False))) == pytest.approx(0.10)


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
