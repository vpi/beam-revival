"""The lidar: its scan data, the mast's slice, and the reader that spins it only while the robot is awake."""

import asyncio
import os

from beamctl import lidar
from beamctl.lidar import DESCRIPTOR, GET_INFO, INFO, START, STOP, Lidar, ScanParser, masked, parse_mask


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
        assert await wait_for(lambda: GET_INFO in read_sent())
        os.write(master, INFO + bytes([0x41, 1, 1, 18]) + bytes(16))     # "I'm a lidar"
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


def test_fine_scan_for_the_map():
    """The brain's map gets every degree (the mean distance in it), not the driving page's nearest per 2."""
    import time
    lid = Lidar(device=None)
    lid.spinning, lid.at, lid.turns = True, time.monotonic(), 7
    lid.points = [(0.2, 1.0), (0.8, 1.2), (90.5, 2.0), (359.9, 0.5)]
    s = lid.scan()
    assert s["turn"] == 7 and len(s["cm"]) == 360
    assert (s["cm"][0], s["cm"][90], s["cm"][359], s["cm"][45]) == (110, 200, 50, 0)
    lid.at -= 10
    assert lid.scan() is None                                     # stale


def test_a_mask_it_cannot_read_is_ignored_not_fatal():
    """A typo in --lidar-mask must not stop the motor server (without it the board powers the robot off)."""
    for bad in ("-30-30", "135–210", "135", "front"):
        assert Lidar(device=None, mask=bad).mask is None
    assert Lidar(device=None, mask="135-210").mask == (135.0, 210.0)


async def test_a_port_that_does_not_answer_like_a_lidar_is_left_alone(monkeypatch):
    """A robot arm (or any other gadget) on the same USB serial chip isn't taken for the lidar."""
    monkeypatch.setattr(lidar, "IDENTIFY_S", 0.3)
    arm_m, arm_s = os.openpty()
    lid_m, lid_s = os.openpty()
    for fd in (arm_m, lid_m):
        os.set_blocking(fd, False)
    arm, real = os.ttyname(arm_s), os.ttyname(lid_s)
    lid = Lidar()
    monkeypatch.setattr(lid.picker, "candidates", lambda: [p for p in (arm, real) if p not in lid.picker.rejected])
    got = bytearray()

    def answer():                                   # the real lidar answers its info request
        try:
            got.extend(os.read(lid_m, 4096))
        except BlockingIOError:
            pass
        if GET_INFO in got:
            os.write(lid_m, INFO + bytes([0x41, 1, 1, 18]) + bytes(16))
            got.clear()
        return lid.present

    lid.start()
    try:
        assert await wait_for(answer)
        assert lid.port.path == real and arm in lid.picker.rejected
        os.write(arm_m, b'{"T":1051,"x":235}\n')    # the arm talking is never read as scans
        assert lid.port.path == real
    finally:
        await lid.stop()
        for fd in (arm_m, arm_s, lid_m, lid_s):
            os.close(fd)


async def test_a_named_port_must_answer_too(monkeypatch):
    """Named in beam.conf but something else is on it: it's not used, and counts as a lost lidar."""
    monkeypatch.setattr(lidar, "IDENTIFY_S", 0.2)
    master, slave = os.openpty()
    lid = Lidar(os.ttyname(slave))
    lid.start()
    try:
        assert await wait_for(lambda: os.ttyname(slave) in lid.picker.rejected)
        assert not lid.present and lid.lost
    finally:
        await lid.stop()
        os.close(master)
        os.close(slave)


async def test_a_lidar_that_misses_the_first_request_is_still_found(monkeypatch):
    """Seen after a server restart: no answer the first time, then a minute without the lidar."""
    monkeypatch.setattr(lidar, "IDENTIFY_S", 0.2)
    monkeypatch.setattr(lidar, "IDENTIFY_GAP_S", 0.05)
    master, slave = os.openpty()
    os.set_blocking(master, False)
    lid = Lidar(os.ttyname(slave))
    asked, got = [0], bytearray()

    def answer():                                   # silent to the first request, answers the second
        try:
            got.extend(os.read(master, 4096))
        except BlockingIOError:
            pass
        if GET_INFO in got:
            asked[0] += 1
            got.clear()
            if asked[0] >= 2:
                os.write(master, INFO + bytes([0x41, 1, 1, 18]) + bytes(16))
        return lid.present

    lid.start()
    try:
        assert await wait_for(answer)
        assert asked[0] == 2 and not lid.picker.rejected
    finally:
        await lid.stop()
        os.close(master)
        os.close(slave)
