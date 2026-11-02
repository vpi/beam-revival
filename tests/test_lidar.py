"""The lidar: its scan data, the mast's slice, and the reader that spins it only while the robot is awake."""

import asyncio
import os

from beamctl import lidar
from beamctl.lidar import DESCRIPTOR, START, STOP, Lidar, ScanParser, masked, parse_mask


def node(angle: float, metres: float, new_turn: bool = False, quality: int = 40) -> bytes:
    q6, q2 = round(angle * 64), round(metres * 4000)
    return bytes([quality << 2 | (0b01 if new_turn else 0b10), (q6 & 0x7F) << 1 | 1, q6 >> 7, q2 & 0xFF, q2 >> 8])


def turn(dist=lambda a: 1.0, step=10) -> bytes:
    return b"".join(node(a, dist(a), new_turn=(a == 0)) for a in range(0, 360, step))


def test_turns_come_out_whole():
    p = ScanParser()
    assert p.feed(b"\x00\x01" + DESCRIPTOR + node(350, 2.0)) == []       # noise, then a partial turn: dropped
    turns = p.feed(turn() + turn(lambda a: 2.5))
    assert len(turns) == 1 and len(turns[0]) == 36                         # the second is still open
    assert turns[0][9] == (90.0, 1.0)


def test_no_echo_and_out_of_step_bytes_are_skipped():
    p = ScanParser()
    data = DESCRIPTOR + node(0, 1.0, True) + node(10, 0.0) + b"\x00" + node(20, 1.5) + node(0, 1.0, True)
    assert p.feed(data) == [[(0.0, 1.0), (20.0, 1.5)]]


def test_data_split_anywhere():
    p, data, turns = ScanParser(), DESCRIPTOR + turn() + turn(), []
    for i in range(0, len(data), 7):
        turns += p.feed(data[i:i + 7])
    assert len(turns) == 1 and len(turns[0]) == 36


def test_the_mast_slice():
    m = parse_mask("135-210")
    assert masked(180, m) and masked(135, m) and not masked(210, m) and not masked(0, m)
    across = parse_mask("350-10")
    assert masked(355, across) and masked(5, across) and not masked(180, across)
    assert parse_mask("") is None and not masked(180, None)


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_spins_while_awake_only():
    master, slave = os.openpty()
    os.set_blocking(master, False)
    awake = [True]
    lid = Lidar(os.ttyname(slave), mask="135-210", should_spin=lambda: awake[0])
    sent = bytearray()

    def read_sent():
        try:
            sent.extend(os.read(master, 4096))
        except BlockingIOError:
            pass
        return sent

    assert lid.snapshot() is None
    lid.start()
    try:
        assert await wait_for(lambda: START in read_sent())
        os.write(master, DESCRIPTOR + turn(lambda a: 0.07 if 135 <= a < 210 else 1.2) + turn())
        assert await wait_for(lambda: lid.snapshot()["fresh"])
        snap = lid.snapshot()
        assert snap["spinning"] and snap["mask"] == [135.0, 210.0]
        cm = snap["cm"]
        assert cm[0] == 120 and cm[45] == 120                              # ahead, and 90 degrees right
        assert cm[90] == 0                                                 # behind: the mast, left out
        sent.clear()
        awake[0] = False                                                   # the robot goes to sleep
        assert await wait_for(lambda: STOP in read_sent())
        assert await wait_for(lambda: not lid.snapshot()["spinning"])
        assert lid.snapshot()["cm"] is None
    finally:
        await lid.stop()
        os.close(master)
        os.close(slave)


async def test_no_lidar_nothing_shown(monkeypatch):
    monkeypatch.setattr(lidar, "AUTO", "/nonexistent/lidar-*")
    lid = Lidar()
    lid.start()
    await asyncio.sleep(0.05)
    assert lid.snapshot() is None
    await lid.stop()
