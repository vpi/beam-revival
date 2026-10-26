"""Motor server against the simulated board, over a real pty and a real WebSocket."""

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from beamctl import protocol as p
from beamctl.server import Config, MotorController, build_app
from beamctl.sim import SimulatedBoard


async def wait_for(pred, timeout=3.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


@pytest.fixture
async def rig():
    """Yields (board, controller, client) with a fast watchdog so tests run in seconds."""
    rigs = []

    async def make(board_kw=None, **cfg_kw):
        board = SimulatedBoard(**(board_kw or {}))
        path = await board.start()
        cfg = Config(device=path, **cfg_kw)
        ctrl = MotorController(cfg)
        client = TestClient(TestServer(build_app(ctrl)))
        await client.start_server()
        rigs.append((board, client))
        await wait_for(lambda: ctrl.link.connected)
        return board, ctrl, client

    yield make
    for board, client in rigs:
        await client.close()
        await board.stop()


async def drive(ws, linear, angular, seconds, hz=20):
    for _ in range(int(seconds * hz)):
        await ws.send_json({"type": "drive", "linear": linear, "angular": angular})
        await asyncio.sleep(1 / hz)


async def test_connects_and_refreshes_watchdog_immediately(rig):
    board, ctrl, _ = await rig()
    assert ctrl.link.connected
    # First thing after opening: a power-cycle order with Beam's 300 s delay.
    assert board.stats.power_commands
    _, delay, kind = board.stats.power_commands[0]
    assert (delay, kind) == (300, p.POWER_CYCLE)


async def test_streams_velocity_commands_at_about_100hz(rig):
    board, ctrl, _ = await rig()
    n0 = board.stats.velocity_commands
    await asyncio.sleep(1.0)
    rate = board.stats.velocity_commands - n0
    assert 60 <= rate <= 110, rate


async def test_drive_forward_then_deadman_stops(rig):
    board, ctrl, client = await rig()
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.4, 0.0, 1.0)
    assert board.linear > 0.3
    assert board.x > 0.1
    # Stop sending: the dead-man must bring the robot to 0 without any "stop" message.
    t_last = time.monotonic()
    assert await wait_for(lambda: board.cmd_linear == 0.0, timeout=2.0)
    assert time.monotonic() - t_last < 1.5
    await ws.close()


async def test_speed_is_clamped_to_server_limit(rig):
    board, ctrl, client = await rig(max_linear=0.3)
    ws = await client.ws_connect("/ws")
    await drive(ws, 5.0, 0.0, 1.5)
    assert board.stats.last_velocity_cmd[0] <= 0.3 + 1e-6
    await ws.close()


async def test_acceleration_is_ramped(rig):
    board, ctrl, client = await rig()
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "drive", "linear": 0.6, "angular": 0.0})
    await asyncio.sleep(0.15)
    # 0.8 m/s^2 ramp: after ~0.15 s the command must still be well below 0.6.
    assert 0.0 < board.stats.last_velocity_cmd[0] < 0.3
    await ws.close()


async def test_disconnect_stops_immediately(rig):
    board, ctrl, client = await rig()
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.4, 0.0, 0.6)
    await ws.close()
    assert await wait_for(lambda: ctrl.link.commanded_linear < 0.05, timeout=0.6)


async def test_estop_latches_until_released(rig):
    board, ctrl, client = await rig()
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.4, 0.0, 0.5)
    await ws.send_json({"type": "estop"})
    await drive(ws, 0.4, 0.0, 0.8)
    assert board.stats.last_velocity_cmd[0] == 0.0
    await ws.send_json({"type": "release_estop"})
    await drive(ws, 0.4, 0.0, 0.5)
    assert board.stats.last_velocity_cmd[0] > 0.0
    await ws.close()


async def test_second_driver_is_denied_while_first_is_active(rig):
    board, ctrl, client = await rig()
    ws1 = await client.ws_connect("/ws")
    ws2 = await client.ws_connect("/ws")
    task = asyncio.create_task(drive(ws1, 0.3, 0.0, 1.0))
    await asyncio.sleep(0.2)
    await ws2.send_json({"type": "drive", "linear": -0.3, "angular": 0.0})
    denied = False
    for _ in range(20):
        msg = await ws2.receive_json(timeout=1)
        if msg.get("type") == "denied":
            denied = True
            break
    await task
    assert denied
    assert board.stats.last_velocity_cmd[0] >= 0.0
    await ws1.close()
    await ws2.close()


async def test_watchdog_keeps_board_alive(rig):
    # 100x time scale: the 300 s delay fires after 3 s if not refreshed. Refresh every 0.5 s.
    board, ctrl, _ = await rig(board_kw={"time_scale": 100.0}, watchdog_refresh_s=0.5)
    await asyncio.sleep(5.0)
    assert board.stats.power_events == []
    assert len(board.stats.power_commands) >= 8


async def test_simulator_power_cycles_without_refresh(rig):
    # Sanity check of the simulator itself: if the server stops refreshing, the board cycles power.
    board, ctrl, _ = await rig(board_kw={"time_scale": 100.0, "reboot_seconds": 0.5},
                               watchdog_refresh_s=3600)
    assert await wait_for(lambda: board.stats.power_events, timeout=5.0)
    assert board.stats.power_events[0].kind == p.POWER_CYCLE


async def test_survives_corrupted_and_dropped_replies(rig):
    board, ctrl, client = await rig(board_kw={"corrupt_rate": 0.2, "drop_rate": 0.1, "seed": 1})
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.3, 0.0, 1.5)
    assert board.linear > 0.2
    assert ctrl._reader.crc_errors > 0 or ctrl.link.timeouts > 0
    assert ctrl.link.connected
    await ws.close()


async def test_hung_board_triggers_reopen_and_recovers(rig):
    board, ctrl, client = await rig(no_comm_reset_s=0.5)
    reconnects = ctrl.link.reconnects
    board.unresponsive = True
    assert await wait_for(lambda: not ctrl.link.connected, timeout=2.0)
    board.unresponsive = False
    assert await wait_for(lambda: ctrl.link.connected and ctrl.link.reconnects > reconnects, timeout=4.0)
    # The first command after a reconnect must be the watchdog refresh, and speed restarts from 0.
    assert ctrl.link.commanded_linear == 0.0


async def test_status_endpoint_and_token(rig):
    board, ctrl, client = await rig(token="s3cret")
    r = await client.get("/api/status")
    assert r.status == 401
    r = await client.get("/api/status?token=s3cret")
    body = await r.json()
    assert body["link"]["connected"] is True
    assert body["board"]["motors_enabled"] is True
    assert 0 < body["board"]["battery_percent"] <= 100


async def test_limiter_clock_is_echoed_as_tag(rig):
    board, ctrl, client = await rig()
    await asyncio.sleep(0.3)
    # The default tag is the board's limiter clock from the previous status, so it's never stale.
    assert board.stats.last_tag > 0
    assert 0 <= board.limiter_clock() - board.stats.last_tag < (200 << 32)
    assert not board.timed_out


async def test_board_ignores_stale_tags(rig):
    # The real board drives only when the tag is a recent limiter clock value. board_time was our
    # first guess, and the Beam+ ignored every command with it.
    board, ctrl, client = await rig(tag_mode="board_time")
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.4, 0.0, 0.8)
    assert board.timed_out
    assert board.cmd_linear == 0.0 and board.x == pytest.approx(0.0, abs=1e-3)
    await ws.close()
