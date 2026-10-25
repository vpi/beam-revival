"""Signaling relay between the robot media page and pilot pages."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from beamctl.server import Config, MotorController, build_app
from beamctl.sim import SimulatedBoard


@pytest.fixture
async def client():
    board = SimulatedBoard()
    path = await board.start()
    c = TestClient(TestServer(build_app(MotorController(Config(device=path)))))
    await c.start_server()
    yield c
    await c.close()
    await board.stop()


async def next_of(ws, kind, timeout=2.0):
    """Skip telemetry and return the next message of the given type."""
    async def loop():
        while True:
            m = await ws.receive_json()
            if m.get("type") == kind:
                return m
    return await asyncio.wait_for(loop(), timeout)


async def test_pilot_without_robot_gets_unavailable(client):
    pilot = await client.ws_connect("/ws")
    await pilot.send_json({"type": "rtc", "data": {"kind": "hello"}})
    await next_of(pilot, "rtc_unavailable")
    await pilot.close()


async def test_offer_answer_and_ice_are_routed_by_pilot_id(client):
    robot = await client.ws_connect("/ws?role=robot")
    p1 = await client.ws_connect("/ws")
    p2 = await client.ws_connect("/ws")

    await p1.send_json({"type": "rtc", "data": {"kind": "hello"}})
    hello1 = await next_of(robot, "rtc")
    await p2.send_json({"type": "rtc", "data": {"kind": "hello"}})
    hello2 = await next_of(robot, "rtc")
    assert hello1["data"] == {"kind": "hello"} and hello1["from"] != hello2["from"]

    # Robot answers p2 only: p2 receives it, p1 doesn't.
    await robot.send_json({"type": "rtc", "to": hello2["from"], "data": {"kind": "offer", "sdp": "X"}})
    got = await next_of(p2, "rtc")
    assert got["data"] == {"kind": "offer", "sdp": "X"}
    with pytest.raises(asyncio.TimeoutError):
        await next_of(p1, "rtc", timeout=0.4)

    await p1.send_json({"type": "rtc", "data": {"kind": "ice", "candidate": {"c": 1}}})
    ice = await next_of(robot, "rtc")
    assert ice == {"type": "rtc", "from": hello1["from"], "data": {"kind": "ice", "candidate": {"c": 1}}}
    for ws in (robot, p1, p2):
        await ws.close()


async def test_robot_hears_bye_when_pilot_leaves(client):
    robot = await client.ws_connect("/ws?role=robot")
    pilot = await client.ws_connect("/ws")
    await pilot.send_json({"type": "rtc", "data": {"kind": "hello"}})
    hello = await next_of(robot, "rtc")
    await pilot.close()
    bye = await next_of(robot, "rtc")
    assert bye == {"type": "rtc", "from": hello["from"], "data": {"kind": "bye"}}
    await robot.close()


async def test_pilots_are_told_when_robot_comes_online(client):
    pilot = await client.ws_connect("/ws")
    robot = await client.ws_connect("/ws?role=robot")
    await next_of(pilot, "robot_online")
    status = await next_of(pilot, "status")
    assert status["media"]["robot_online"] is True
    await robot.close()
    await pilot.close()


async def test_new_robot_page_replaces_old_with_stand_down_code(client):
    from beamctl.signaling import REPLACED_CLOSE_CODE
    old = await client.ws_connect("/ws?role=robot")
    new = await client.ws_connect("/ws?role=robot")
    msg = await old.receive(timeout=2)
    while msg.type.name == "TEXT":            # skip the brain/frames status messages
        msg = await old.receive(timeout=2)
    assert msg.type.name in ("CLOSE", "CLOSED", "CLOSING")
    assert old.close_code == REPLACED_CLOSE_CODE   # the page must not reconnect and fight back
    # The new page is the robot now: pilots reach it.
    pilot = await client.ws_connect("/ws")
    await pilot.send_json({"type": "rtc", "data": {"kind": "hello"}})
    hello = await next_of(new, "rtc")
    assert hello["data"] == {"kind": "hello"}
    await pilot.close()
    await new.close()


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Tailscale-User-Login", "Forwarded"])
async def test_robot_role_rejected_through_a_proxy(client, header):
    # tailscale serve connects from 127.0.0.1 on behalf of remote visitors.
    r = await client.get("/ws?role=robot", headers={header: "someone@example.com"})
    assert r.status == 403


async def test_robot_role_rejected_from_remote_hosts(monkeypatch, client):
    # TestClient connects from 127.0.0.1; pretend the request came from the LAN.
    from aiohttp import web
    monkeypatch.setattr(web.BaseRequest, "remote", property(lambda self: "192.168.1.50"))
    r = await client.get("/ws?role=robot")
    assert r.status == 403
