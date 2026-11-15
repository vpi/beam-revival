"""The simulated arm itself behaves like the firmware where it matters to the server."""

import asyncio
import json
import math
import os
import time

import pytest

from beamctl.armsim import SimulatedArm


class Wire:
    """The server's end of the pty, plainly."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.buf = b""

    def send(self, obj):
        os.write(self.fd, (json.dumps(obj) + "\n").encode())

    async def lines(self, wait=0.3):
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                self.buf += os.read(self.fd, 4096)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.02)
        *done, self.buf = self.buf.split(b"\n")
        return [d.decode(errors="replace").strip() for d in done if d.strip()]

    def close(self):
        os.close(self.fd)


@pytest.fixture
async def sim():
    made = []

    async def make(**kw):
        s = SimulatedArm(None, boot_s=0.05, time_scale=0.01, **kw)
        await s.start()
        made.append(s)
        return s
    yield make
    for s in made:
        await s.stop()


async def wait_done(s):
    end = time.monotonic() + 5
    while not s.mission_done and time.monotonic() < end:
        await asyncio.sleep(0.02)


async def test_boot_chatter_start_pose_and_echo(sim):
    s = await sim()
    w = Wire(s.path)
    text = await w.lines(1.0)
    assert any("Power up" in t for t in text) and any("missionPlay finished" in t for t in text)
    assert s.pos == pytest.approx({"b": 0, "s": 0, "e": math.pi / 2, "t": 3.14}, abs=1e-3)
    w.send({"T": 105})
    out = await w.lines()
    assert out[0] == '{"T":105}'                                      # echoed: InfoPrint is 1 after boot
    fb = json.loads(out[1])
    assert fb["T"] == 1051 and fb["torswitchE"] == 1 and fb["v"] == 1232
    w.send({"T": 605, "cmd": 0})
    await w.lines()
    w.send({"T": 105})
    assert [json.loads(x)["T"] for x in await w.lines()] == [1051]    # quiet: no echo
    w.close()


async def test_joints_move_at_the_commanded_speed_within_the_firmwares_limits(sim):
    s = await sim()
    await wait_done(s)
    w = Wire(s.path)
    w.send({"T": 121, "joint": 3, "angle": 120, "spd": 30, "acc": 10})        # 30 degrees at 30 deg/s
    await asyncio.sleep(0.5)
    assert 100 < math.degrees(s.pos["e"]) < 110
    await asyncio.sleep(0.8)
    assert math.degrees(s.pos["e"]) == pytest.approx(120, abs=0.5)
    w.send({"T": 102, "base": 9, "shoulder": -9, "elbow": 1.57, "hand": 3.14, "spd": 0, "acc": 10})
    await asyncio.sleep(1.5)
    assert s.pos["b"] == pytest.approx(math.pi) and s.pos["s"] == pytest.approx(-math.pi / 2)   # clamped
    w.close()


async def test_bad_json_is_dropped_without_a_word(sim):
    s = await sim()
    await wait_done(s)
    w = Wire(s.path)
    await w.lines()
    os.write(w.fd, b'{"T":105\n')
    assert await w.lines() == [] and s.received == []
    w.close()


async def test_t0_drops_it_and_makes_it_deaf(sim):
    s = await sim()
    await wait_done(s)
    w = Wire(s.path)
    w.send({"T": 0})
    await asyncio.sleep(0.02)
    assert s.t0_received == 1 and not any(s.torque.values())
    w.send({"T": 105})
    assert not any('"T":1051' in x for x in await w.lines(0.05))          # deaf (10 s, scaled to 0.1)
    await asyncio.sleep(0.15)
    assert all(s.torque.values())
    w.close()


async def test_input_during_the_mission_aborts_it_and_is_handled_after(sim):
    s = await sim(mission=[{"T": 111, "cmd": 20000}, {"T": 301, "mode": 0}])   # a long wait first
    end = time.monotonic() + 3
    while not s._in_mission and time.monotonic() < end:
        await asyncio.sleep(0.02)
    w = Wire(s.path)
    w.send({"T": 605, "cmd": 0})
    await asyncio.sleep(0.4)
    out = await w.lines()
    assert s.aborted and not s.mission_done and s.espnow_mode == 3      # the radio step never ran
    assert s.info_print == 0                                             # the command itself, after
    assert any("abort" in x for x in out)
    w.close()


async def test_a_wrong_device_sends_ultrasonic_frames_not_json(sim):
    s = await sim(wrong=True)
    w = Wire(s.path)
    await asyncio.sleep(0.4)
    w.send({"T": 105})
    raw = (await w.lines(0.3))
    assert not any('"T":1051' in x for x in raw)
    w.close()
