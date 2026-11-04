"""The motor server's own rules: tokens, who may release the E-stop, nothing relayed while asleep, a clean stop."""

import asyncio
import json
import math
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from beamctl import protocol as p
from beamctl.server import Config, DriveState, MotorController, build_app, same_secret
from beamctl.signaling import MediaHub
from beamctl.sim import SimulatedBoard

KEY = "brain-secret"


@pytest.fixture
async def rig():
    board = SimulatedBoard()
    ctrl = MotorController(Config(device=await board.start(), brain_key=KEY, token="sesame",
                                  range_device=None, lidar_device=None))
    client = TestClient(TestServer(build_app(ctrl)))
    await client.start_server()
    yield ctrl, client
    await client.close()
    await board.stop()


async def next_of(ws, kind, timeout=2.0):
    async def loop():
        while True:
            m = await ws.receive_json()
            if m.get("type") == kind:
                return m
    return await asyncio.wait_for(loop(), timeout)


def test_secrets_compare_as_bytes():
    assert same_secret("sesame", "sesame") and not same_secret("sesam", "sesame")
    assert not same_secret("sésame", "sesame") and same_secret("sésame", "sésame")


async def test_a_non_ascii_token_is_unauthorized_not_an_error(rig):
    _, client = rig
    assert (await client.get("/api/status?token=s%C3%A9same")).status == 401
    assert (await client.get("/api/status?token=%E2%9C%93")).status == 401
    assert (await client.get("/api/status?token=sesame")).status == 200
    assert (await client.get("/ws?token=sesame&role=brain&key=%C3%A9")).status == 403


async def until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_only_who_pressed_stop_or_the_driver_may_release_it(rig):
    ctrl, client = rig
    driver = await client.ws_connect("/ws?token=sesame")
    viewer = await client.ws_connect("/ws?token=sesame")
    await driver.send_json({"type": "drive", "linear": 0.2, "angular": 0.0})
    assert await until(lambda: ctrl.drive.owner is not None)
    await viewer.send_json({"type": "estop"})                    # anyone may stop it...
    assert await until(lambda: ctrl.drive.estop)
    await driver.send_json({"type": "release_estop"})            # ...the driver may release it
    assert await until(lambda: not ctrl.drive.estop)

    other = await client.ws_connect("/ws?token=sesame")
    await driver.send_json({"type": "estop"})
    assert await until(lambda: ctrl.drive.estop)
    await other.send_json({"type": "release_estop"})             # a viewer can't undo someone else's stop
    assert "release" in (await next_of(other, "denied"))["reason"]
    assert ctrl.drive.estop
    await driver.close()                                         # once that page has left, anyone may
    assert await until(lambda: ctrl.drive.estop_by is None)
    await other.send_json({"type": "release_estop"})
    assert await until(lambda: not ctrl.drive.estop)
    await viewer.close()
    await other.close()


def test_releasing_by_the_page_that_pressed_it():
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None))
    me, other = object(), object()
    c.set_estop(True, me)
    assert not c.set_estop(False, other) and c.drive.estop
    c.set_estop(True, other)                                     # pressing again doesn't take it over
    assert not c.set_estop(False, other)
    assert c.set_estop(False, me) and not c.drive.estop


def test_becoming_the_driver_after_the_stop_doesnt_allow_releasing_it():
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None))
    c.sleep.asleep = False
    me, other = object(), object()
    c.set_estop(True, me)
    c.set_target(0.0, 0.0, other)                                # a "stop" makes it the driver now...
    assert c.drive.owner is other
    assert not c.may_release_estop(other) and not c.set_estop(False, other)   # ...but not the driver then
    assert c.may_release_estop(me)


async def test_the_robot_page_is_refused_while_asleep(rig):
    ctrl, client = rig
    ctrl.go_to_sleep("test")
    assert (await client.get("/ws?token=sesame&role=robot")).status == 503
    ctrl.wake_up("test")
    robot = await client.ws_connect("/ws?token=sesame&role=robot")
    await robot.close()


async def test_nothing_reaches_the_brain_while_asleep(rig):
    ctrl, client = rig
    brain = await client.ws_connect(f"/ws?token=sesame&role=brain&key={KEY}")
    robot = await client.ws_connect("/ws?token=sesame&role=robot")
    pilot = await client.ws_connect("/ws?token=sesame")
    await asyncio.sleep(0.1)
    frame = json.dumps({"type": "frame", "cam": "head", "w": 640, "h": 480, "jpeg": "QUJD"}, separators=(",", ":"))
    ctrl.go_to_sleep("test")                 # a robot page still connected as it falls asleep (a dev setup)
    await robot.send_str(frame)
    await robot.send_bytes(b"\0\0" * 160)
    with pytest.raises(asyncio.TimeoutError):
        await next_of(brain, "frame", timeout=0.4)
    await pilot.send_json({"type": "rtc", "data": {"kind": "hello"}})    # no calls to a sleeping robot
    await next_of(pilot, "rtc_unavailable")
    ctrl.wake_up("test")
    await robot.send_str(frame)
    assert (await next_of(brain, "frame"))["cam"] == "head"
    for ws in (brain, robot, pilot):
        await ws.close()


async def test_no_listening_while_asleep():
    asleep = [False]
    hub = MediaHub(asleep=lambda: asleep[0])
    hub.robot = hub.brain = type("Open", (), {"closed": False, "send_json": None})()
    hub.brain_ears = True
    assert hub.listen_allowed
    asleep[0] = True
    assert not hub.listen_allowed


def test_the_first_minute_after_boot_is_not_driving():
    assert DriveState().last_input == -math.inf
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None))
    snap = c.snapshot()["drive"]
    assert snap["deadman_active"] and snap["driver"] is None
    assert c._effective_target(time.monotonic()) == (0.0, 0.0)
    assert not time.monotonic() - c.drive.last_input < 60      # the sleep schedule's "busy"


async def test_stop_ends_the_motor_loop_before_the_zeros():
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None))
    sent = []

    class Port:
        is_open = True

        def write(self, data):
            sent.append(data)

        def close(self):
            self.is_open = False

    async def loop():                                            # a motor loop still driving
        while True:
            sent.append(b"moving")
            await asyncio.sleep(0.001)

    c._port = Port()
    c._task = asyncio.create_task(loop())
    await asyncio.sleep(0.01)
    await c.stop()
    zero = p.velocity(0.0, 0.0, 0)
    first = sent.index(zero)
    assert sent[first:] == [zero] * 3


def test_server_secrets_come_from_the_environment(monkeypatch):
    """beam-setup passes the token and brain key as $BEAM_TOKEN / $BEAM_BRAIN_KEY, not on the command line."""
    import os
    import sys

    from beamctl import server

    got = {}
    monkeypatch.setattr(server, "MotorController", lambda cfg: got.setdefault("cfg", cfg))
    monkeypatch.setattr(server, "build_app", lambda ctl, **kw: None)
    monkeypatch.setattr(server.web, "run_app", lambda app, **kw: None)
    monkeypatch.setenv("BEAM_TOKEN", "tok-from-env")
    monkeypatch.setenv("BEAM_BRAIN_KEY", "key-from-env-0123")
    monkeypatch.setattr(sys, "argv", ["beamctl.server", "--device", "/dev/null"])
    server.main()
    assert got["cfg"].token == "tok-from-env" and got["cfg"].brain_key == "key-from-env-0123"
    assert "BEAM_TOKEN" not in os.environ and "BEAM_BRAIN_KEY" not in os.environ   # not handed to children


def _stopping_controller(powering_off, monkeypatch, delay=20):
    from beamctl import server

    monkeypatch.setattr(server, "system_powering_off", lambda: powering_off)
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None,
                               power_off_delay_s=delay))
    sent = []

    class Port:
        is_open = True

        def write(self, data):
            sent.append(data)

        def close(self):
            self.is_open = False

    c._port = Port()
    return c, sent


async def test_poweroff_tells_the_base_to_switch_off_after_the_zeros(monkeypatch):
    """Otherwise the armed 300 s power-cycle starts the robot again a few minutes after `poweroff`."""
    c, sent = _stopping_controller(True, monkeypatch)
    await c.stop()
    zero, off = p.velocity(0.0, 0.0, 0), p.power(20, p.POWER_OFF)
    assert sent == [zero] * 3 + [off] * 3
    cmd = p.decode_command(off)
    assert (cmd.code, cmd.a, cmd.b) == (p.CMD_POWER, 20, 2)


async def test_a_restart_or_reboot_leaves_the_power_cycle_armed(monkeypatch):
    for powering_off, delay in ((False, 20), (True, 0)):         # not powering off; or switched off by --power-off-delay 0
        c, sent = _stopping_controller(powering_off, monkeypatch, delay)
        await c.stop()
        assert sent == [p.velocity(0.0, 0.0, 0)] * 3


def test_only_poweroff_and_halt_count_as_powering_off(monkeypatch):
    from beamctl import server

    def jobs(text):
        class R:
            stdout = text
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: R())
        return server.system_powering_off()

    assert jobs("1 poweroff.target start waiting\n2 beam-server.service stop running\n")
    assert jobs("1 halt.target start waiting\n")
    assert not jobs("1 reboot.target start waiting\n2 beam-server.service stop running\n")
    assert not jobs("2 beam-server.service restart running\n")
    assert not jobs("")

    def missing(*a, **k):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(server.subprocess, "run", missing)
    assert not server.system_powering_off()                         # a dev machine without systemd


async def test_only_the_screen_key_takes_the_robot_and_panel_roles():
    """Any app on the panel phone reaches 127.0.0.1 through adb reverse: being local isn't enough."""
    board = SimulatedBoard()
    ctrl = MotorController(Config(device=await board.start(), screen_key="boot-key-123",
                                  range_device=None, lidar_device=None))
    client = TestClient(TestServer(build_app(ctrl)))
    await client.start_server()
    try:
        for role in ("robot", "panel"):
            for q in ("", "&screen_key=wrong"):
                r = await client.get(f"/ws?role={role}{q}")
                assert r.status == 403, (role, q)
            ws = await client.ws_connect(f"/ws?role={role}&screen_key=boot-key-123")
            await ws.close()
    finally:
        await client.close()
        await board.stop()
