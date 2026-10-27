"""The phone panel on the robot: rear-camera signaling, Call owner, volume, sleep and privacy."""

import asyncio

import pytest
from aiohttp import WSServerHandshakeError

from test_signaling import client, next_of  # noqa: F401  (client is a fixture)


async def test_rear_camera_signaling_is_between_the_robot_page_and_the_panel(client):
    # Drivers can't reach the phone (home Wi-Fi only), so the robot page receives it and passes it on.
    robot = await client.ws_connect("/ws?role=robot")
    panel = await client.ws_connect("/ws?role=panel")
    await robot.send_json({"type": "rtc", "to": "panel", "data": {"kind": "hello"}})
    hello = await next_of(panel, "rtc")
    assert hello == {"type": "rtc", "from": "robot", "data": {"kind": "hello"}}

    await panel.send_json({"type": "rtc", "to": "robot", "data": {"kind": "offer", "sdp": "R"}})
    got = await next_of(robot, "rtc")
    assert got == {"type": "rtc", "from": "panel", "data": {"kind": "offer", "sdp": "R"}}
    for ws in (robot, panel):
        await ws.close()


async def test_pilots_cannot_reach_the_phone_camera_directly(client):
    robot = await client.ws_connect("/ws?role=robot")
    panel = await client.ws_connect("/ws?role=panel")
    pilot = await client.ws_connect("/ws")
    await pilot.send_json({"type": "rtc", "target": "panel", "data": {"kind": "hello"}})
    with pytest.raises(asyncio.TimeoutError):
        await next_of(panel, "rtc", timeout=0.4)
    await panel.send_json({"type": "rtc", "to": "p1", "data": {"kind": "offer", "sdp": "X"}})
    with pytest.raises(asyncio.TimeoutError):
        await next_of(pilot, "rtc", timeout=0.4)
    for ws in (robot, panel, pilot):
        await ws.close()


async def test_robot_page_hears_when_the_panel_comes_and_goes(client):
    robot = await client.ws_connect("/ws?role=robot")
    assert (await next_of(robot, "panel"))["online"] is False
    panel = await client.ws_connect("/ws?role=panel")
    assert (await next_of(robot, "panel"))["online"] is True
    await panel.close()
    assert (await next_of(robot, "panel"))["online"] is False
    await robot.close()


async def test_call_owner_reaches_every_driver(client):
    panel = await client.ws_connect("/ws?role=panel")
    p1 = await client.ws_connect("/ws")
    p2 = await client.ws_connect("/ws")
    await asyncio.sleep(0.1)
    await panel.send_json({"type": "call"})
    for p in (p1, p2):
        assert (await next_of(p, "call"))["type"] == "call"
    for ws in (panel, p1, p2):
        await ws.close()


async def test_volume_buttons_reach_the_robot_page(client):
    robot = await client.ws_connect("/ws?role=robot")
    panel = await client.ws_connect("/ws?role=panel")
    await panel.send_json({"type": "volume", "delta": -0.1})
    assert (await next_of(robot, "volume"))["delta"] == pytest.approx(-0.1)
    for ws in (robot, panel):
        await ws.close()


async def test_panel_gets_status_and_can_sleep_and_wake_the_robot(client):
    panel = await client.ws_connect("/ws?role=panel")
    status = await next_of(panel, "status")
    assert status["sleep"]["asleep"] is False and "viewers" in status["media"]
    await panel.send_json({"type": "sleep"})
    async def asleep():
        while not (await next_of(panel, "status"))["sleep"]["asleep"]:
            pass
    await asyncio.wait_for(asleep(), 3)
    await panel.send_json({"type": "wake"})
    async def awake():
        while (await next_of(panel, "status"))["sleep"]["asleep"]:
            pass
    await asyncio.wait_for(awake(), 3)
    await panel.close()


async def test_panel_role_only_from_the_robot_itself(client):
    # Through `tailscale serve` (a proxy) a remote visitor must not become the panel: it's a camera.
    with pytest.raises(WSServerHandshakeError) as e:
        await client.ws_connect("/ws?role=panel", headers={"X-Forwarded-For": "100.64.0.9"})
    assert e.value.status == 403


async def test_panel_page_is_served(client):
    r = await client.get("/panel")
    assert r.status == 200 and "Call owner" in await r.text()


async def test_rear_light_switch_goes_to_the_phone_and_its_state_to_every_driver(client):
    panel = await client.ws_connect("/ws?role=panel")
    p1 = await client.ws_connect("/ws")
    p2 = await client.ws_connect("/ws")
    await asyncio.sleep(0.1)
    await p1.send_json({"type": "rear_light", "on": True})
    assert (await next_of(panel, "light")) == {"type": "light", "on": True}
    await panel.send_json({"type": "light", "on": True, "available": True})
    for p in (p1, p2):
        assert (await next_of(p, "rear_light")) == {"type": "rear_light", "on": True, "available": True}
    late = await client.ws_connect("/ws")                     # a driver joining later sees it too
    assert (await next_of(late, "rear_light"))["on"] is True
    await panel.close()                                       # phone gone: no light button
    assert (await next_of(p1, "rear_light")) == {"type": "rear_light", "on": False, "available": False}
    for ws in (p1, p2, late):
        await ws.close()
