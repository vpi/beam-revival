"""The server's side of the brain: access control, frame relay, message routing, driving priority."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from beamctl.server import Config, MotorController, build_app
from beamctl.sim import SimulatedBoard

KEY = "brain-secret"


@pytest.fixture
async def rig():
    board = SimulatedBoard()
    path = await board.start()
    ctrl = MotorController(Config(device=path, brain_key=KEY))
    client = TestClient(TestServer(build_app(ctrl)))
    await client.start_server()
    yield board, ctrl, client
    await client.close()
    await board.stop()


async def next_of(ws, kind, timeout=2.0):
    async def loop():
        while True:
            m = await ws.receive_json()
            if m.get("type") == kind:
                return m
    return await asyncio.wait_for(loop(), timeout)


async def test_brain_needs_the_key(rig):
    _, _, client = rig
    assert (await client.get("/ws?role=brain")).status == 403
    assert (await client.get("/ws?role=brain&key=wrong")).status == 403


async def test_brain_disabled_without_configured_key():
    board = SimulatedBoard()
    path = await board.start()
    client = TestClient(TestServer(build_app(MotorController(Config(device=path)))))
    await client.start_server()
    try:
        assert (await client.get(f"/ws?role=brain&key={KEY}")).status == 403
    finally:
        await client.close()
        await board.stop()


async def test_robot_is_asked_for_frames_only_while_brain_is_online(rig):
    _, _, client = rig
    robot = await client.ws_connect("/ws?role=robot")
    assert (await next_of(robot, "frames"))["fps"] == 0
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    assert (await next_of(robot, "brain"))["online"] is True
    assert (await next_of(robot, "frames"))["fps"] > 0
    await brain.close()
    assert (await next_of(robot, "brain"))["online"] is False
    assert (await next_of(robot, "frames"))["fps"] == 0
    await robot.close()


async def test_frames_reach_the_brain_unchanged(rig):
    _, _, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    robot = await client.ws_connect("/ws?role=robot")
    frame = json.dumps({"type": "frame", "cam": "head", "w": 640, "h": 480, "jpeg": "QUJD"}, separators=(",", ":"))
    await robot.send_str(frame)
    got = await asyncio.wait_for(brain.receive_str(), 2)
    while '"telemetry"' in got:                     # the brain also gets 10 Hz telemetry
        got = await asyncio.wait_for(brain.receive_str(), 2)
    assert got == frame
    await robot.close()
    await brain.close()


async def test_brain_results_reach_pilots_and_speech_reaches_robot(rig):
    _, _, client = rig
    robot = await client.ws_connect("/ws?role=robot")
    pilot = await client.ws_connect("/ws")
    assert (await next_of(pilot, "brain"))["online"] is False     # greeting on connect
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    assert (await next_of(pilot, "brain"))["online"] is True      # then the brain arrives

    await brain.send_json({"type": "faces", "w": 640, "h": 480, "faces": [{"id": 1, "x": 1, "y": 2, "w": 3, "h": 4, "name": "Ann"}]})
    assert (await next_of(pilot, "faces"))["faces"][0]["name"] == "Ann"

    await brain.send_json({"type": "people", "people": [{"name": "Ann"}]})
    assert (await next_of(pilot, "people"))["people"] == [{"name": "Ann"}]
    late = await client.ws_connect("/ws")                       # joins later, still gets the list
    assert (await next_of(late, "people"))["people"] == [{"name": "Ann"}]

    await brain.send_json({"type": "say", "text": "Hi Ann!", "wav": "UklGRg=="})
    say = await next_of(robot, "say")
    assert say["text"] == "Hi Ann!" and say["wav"] == "UklGRg=="
    for ws in (robot, pilot, late, brain):
        await ws.close()


async def test_paused_state_reaches_robot_screen_and_pilots(rig):
    _, _, client = rig
    robot = await client.ws_connect("/ws?role=robot")
    pilot = await client.ws_connect("/ws")
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    await brain.send_json({"type": "status", "paused": True})

    async def paused_msg(ws):
        while True:
            m = await next_of(ws, "brain")
            if "paused" in m:
                return m
    for ws in (robot, pilot):
        m = await asyncio.wait_for(paused_msg(ws), 2)
        assert m == {"type": "brain", "online": True, "paused": True}
    for ws in (robot, pilot, brain):
        await ws.close()


async def test_seen_names_reach_robot_screen(rig):
    _, _, client = rig
    robot = await client.ws_connect("/ws?role=robot")
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    await brain.send_json({"type": "seen", "names": ["Alex"]})
    assert (await next_of(robot, "seen"))["names"] == ["Alex"]
    await brain.close()
    assert (await next_of(robot, "seen"))["names"] == []        # brain gone: nobody is "seen"
    await robot.close()


async def test_driver_names_from_tailscale_reach_robot_screen(rig):
    _, _, client = rig
    robot = await client.ws_connect("/ws?role=robot")
    # Through `tailscale serve` (local connection) the header names the visitor.
    pilot = await client.ws_connect("/ws", headers={"Tailscale-User-Name": "Alex P."})
    names = (await next_of(robot, "pilots"))["names"]
    while not names:
        names = (await next_of(robot, "pilots"))["names"]
    assert list(names.values()) == ["Alex"]
    await pilot.close()
    while (await next_of(robot, "pilots"))["names"]:
        pass                                                      # cleared when they leave
    await robot.close()


async def test_driver_name_header_ignored_from_remote_hosts(monkeypatch, rig):
    from aiohttp import web
    from beamctl.server import visitor_name
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/ws", headers={"Tailscale-User-Name": "Mallory"})
    monkeypatch.setattr(web.BaseRequest, "remote", property(lambda self: "192.168.1.50"))
    assert visitor_name(req) is None                              # a LAN client could fake the header
    monkeypatch.setattr(web.BaseRequest, "remote", property(lambda self: "127.0.0.1"))
    assert visitor_name(req) == "Mallory"
    enc = make_mocked_request("GET", "/ws", headers={"Tailscale-User-Name": "=?utf-8?q?Vitali=C3=AF_P?="})
    assert visitor_name(enc) == "Vitaliï"


async def test_pilot_commands_reach_the_brain_with_sender(rig):
    _, _, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    pilot = await client.ws_connect("/ws")
    await pilot.send_json({"type": "brain", "data": {"kind": "enroll", "face_id": 7, "name": "Ann"}})
    cmd = await next_of(brain, "brain_cmd")
    assert cmd["data"] == {"kind": "enroll", "face_id": 7, "name": "Ann"} and cmd["from"].startswith("p")
    await pilot.close()
    await brain.close()


async def test_human_always_overrides_brain(rig):
    board, ctrl, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    pilot = await client.ws_connect("/ws")

    # Brain drives while nobody else is.
    for _ in range(6):
        await brain.send_json({"type": "drive", "linear": 0.2, "angular": 0.0})
        await asyncio.sleep(0.05)
    assert ctrl.snapshot()["drive"]["driver"] == "brain"
    assert ctrl.drive.target_linear == pytest.approx(0.2)

    # A person takes over immediately...
    await pilot.send_json({"type": "drive", "linear": -0.1, "angular": 0.0})
    await asyncio.sleep(0.05)
    assert ctrl.snapshot()["drive"]["driver"] == "human"
    assert ctrl.drive.target_linear == pytest.approx(-0.1)

    # ...and the brain is refused while they are driving.
    await brain.send_json({"type": "drive", "linear": 0.3, "angular": 0.0})
    assert (await next_of(brain, "denied"))["reason"] == "a person is driving"
    assert ctrl.drive.target_linear == pytest.approx(-0.1)
    await pilot.close()
    await brain.close()


async def test_brain_disconnect_stops_its_driving(rig):
    board, ctrl, client = rig
    brain = await client.ws_connect(f"/ws?role=brain&key={KEY}")
    for _ in range(4):
        await brain.send_json({"type": "drive", "linear": 0.2, "angular": 0.0})
        await asyncio.sleep(0.05)
    await brain.close()
    await asyncio.sleep(0.1)
    assert ctrl.drive.owner is None and ctrl.drive.target_linear == 0.0
