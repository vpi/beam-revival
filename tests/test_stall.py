"""Blocked wheels: told to move, the wheels don't turn (a shoe under one): the server stops trying that way."""

import asyncio

from beamctl import protocol as p
from beamctl.stall import StallWatch
from test_integration import drive, rig, wait_for  # noqa: F401  (rig is a fixture)


def feed(w, seconds, told_lin, moving_lin, told_ang=0.0, start=0.0, at=(0.0, 0.0), hz=50):
    """Statuses for `seconds`: told to drive at told_lin, really moving at moving_lin (linear = (L - R) * k1).
    Returns (time, counts)."""
    t, (l, r) = start, at
    for _ in range(int(seconds * hz)):
        t += 1 / hz
        a = moving_lin / hz / p.ENCODER_LINEAR_SCALE
        l, r = l + a / 2, r - a / 2
        w.update(t, round(l), round(r), told_lin, told_ang)
    return t, (l, r)


def test_driving_normally_is_never_blocked():
    w = StallWatch()
    feed(w, 3.0, 0.3, 0.28)
    assert not w.blocked and abs(w.measured[0] - 0.28) < 0.02
    feed(w, 3.0, 0.05, 0.0, start=3.0)            # a crawl that doesn't move (thick rug): not checked
    assert not w.blocked


def test_wheels_that_dont_turn_hold_that_way_until_let_go():
    w = StallWatch()
    t, at = feed(w, 0.8, 0.3, 0.0)
    assert not w.blocked                           # not yet: time for the robot to get going
    t, at = feed(w, 0.6, 0.3, 0.0, start=t, at=at)
    assert w.blocked == {"linear": 1} and w.count == 1
    assert w.limit(0.3, 0.2) == (0.0, 0.2)         # forward held; turning still free
    assert w.limit(-0.2, 0.0) == (-0.2, 0.0)       # backing away clears it
    assert not w.blocked
    feed(w, 1.6, 0.3, 0.0, start=t, at=at)
    assert w.blocked
    w.limit(0.0, 0.0)                              # let go: free to try again
    assert not w.blocked and w.limit(0.3, 0.0) == (0.3, 0.0)


def test_pushing_back_against_something_counts_too():
    w = StallWatch()
    feed(w, 1.6, -0.2, 0.01)                       # told back, creeping forward a little: stuck
    assert w.blocked == {"linear": -1}


def test_a_turn_on_the_spot_that_doesnt_turn():
    w = StallWatch()
    t, l, r = 0.0, 0, 0
    for _ in range(80):
        t += 0.02
        w.update(t, l, r, 0.0, 0.6)                # told to turn, encoders still
    assert w.blocked == {"angular": 1}
    w2 = StallWatch()
    t, l, r = 0.0, 0, 0
    for _ in range(80):
        t += 0.02
        b = -0.6 * 0.02 / p.ENCODER_ANGULAR_SCALE  # turning as told
        l, r = l + b / 2, r + b / 2
        w2.update(t, int(l), int(r), 0.0, 0.6)
    assert not w2.blocked and abs(w2.measured[1] - 0.6) < 0.05


async def test_the_server_stops_pushing_against_a_wall(rig):
    """The simulated wheels stall against a wall (no encoder counts); after a second the server holds forward."""
    class Wall:
        def collides(self, x, y):
            return x > 0.3

        def room_at(self, x, y):
            return None

    board, ctrl, client = await rig({"world": Wall()}, range_device=None, lidar_device=None)
    ws = await client.ws_connect("/ws")
    await drive(ws, 0.3, 0.0, 3.0)
    assert ctrl.stall.blocked == {"linear": 1} and board.cmd_linear == 0.0
    status = await (await client.get("/api/status")).json()
    assert status["drive"]["wheels"]["blocked"] == {"linear": 1}
    await drive(ws, -0.2, 0.0, 0.5)                # back up: allowed, and it clears
    assert await wait_for(lambda: board.cmd_linear < 0) and not ctrl.stall.blocked
    await ws.close()
