"""The 8x8 matrix distance sensor: its text frames, which cells are obstacles in the robot's path (not the
floor), and the reader that finds it among USB ports."""

import asyncio
import json
import math
import os

from beamctl import rangefinder
from beamctl.matrixtof import Frames, MatrixSensor, cell_mm
from beamctl.server import Config, MotorController

# Two frames as the real sensor sent them (firmware 1.3, lying on a desk), and the start of a third.
REAL = (b"y0:1373,1255,4000,4000,4000,4000,402,4000, \r\ny1:1385,1233,4000,4000,4000,386,4000,4000, \r\n"
        b"y2:1403,4000,4000,4000,4000,4000,4000,516, \r\ny3:1360,4000,4000,4000,4000,4000,365,4000, \r\n"
        b"y4:435,1265,4000,4000,4000,4000,4000,4000, \r\ny5:1507,4000,4000,4000,1127,1009,947,899, \r\n"
        b"y6:1570,1430,4000,1193,1144,1049,978,4000, \r\ny7:1583,1429,1286,1214,1115,1057,992,4000, \r\n") * 2 \
       + b"y0:1324,1265,4000"


def frame_text(rows) -> bytes:
    return b"".join(b"y%d:%s, \r\n" % (i, b",".join(b"%d" % v for v in r)) for i, r in enumerate(rows))


def grid(fill=4000, **cells):
    """An 8x8 frame of `fill`, with cells like r3c4=500."""
    rows = [[fill] * 8 for _ in range(8)]
    for k, v in cells.items():
        r, c = int(k[1]), int(k[3])
        rows[r][c] = v
    return rows


def test_frames_from_the_real_sensor():
    f = Frames()
    out = f.feed(REAL[:100]) + f.feed(REAL[100:])           # split anywhere
    assert len(out) == 2 and out[0] == out[1]
    assert out[0][0] == [1373, 1255, 4000, 4000, 4000, 4000, 402, 4000] and out[0][7][6] == 992
    rest = b"\n".join(frame_text(grid()).split(b"\n")[1:])
    assert len(f.feed(b",1,2,3,4,5, \r\n" + rest)) == 1            # the third frame, finished in the next read


def test_a_frame_is_eight_rows_in_order():
    f = Frames()
    rows = frame_text(grid()).split(b"\n")
    assert f.feed(b"\n".join(rows[:3] + rows[5:]) + b"\n") == []          # y3, y4 lost: no frame
    assert len(f.feed(frame_text(grid()))) == 1
    assert f.feed(b"garbage\r\n" * 10 + b'{"T":1051}\n') == []


def test_cells():
    assert cell_mm(4000) is None and cell_mm(0) is None              # no reading
    assert cell_mm(12) == 0 and cell_mm(20) == 20 and cell_mm(3999) == 3999


def sensor(**kw) -> MatrixSensor:
    return MatrixSensor("/nonexistent", **kw)


def test_the_floor_is_not_an_obstacle():
    s = sensor(height_m=0.10, tilt_deg=0.0)
    # The bottom row looks 26.25 degrees down: from 10 cm up it meets the floor 0.226 m along.
    floor = round(0.10 / math.sin(math.radians(26.25)) * 1000)
    assert s.nearest([[cell_mm(v) for v in r] for r in grid(**{f"r7c{c}": floor for c in range(8)})]) is None
    # A shoe 40 cm ahead, straight in front (rows 3-4, the middle columns): counted.
    assert abs(s.nearest([[cell_mm(v) for v in r] for r in grid(r4c3=400, r4c4=400)]) - 0.40 * math.cos(math.radians(3.75)) ** 2) < 0.01
    # The outer column (26 degrees off) at 80 cm: 0.35 m to the side, outside the robot's path...
    assert s.nearest([[cell_mm(v) for v in r] for r in grid(r4c0=800)]) is None
    # ...but at 40 cm it's 0.18 m to the side: in front of a wheel.
    assert s.nearest([[cell_mm(v) for v in r] for r in grid(r4c0=400)]) is not None


def test_tilted_down_the_floor_comes_closer_and_still_isnt_an_obstacle():
    s = sensor(height_m=0.10, tilt_deg=15.0)
    # The middle rows (3.75 degrees down + 15) meet the floor about 0.31 m along.
    d = round(0.10 / math.sin(math.radians(18.75)) * 1000)
    assert s.nearest([[cell_mm(v) for v in r] for r in grid(**{f"r4c{c}": d for c in range(8)})]) is None
    # A third of that: something 7 cm tall standing on the floor, in the way.
    assert s.nearest([[cell_mm(v) for v in r] for r in grid(r4c4=d // 3)]) is not None


def test_behind_the_front_edge_and_flipped():
    s = sensor(to_front_m=0.05, flip="x")
    s._on_data(frame_text(grid(r3c0=800)) * 2)   # column 0 as sent is the right edge once flipped
    assert s.grid[3][7] == 800 and s.grid[3][0] is None
    assert s.gap_m is None                        # 26 degrees off at 0.8 m: outside the path
    s._on_data(frame_text(grid(r3c3=500)) * 3)
    assert abs(s.gap_m - (0.5 * math.cos(math.radians(3.75)) ** 2 - 0.05)) < 0.005


# As first set on the robot: 25 cm up, 8 degrees down, on carpet. The floor, as it read there.
ROBOT = dict(height_m=0.25, tilt_deg=8.0)
FLOOR = {**{f"r6c{c}": v for c, v in enumerate([591, 591, 575, 612, 588, 597, 563, 552])},
         **{f"r7c{c}": v for c, v in enumerate([430, 441, 441, 434, 440, 437, 433, 404])}}


def test_on_the_robot_the_floor_is_floor_even_where_it_reads_short():
    s = sensor(**ROBOT)
    cells = lambda **kw: [[cell_mm(v) for v in r] for r in grid(**kw)]
    assert s.nearest(cells(**FLOOR)) is None
    # The bottom corner cells read short at times (seen: 381 mm with its neighbours at 430).
    assert s.nearest(cells(**{**FLOOR, "r7c7": 381, "r7c0": 381})) is None


def test_each_cell_goes_by_the_middle_of_its_last_three():
    assert MatrixSensor.cell([500, 990, None]) == 990 and MatrixSensor.cell([500, 520, 990]) == 520
    assert MatrixSensor.cell([500, None, None]) is None and MatrixSensor.cell([500, None]) is None
    assert MatrixSensor.cell([500, 520]) == 520 and MatrixSensor.cell([500]) == 500


def test_a_cell_that_flickers_for_one_frame_isnt_an_obstacle():
    """With nothing in front for 1.2 m, row-5 cells came and went one frame at a time: 573, then nothing, 500, 990..."""
    s = sensor(**ROBOT)
    for flick in (dict(r5c3=573), {}, dict(r5c4=500), dict(r5c4=990), dict(r5c5=477), dict(r5c3=437)):
        s._on_data(frame_text(grid(**FLOOR, **flick)))
        assert s.gap_m is None, flick


def test_something_really_there_counts_from_its_second_frame():
    # A shoe 10 cm tall half a metre ahead: row 5 meets its top 0.515 m along.
    s = sensor(**ROBOT)
    s._on_data(frame_text(grid(**FLOOR, r5c3=515)))
    assert s.gap_m is None                                       # one frame: could be a flicker
    s._on_data(frame_text(grid(**FLOOR, r5c3=530)))              # 1/15 s later, about the same
    assert s.gap_m is not None and 0.45 < s.gap_m < 0.52
    s._on_data(frame_text(grid(**FLOOR, r5c3=505)) + frame_text(grid(**FLOOR, r5c3=500)))   # two in one read
    assert s.gap_m is not None and s.gap_m < 0.5
    # Coming closer at walking pace (10 cm a frame) it stays an obstacle, a frame behind at most.
    for mm in (400, 300, 200):
        s._on_data(frame_text(grid(**FLOOR, r4c4=mm + 100)) + frame_text(grid(**FLOOR, r4c4=mm)))
        assert s.gap_m is not None and s.gap_m < (mm + 100) / 1000


def test_obstacle_stop_uses_it():
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None))
    c.lidar = None
    m = c.matrix
    m.port = type("P", (), {"is_open": True})()              # as if found
    m._on_data(frame_text(grid(r4c3=300, r4c4=300)) * 2)
    a = c.ahead()
    assert a["by"] == "sensor" and 0.28 < a["gap_m"] < 0.31 and a["max_forward"] < c.cfg.max_linear
    m._on_data(frame_text(grid()) * 3)
    assert m.gap_m is None and c.ahead()["gap_m"] is None    # gone from view: gone
    snap = c.matrix.snapshot()
    assert snap["m"] is None and snap["fresh"] and len(snap["grid"]) == 8


async def test_found_among_ports_by_its_frames(monkeypatch):
    """Another board with the same generic RP2040 id (talking, but not 8x8 frames) isn't taken for it."""
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    monkeypatch.setattr(rangefinder, "IDENTIFY_S", 0.4)
    other_m, other_s = os.openpty()
    real_m, real_s = os.openpty()
    other, real = os.ttyname(other_s), os.ttyname(real_s)
    s = MatrixSensor()
    monkeypatch.setattr(s, "_candidates", lambda: [p for p in (other, real) if p not in s.picker.rejected])
    s.start()
    try:
        for _ in range(60):
            os.write(other_m, b"hello from another pico\r\n")
            os.write(real_m, frame_text(grid(r3c3=800)))
            await asyncio.sleep(0.05)
            if s.present:
                break
        assert s.present and s.port.path == real and other in s.picker.rejected
        assert 0.75 < s.snapshot()["m"] < 0.8
    finally:
        await s.stop()
        for fd in (other_m, other_s, real_m, real_s):
            os.close(fd)


# --- the learnt floor ---

# As set after measuring the lens (24.5 cm) and the shoe test: 3.5 degrees down.
NOW = dict(height_m=0.245, tilt_deg=3.5)

def frames_of(n=20, **cells):
    return [[[cell_mm(v) for v in r] for r in grid(**{**FLOOR, **cells})] for _ in range(n)]


def learnt():
    s = sensor(**NOW)
    s.floor, _ = s.floor_from(frames_of())
    return s


def on_floor(**cells):
    """A frame of the floor as it read, with some cells changed (4000: no reading)."""
    return frame_text(grid(**{**FLOOR, **cells}))


def test_learning_the_floor_takes_the_cells_that_see_floor():
    s = sensor(**NOW)
    floor, why = s.floor_from(frames_of())
    assert floor is not None and "16 of 64" in why
    assert floor[7] == [430, 441, 441, 434, 440, 437, 433, 404] and floor[5] == [None] * 8


def test_learning_the_floor_wants_the_way_clear():
    s = sensor(**NOW)
    floor, why = s.floor_from(frames_of(r6c3=440))           # a shoe's toe 0.4 m ahead
    assert floor is None and "in the way" in why
    floor, why = s.floor_from(frames_of(r4c4=1150))          # its collar 1.1 m ahead: beyond 1 m, fine
    assert floor is not None
    floor, why = s.floor_from(frames_of(r7c2=4000))          # the bottom row not all seeing floor
    assert floor is None and "bottom row" in why
    assert s.floor_from(frames_of(5))[0] is None              # too few frames


def test_with_the_floor_learnt_height_is_told_from_it():
    s = learnt()
    # The corner reading a little short of even its own floor (381 against 404), steadily: the floor.
    s._on_data(on_floor(r7c7=381) * 3)
    assert s.gap_m is None
    # A shoe's toe 0.4 m ahead, seen by row 6 at 0.44 m (its floor there 0.61): 6.5 cm up, counted.
    s._on_data(on_floor(r6c3=440) * 3)
    assert s.gap_m is not None and 0.35 < s.gap_m < 0.45
    # A flip-flop's 0.53 against 0.61: 3 cm. Driven over.
    s._on_data(on_floor(r6c3=530) * 3)
    assert s.gap_m is None
    # Cells that never saw floor (row 5 and up) still go by height: the collar 0.75 m ahead.
    s._on_data(on_floor(r4c3=751) * 3)
    assert s.gap_m is not None and 0.7 < s.gap_m < 0.8


def test_kitchen_mats_are_driven_over():
    """A black rubber mat, 1-2 cm, on a vinyl floor (floor learnt on carpet): row 6 blank in every frame (black sends
    nothing back), the bottom row a little short and noisy, single frames up to 7 cm high. As it read there."""
    s = learnt()
    mat = dict(r7c0=378, r7c1=387, r7c2=403, r7c3=408, r7c4=414, r7c5=396, r7c6=393, r7c7=379,
               **{f"r6c{c}": 4000 for c in range(8)},
               **{f"r5c{c}": v for c, v in enumerate([4000, 854, 876, 885, 892, 900, 4000, 4000])})
    spikes = [dict(r7c0=290, r7c1=333), dict(r7c2=338, r7c7=340), dict(r7c3=375, r7c4=369), {}]
    for i in range(20):
        s._on_data(on_floor(**{**mat, **spikes[i % 4]}))
        assert s.gap_m is None, i
    # Something 10 cm tall on the mat, though, still counts.
    s._on_data(on_floor(**{**mat, "r5c3": 515}) * 2)
    assert s.gap_m is not None and s.gap_m < 0.55


def test_the_floor_is_kept_and_only_for_the_same_mount(tmp_path):
    f = tmp_path / "matrix-floor.json"
    s = sensor(**NOW, floor_file=str(f))
    assert s.floor is None
    s.floor, _ = s.floor_from(frames_of())
    s.floor_at = 1.0
    f.write_text(json.dumps({"mount": s._mount(), "at": 1.0, "cells": s.floor}))
    assert sensor(**NOW, floor_file=str(f)).floor == s.floor
    assert sensor(height_m=0.245, tilt_deg=5.0, floor_file=str(f)).floor is None      # tilted differently since
    f.write_text(json.dumps({"mount": s._mount(), "at": 1.0, "cells": s.floor, "steady": [[True] * 8] * 8}))
    assert sensor(**NOW, floor_file=str(f)).floor == s.floor                          # as saved by 1.6.1-11
    f.write_text("{not json")
    assert sensor(**NOW, floor_file=str(f)).floor is None
    f.write_text(json.dumps({"mount": s._mount(), "cells": [[9999] * 8] * 8}))
    assert sensor(**NOW, floor_file=str(f)).floor is None
    s.floor_file = f
    s.forget_floor()
    assert s.floor is None and not f.exists()


async def learning(s, still=lambda: True, **cells):
    """Learn the floor while frames come in, as from the sensor."""
    s.port = type("P", (), {"is_open": True})()
    task = asyncio.ensure_future(s.learn_floor(still))
    for _ in range(40):
        s._on_data(frame_text(grid(**{**FLOOR, **cells})))
        await asyncio.sleep(0.01)
        if task.done():
            break
    return await task


async def test_learning_the_floor_from_the_sensor(monkeypatch, tmp_path):
    from beamctl import matrixtof
    monkeypatch.setattr(matrixtof, "FLOOR_LEARN_S", 0.3)
    f = tmp_path / "matrix-floor.json"
    s = sensor(**NOW, floor_file=str(f))
    ok, why = await learning(s)
    assert ok and "16 of 64" in why and s.floor[6][3] == 612
    assert json.loads(f.read_text())["cells"] == s.floor and s.snapshot()["floor"]["cells"] == 16
    assert sensor(**NOW, floor_file=str(f)).floor == s.floor          # after a restart too
    ok, why = await learning(s, r6c3=440)                                # a shoe in the way: the old floor stays
    assert not ok and "in the way" in why and s.floor[6][3] == 612
    moved = iter([True, True, False])
    ok, why = await learning(s, still=lambda: next(moved, False))
    assert not ok and "moved" in why


async def test_the_server_learns_it_only_standing_still(monkeypatch):
    from beamctl import matrixtof
    monkeypatch.setattr(matrixtof, "FLOOR_LEARN_S", 0.3)
    c = MotorController(Config(device="/nonexistent", range_device=None, lidar_device=None,
                               matrix_height_m=0.245, matrix_tilt_deg=3.5))
    c.drive.target_linear = 0.1
    ok, why = await c.learn_matrix_floor("test")
    assert not ok and "stop the robot" in why
    c.drive.target_linear = 0.0
    c.matrix.port = type("P", (), {"is_open": True})()
    task = asyncio.ensure_future(c.learn_matrix_floor("test"))
    for _ in range(40):
        c.matrix._on_data(frame_text(grid(**FLOOR)))
        await asyncio.sleep(0.01)
        if task.done():
            break
    ok, why = await task
    assert ok and c.matrix.floor is not None



# --- low things that go out of view: nothing kept ---

def test_what_goes_out_of_view_is_gone():
    """It used to remember low things that went out of view (under it, or out to the side) and bring them closer as
    it drove: in narrow passages that held it still for what it had passed (a door frame's foot, someone's feet), and
    the AI gave up. Now only what's in view counts."""
    s = learnt()
    s._on_data(on_floor(r6c3=440) * 2)                   # a shoe's toe 0.4 m ahead, 6.5 cm up
    assert s.gap_m is not None and 0.35 < s.gap_m < 0.45
    s._on_data(on_floor() * 3)                           # under the view now
    assert s.gap_m is None and s.mm is None
    assert "remembered_m" not in (s.snapshot() or {})
