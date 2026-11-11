"""The 8x8 matrix distance sensor (DFRobot SEN0628, "Matrix LiDAR") on its own USB-C port; see docs/sensors.md.

Over USB it's a plain serial port (an RP2040, USB id 2e8a:000a, product "Pico") that talks all the time, about
15 frames a second, as text: eight lines "y0:" to "y7:", each with eight distances in mm and a trailing comma.
4000 means no reading in that cell. It's never written to.

The cells cover about 60 degrees each way. Each one is a direction, so with the sensor's height and tilt each
reading is a point in front of the robot: points on the floor are the floor, points in the robot's path above it
are obstacles. The obstacle stop takes the nearest of those, like one distance sensor straight ahead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path

from .rangefinder import RangeSensor
from .serialport import PortPicker

log = logging.getLogger("beam.range")

AUTO = "/dev/serial/by-id/usb-Raspberry_Pi_Pico_*"   # the RP2040's own USB port (generic id: checked by its frames)
SIZE = 8
NONE_MM = 4000                 # the sensor's "no reading"
MIN_MM = 20                    # closer than this counts as touching (0 mm), never as clear
FOV_DEG = 60.0                 # across the 8 columns, and down the 8 rows
MIN_HEIGHT_M = 0.05            # lower than this is floor: the robot drives over it (kitchen rubber mats, 1-2 cm, a
                               # threshold, a flat rug; a flip-flop too, which is fine)
FLOOR_PER_M = 0.03             # ...plus this much per metre away where it goes by geometry alone: floor seen at a
                               # slant reads a little short, most at the corners of the view
TOP_M = 1.6                    # and higher than the robot don't matter
MEDIAN_OF = 3                  # each cell is judged on the middle of its last 3 readings (blank in 2 of them: none).
                               # Floor seen at a slant is noisy: over a black kitchen mat single frames read up to
                               # 7 cm high, two in a row up to 5; the middle of three stayed under 2.5 (181 frames)

# The floor, learnt. Each cell covers a 7.5 degree cone, and on a slanted floor it reports the near edge of it, so the
# floor reads shorter than the cell's centre line says (on ours, 20% at the bottom row), and corner cells shorter
# still. So with the way ahead clear, the robot can learn what each cell reads as floor; from then on a cell that saw
# floor tells height by how much shorter than that it reads, instead of by geometry (which a corner cell fools).
FLOOR_LEARN_S = 3.0            # how long it watches the floor
FLOOR_SEEN = 0.7               # a cell has to read in this share of frames to have a floor
FLOOR_LEARN_Z_M = 0.06         # a reading this low (by geometry) is floor...
CLEAR_AHEAD_M = 1.0            # ...and anything higher within this far ahead means the way isn't clear


ROW = re.compile(rb"y([0-7]):\s*((?:\d+\s*,\s*){8})")


class Frames:
    """Text in, whole 8x8 frames out (rows in the order sent; a frame is y0..y7 in a row)."""

    def __init__(self):
        self.buf = b""
        self.rows: list[list[int]] = []

    def feed(self, data: bytes) -> list[list[list[int]]]:
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > 4096:                   # no newline in a long while: not this sensor
            self.buf = b""
        out = []
        for line in lines:
            m = ROW.search(line)
            if not m:
                self.rows = []
                continue
            n = int(m.group(1))
            if n != len(self.rows):                # out of step: start again at the next y0
                self.rows = []
                if n != 0:
                    continue
            self.rows.append([int(v) for v in m.group(2).split(b",")[:SIZE]])
            if len(self.rows) == SIZE:
                out.append(self.rows)
                self.rows = []
        return out



def cell_mm(v: int) -> int | None:
    """None = no reading, 0 = closer than it measures."""
    if v <= 0 or v >= NONE_MM:
        return None
    return v if v >= MIN_MM else 0


class MatrixSensor(RangeSensor):
    WHAT = "8x8 distance sensor"
    BAUD = None

    def __init__(self, device: str = "auto", height_m: float = 0.10, tilt_deg: float = 0.0,
                 to_front_m: float = 0.0, half_width_m: float = 0.25, flip: str = "", claimed: set[str] | None = None,
                 floor_file: str | None = None):
        """height_m: its lens above the floor; tilt_deg: how far it points down (negative: up); to_front_m: how far
        it sits behind the robot's front edge; half_width_m: the robot's path either side of its centre line;
        flip: "x" if left and right come out swapped, "y" if it's upside down, "xy" for both;
        floor_file: where the learnt floor is kept (see learn_floor)."""
        super().__init__(device, 0.0, claimed)
        self.picker = PortPicker(device or "auto", AUTO)
        self.height_m, self.tilt, self.to_front_m, self.half_width_m = height_m, math.radians(tilt_deg), to_front_m, half_width_m
        self.tilt_deg = tilt_deg
        self.flip = flip
        self.grid: list[list[int | None]] | None = None    # the latest frame, as seen from behind the sensor, top row first
        self.gap_m: float | None = None                    # the nearest obstacle in the path, from the front edge
        self._frames = Frames()
        step = math.radians(FOV_DEG / SIZE)
        self._dirs = [[self._direction((c - 3.5) * step, (3.5 - r) * step) for c in range(SIZE)] for r in range(SIZE)]
        self.floor: list[list[int | None]] | None = None   # each cell's floor reading (mm), where it sees floor
        self.floor_at: float | None = None                 # when it was learnt (unix time)
        self.floor_file = Path(floor_file) if floor_file else None
        self._learning: list | None = None                 # frames collected while learning the floor
        self._recent: list = []                            # the last MEDIAN_OF frames
        self._load_floor()

    # -- the learnt floor --

    def _mount(self) -> dict:
        return {"height_m": self.height_m, "tilt_deg": self.tilt_deg, "flip": self.flip}

    def _load_floor(self) -> None:
        if self.floor_file is None:
            return
        try:
            d = json.loads(self.floor_file.read_text())
            cells = d["cells"]
            ok = (len(cells) == SIZE and all(len(r) == SIZE for r in cells)
                  and all(v is None or (isinstance(v, int) and 0 < v < NONE_MM) for r in cells for v in r))
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("can't read the 8x8 sensor's floor from %s: %s", self.floor_file, e)
            return
        if not ok:
            log.warning("the 8x8 sensor's floor in %s isn't 8x8 readings; not using it", self.floor_file)
        elif d.get("mount") != self._mount():
            log.info("the 8x8 sensor's floor was learnt mounted differently; not using it (learn it again)")
        else:
            self.floor, self.floor_at = cells, d.get("at")

    def floor_from(self, grids: list[list[list[int | None]]]):
        """(each cell's floor reading, why) from frames of a clear floor; (None, why not) if it can't."""
        if len(grids) < 10:
            return None, "the 8x8 sensor sent too few frames"
        cells: list[list[int | None]] = [[None] * SIZE for _ in range(SIZE)]
        for r in range(SIZE):
            for c in range(SIZE):
                vals = sorted(g[r][c] for g in grids if g[r][c])
                if len(vals) < FLOOR_SEEN * len(grids):
                    continue
                mm = vals[len(vals) // 2]
                ahead, right, up = self._dirs[r][c]
                d = mm / 1000
                x, y, z = d * ahead, d * right, self.height_m + d * up
                if z < FLOOR_LEARN_Z_M:
                    cells[r][c] = mm
                elif 0 < x - self.to_front_m < CLEAR_AHEAD_M and abs(y) <= self.half_width_m and z < TOP_M:
                    return None, (f"something is in the way {x - self.to_front_m:.2f} m ahead: "
                                  f"clear the floor for {CLEAR_AHEAD_M:.0f} m in front and try again")
        if not all(cells[SIZE - 1]):
            return None, "the bottom row doesn't see the floor: is the sensor tilted, and the way ahead clear?"
        return cells, f"learnt the floor in {sum(1 for r in cells for v in r if v)} of 64 cells"

    async def learn_floor(self, still=lambda: True) -> tuple[bool, str]:
        """Watch the floor for FLOOR_LEARN_S (the robot standing still, the way ahead clear) and keep it.
        `still()` is asked as it goes: if the robot moves, it gives up."""
        if self._learning is not None:
            return False, "already learning the floor"
        if not self.present:
            return False, "the 8x8 sensor isn't there"
        self._learning = []
        try:
            end = time.monotonic() + FLOOR_LEARN_S
            while time.monotonic() < end:
                if not still():
                    return False, "the robot moved: keep it still while the floor is learnt"
                await asyncio.sleep(0.1)
            grids = self._learning
        finally:
            self._learning = None
        cells, why = self.floor_from(grids)
        if cells is None:
            return False, why
        self.floor, self.floor_at = cells, time.time()
        if self.floor_file is not None:
            try:
                tmp = self.floor_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"mount": self._mount(), "at": self.floor_at, "cells": cells}))
                tmp.replace(self.floor_file)
            except OSError as e:
                log.warning("can't save the 8x8 sensor's floor to %s: %s", self.floor_file, e)
                return True, why + " (but couldn't save it: it lasts until the server restarts)"
        log.info("8x8 sensor: %s", why)
        return True, why

    def forget_floor(self) -> None:
        self.floor = self.floor_at = None
        if self.floor_file is not None:
            try:
                self.floor_file.unlink(missing_ok=True)
            except OSError as e:
                log.warning("can't remove %s: %s", self.floor_file, e)
        log.info("8x8 sensor: floor forgotten; going by its height and tilt alone")

    def _direction(self, az: float, el: float) -> tuple[float, float, float]:
        """A cell's direction on the robot: (ahead, to the right, up) for 1 m along it."""
        f, right, up = math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)
        t = self.tilt
        return f * math.cos(t) + up * math.sin(t), right, -f * math.sin(t) + up * math.cos(t)

    def _on_data(self, data: bytes) -> None:
        frames = self._frames.feed(data)
        if not frames:
            return
        for rows in frames:                        # every frame, so "the last 3" really are
            if "y" in self.flip:
                rows = rows[::-1]
            if "x" in self.flip:
                rows = [r[::-1] for r in rows]
            self.grid = [[cell_mm(v) for v in r] for r in rows]
            self._recent = (self._recent + [self.grid])[-MEDIAN_OF:]
            # judged once it has two: one frame alone could be a flicker
            self.gap_m = self._scan(*self._recent) if len(self._recent) > 1 else None
            if self._learning is not None:
                self._learning.append(self.grid)
        self.mm = None if self.gap_m is None else round(self.gap_m * 1000)
        self.at = time.monotonic()
        self.frames += len(frames)

    def nearest(self, *frames: list[list[int | None]]) -> float | None:
        """The gap to the nearest thing in the robot's path, or None (see _scan)."""
        return self._scan(*frames)

    @staticmethod
    def cell(readings: list[int | None]) -> int | None:
        """A cell's reading from its last few: the middle one; None if it was blank in at least half of them.
        (Of two, the further: a short one alone could be noise.)"""
        got = sorted(v for v in readings if v is not None)
        if len(got) * 2 <= len(readings):
            return None
        return got[len(got) // 2]

    def _scan(self, *frames: list[list[int | None]]):
        """The gap to the nearest thing in the robot's path at least MIN_HEIGHT_M tall, or None; judged on the middle of
        each cell's readings in the frames given (the last few, oldest first). A cell that learnt the floor tells
        height by how much shorter than its floor it reads; the others by the sensor's height and tilt."""
        gaps = []
        floor = self.floor
        for r in range(SIZE):
            for c in range(SIZE):
                mm = self.cell([f[r][c] for f in frames])
                if mm is None:
                    continue
                ahead, right, up = self._dirs[r][c]
                d = mm / 1000
                x, y = d * ahead, d * right
                own = floor[r][c] if floor is not None else None
                if own is not None:
                    h = (own - mm) / 1000 * -up                       # how far above its floor, along its line
                    tall = h > MIN_HEIGHT_M
                else:
                    h = self.height_m + d * up
                    tall = MIN_HEIGHT_M + FLOOR_PER_M * d < h
                if x > 0 and abs(y) <= self.half_width_m and tall and h < TOP_M:
                    gaps.append(max(x - self.to_front_m, 0.0))
        return min(gaps) if gaps else None

    def snapshot(self) -> dict | None:
        s = super().snapshot()
        if s is None:
            return None
        return {**s, "grid": self.grid if s["fresh"] else None,
                "floor": None if self.floor is None else {"cells": sum(1 for r in self.floor for v in r if v),
                                                          "at": self.floor_at},
                "learning": self._learning is not None}

    def readings(self) -> list[dict]:
        s = RangeSensor.snapshot(self)
        return [] if s is None else [{**s, "angle": 0.0}]

