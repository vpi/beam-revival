"""Where the robot is on its floor map (lidarmap.py), from the lidar, wheels and gyro.

Tracking: between scans the pose moves by the wheels' distance and the gyro's turn. Every few centimetres or
degrees (or each second standing still) the latest lidar turn is matched against the map's walls, the same grid
search the map was built with, and the pose is corrected by what that found. The gyro alone drifts (it under-reads
turns by a few per cent: about 100 degrees over the 10 minute mapping drive), the walls don't.

On the charger the robot is on the dock, wherever tracking thought it was: the map's origin is the docked pose.

Finding itself (find): when tracking stops fitting (carried somewhere, woken away from the dock, the brain
restarted), one scan is tried everywhere on the free floor at every heading: the scan drawn as a small picture,
slid over the map's walls (cv2.filter2D) at 10 cm and 2 degrees, the best spots refined the tracking way. It
only answers if one spot clearly beats every other: a long bare corridor can look the same in many places.
While tracking fits only so-so, it also looks everywhere every few seconds and moves if somewhere else fits much
better: a wrong start (someone standing close when it found itself) otherwise keeps fitting just well enough.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from .lidarmap import FAST_TURN, RES, Grid, scan_points

log = logging.getLogger("beambrain.localize")

GYRO_SCALE = 1.037                 # the gyro under-reads turns (measured on the first real mapping drive)
STEP_M, STEP_DEG, STEP_S = 0.03, 2.0, 1.0   # match a scan when moved this much (or this long standing)
SEARCH_EVERY_S = 2.0               # a search takes ~0.3 s of the brain's time: not every scan
GOOD_FIT = 0.4                     # a match counts if this share of the scan falls on the map's walls (a door
                                   # shut that was open when mapped: about 0.5 near it)
UNSURE_AFTER, LOST_AFTER = 3, 8    # poor matches in a row
FIND_RES, FIND_STEP_DEG, FIND_RANGE_M = 0.10, 2.0, 6.0
FOUND_FIT = 0.6                    # finding itself needs a better fit than tracking does
AMBIGUOUS = 0.05                   # ...and no other place within this of it
# Tracking from a wrong start can keep fitting just well enough: while the recent fits are mediocre, look
# everywhere now and then, and move if somewhere else clearly fits much better.
CHECK_EVERY_S, CHECK_BELOW, BETTER_BY = 5.0, 0.75, 0.15
MIN_POINTS = 30


def compose(a, b):
    """Pose b given relative to pose a, in the map."""
    c, s = math.cos(a[2]), math.sin(a[2])
    return (a[0] + c * b[0] - s * b[1], a[1] + s * b[0] + c * b[1], a[2] + b[2])


def relative(a, b):
    """Pose b as seen from pose a."""
    c, s = math.cos(a[2]), math.sin(a[2])
    dx, dy = b[0] - a[0], b[1] - a[1]
    return (c * dx + s * dy, -s * dx + c * dy, b[2] - a[2])


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


@dataclass
class Job:
    """A scan to match, where the robot was thought to be when it was taken (None: search everywhere), and where
    the wheels and gyro alone had it then (to carry over what it moved while the match ran)."""
    pts: np.ndarray
    pose: tuple[float, float, float] | None
    odom: tuple[float, float, float]
    check: bool = False                # a search while tracking: is somewhere else much better?


class Localizer:
    def __init__(self, grid: Grid, dock: tuple[float, float, float] | None):
        self.grid, self.dock = grid, dock
        self.pose: tuple[float, float, float] | None = None
        self.state = "searching"           # "here", "unsure" (a few poor fits), "searching"
        self.fit = 0.0
        self.misses = 0
        self.odom = (0.0, 0.0, 0.0)        # the wheels and gyro alone, from wherever they started
        self._last = None                 # (yaw_deg, odometry_m) of the last telemetry
        self._matched_at = None            # (yaw_deg, odometry_m, t) of the last scan matched
        self._turn = None
        self._job: Job | None = None
        self.recent: deque[float] = deque(maxlen=5)   # the latest tracking fits
        self._checked_at = -math.inf
        self.grid.likelihood()            # once, up front: matching uses it from a worker thread

    # --- telemetry (cheap, on the brain's loop) ---

    def feed(self, t: dict, now: float) -> None:
        b, scan = t.get("board") or {}, t.get("scan")
        if "yaw_deg" not in b:
            return
        yaw, odo = float(b["yaw_deg"]), float(b.get("odometry_m", 0.0))
        if self._last is not None:
            dth = math.radians((yaw - self._last[0] + 180) % 360 - 180) * GYRO_SCALE
            ds = odo - self._last[1]
            step = (ds * math.cos(dth / 2), ds * math.sin(dth / 2), dth)
            self.odom = compose(self.odom, step)
            if self.pose is not None:
                self.pose = compose(self.pose, step)
        self._last = (yaw, odo)
        docked = bool(b.get("charging") or b.get("charger_detected")) and self.dock is not None
        if docked:
            if self.state != "here":
                log.info("localize: on the charger, so on the dock")
            self.pose, self.state, self.misses = self.dock, "here", 0
        if not scan or scan.get("turn") == self._turn or abs(float(b.get("actual_angular", 0.0))) > FAST_TURN:
            return
        m = self._matched_at
        if m is not None and self.state == "searching" and now - m[2] < SEARCH_EVERY_S:
            return
        if m is not None and self.state != "searching" and abs(odo - m[1]) < STEP_M \
                and abs((yaw - m[0] + 180) % 360 - 180) < STEP_DEG and now - m[2] < STEP_S:
            return
        pts = scan_points(scan["cm"])
        if len(pts) < MIN_POINTS:
            return
        self._turn, self._matched_at = scan.get("turn"), (yaw, odo, now)
        if self.state != "searching" and not docked and len(self.recent) == self.recent.maxlen \
                and float(np.mean(self.recent)) < CHECK_BELOW and now - self._checked_at > CHECK_EVERY_S:
            self._checked_at = now
            self._job = Job(pts, None, self.odom, check=True)
            return
        self._job = Job(pts, self.pose if self.state != "searching" else None, self.odom)

    def take(self) -> Job | None:
        job, self._job = self._job, None
        return job

    # --- matching (slow: in a worker thread) ---

    def match(self, job: Job):
        """(pose, fit) for a tracking job; for a search, the place found or None."""
        if job.pose is None:
            return find(self.grid, job.pts)
        return self.grid.match(job.pose, job.pts)

    # --- back on the loop ---

    def apply(self, job: Job, result) -> None:
        moved = relative(job.odom, self.odom)              # since the scan was taken
        if job.check:
            if result is None or self.pose is None or self.state == "searching":
                return
            pose, fit = result
            pose, now_fit = compose(pose, moved), float(np.mean(self.recent)) if self.recent else 0.0
            elsewhere = math.hypot(pose[0] - self.pose[0], pose[1] - self.pose[1]) > 0.5 \
                or abs(_wrap(pose[2] - self.pose[2])) > math.radians(20)
            if elsewhere and fit > now_fit + BETTER_BY:
                log.info("localize: fits much better elsewhere on the map (%.2f, not %.2f): moved there", fit, now_fit)
                self.pose, self.fit, self.state, self.misses = pose, fit, "here", 0
                self.recent.clear()
            return
        if job.pose is None:
            if result is not None and self.state == "searching":
                pose, self.fit = result
                self.pose, self.state, self.misses = compose(pose, moved), "here", 0
                log.info("localize: found on the map (fit %.2f)", self.fit)
            return
        pose, fit = result
        self.fit = fit
        if self.pose is None or self.state == "searching":
            return
        self.recent.append(fit)
        if fit >= GOOD_FIT:
            self.pose = compose(pose, moved)
            if self.state != "here":
                log.info("localize: fitting the map again (fit %.2f)", fit)
            self.state, self.misses = "here", 0
            return
        self.misses += 1
        if self.misses >= LOST_AFTER:
            log.info("localize: lost (%d poor fits in a row); looking for where it is", self.misses)
            self.state = "searching"
        elif self.misses >= UNSURE_AFTER and self.state == "here":
            self.state = "unsure"

    def snapshot(self) -> dict:
        if self.pose is None or self.state == "searching":
            return {"state": "searching"}
        return {"state": self.state, "x": round(self.pose[0], 3), "y": round(self.pose[1], 3),
                "th": round(math.degrees(_wrap(self.pose[2])), 1), "fit": round(self.fit, 2)}


# --- finding itself anywhere on the map ----------------------------------------------------------------------

_coarse_cache: dict[int, tuple] = {}


def _coarse(grid: Grid):
    """The map's likelihood and free floor at FIND_RES, cropped to what was seen."""
    key = id(grid.lo)
    if key not in _coarse_cache:
        k = round(FIND_RES / RES)
        field_, lo = grid.likelihood(), grid.lo
        seen = np.argwhere(np.abs(lo) > 0.3)
        (r0, c0), (r1, c1) = seen.min(0) // k * k, (seen.max(0) // k + 1) * k
        n_r, n_c = (r1 - r0) // k, (c1 - c0) // k
        f = field_[r0:r1, c0:c1].reshape(n_r, k, n_c, k).max((1, 3))
        free = (lo[r0:r1, c0:c1] < -0.3).reshape(n_r, k, n_c, k).mean((1, 3)) > 0.5
        _coarse_cache.clear()
        _coarse_cache[key] = (f, free, grid.origin + np.array([c0, r0]) * RES)
    return _coarse_cache[key]


def find(grid: Grid, pts: np.ndarray):
    """Where on the map this scan was taken: ((x, y, heading), fit), or None if nowhere clearly."""
    f, free, corner = _coarse(grid)
    near = pts[np.hypot(pts[:, 0], pts[:, 1]) < FIND_RANGE_M]
    if len(near) < MIN_POINTS:
        return None
    half = int(FIND_RANGE_M / FIND_RES) + 1
    candidates = []
    for deg in np.arange(0, 360, FIND_STEP_DEG):
        th = math.radians(deg)
        c, s = math.cos(th), math.sin(th)
        off = np.c_[c * near[:, 0] - s * near[:, 1], s * near[:, 0] + c * near[:, 1]]
        cells = np.round(off / FIND_RES).astype(int) + half
        kernel = np.zeros((2 * half + 1, 2 * half + 1), np.float32)
        np.add.at(kernel, (cells[:, 1], cells[:, 0]), 1.0 / len(near))
        score = cv2.filter2D(f, -1, kernel, anchor=(half, half), borderType=cv2.BORDER_CONSTANT)
        score[~free] = 0
        for i in np.argsort(score, axis=None)[-3:]:
            r, col = np.unravel_index(i, score.shape)
            candidates.append((float(score[r, col]), corner[0] + (col + 0.5) * FIND_RES,
                               corner[1] + (r + 0.5) * FIND_RES, th))
    candidates.sort(reverse=True)
    refined = []
    for _, x, y, th in candidates[:24]:
        pose, fit = grid.match((x, y, th), pts)
        refined.append((fit, pose))
    refined.sort(key=lambda r: -r[0])
    best_fit, best = refined[0]
    if best_fit < FOUND_FIT:
        return None
    for fit, pose in refined[1:]:
        far = math.hypot(pose[0] - best[0], pose[1] - best[1]) > 0.5 or abs(_wrap(pose[2] - best[2])) > math.radians(20)
        if far and fit > best_fit - AMBIGUOUS:
            return None                    # two places look alike: don't guess
    return best, best_fit
