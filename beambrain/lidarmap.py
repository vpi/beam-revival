"""A floor map from the robot's own lidar: drive it round once, it works out the walls and furniture.

Recording (Recorder): while a person drives, every few centimetres or degrees of movement the brain keeps the
lidar's latest turn (1 degree steps, from the robot's telemetry) with the gyro heading and wheel odometry. Saved
on the brain only (~/beam-brain/maps/): a floor plan of someone's home is private.

Building (build): the classic way. Each scan's pose starts from the previous one moved by the wheels and gyro,
then is nudged (a grid search over position and heading) until its points best fit the walls mapped so far
(a likelihood field: how near each point falls to a wall). Then the scan is drawn into an occupancy grid: free
along each beam, occupied at its end. Scans taken while turning fast are skipped (the lidar turns 10 times a
second, so they come out smeared), the pose is still carried through them.

  python3 -m beambrain.lidarmap ~/beam-brain/maps/<recording>.jsonl.gz    # (re)build a recording
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("beambrain.lidarmap")

LIDAR_BEHIND_AXLE_M = -0.035       # measured: 6 cm behind the base's front edge, the axle 9.5 cm: so 3.5 cm ahead
RES = 0.05                         # metres per map cell
MIN_M, MAX_M = 0.15, 8.0           # lidar readings used
KEEP_EVERY_M, KEEP_EVERY_DEG, KEEP_EVERY_S = 0.05, 3.0, 2.0
FAST_TURN = 0.35                   # rad/s: scans turning faster than this aren't matched or drawn
SEARCH_M, SEARCH_DEG = 0.20, 6.0   # how far a scan's pose may be nudged from the wheels' and gyro's
SIGMA = 0.05                       # metres: how near a point must fall to a wall to count


# --- recording ---------------------------------------------------------------------------------------------

class Recorder:
    """Keeps scans from the robot's telemetry while mapping."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.started = time.time()
        self._last_turn = None
        self._last = None
        self.metres = 0.0

    def feed(self, t: dict) -> bool:
        """One telemetry message. True if it kept a scan."""
        scan, b = t.get("scan"), t.get("board") or {}
        if not scan or scan.get("turn") == self._last_turn or "yaw_deg" not in b:
            return False
        yaw, odo = float(b["yaw_deg"]), float(b.get("odometry_m", 0.0))
        if self._last is not None:
            dyaw = abs((yaw - self._last[0] + 180) % 360 - 180)
            dodo = abs(odo - self._last[1])
            if dodo < KEEP_EVERY_M and dyaw < KEEP_EVERY_DEG and time.monotonic() - self._last[2] < KEEP_EVERY_S:
                return False
            self.metres += dodo
        self._last_turn, self._last = scan["turn"], (yaw, odo, time.monotonic())
        self.rows.append({"t": round(time.time() - self.started, 2), "yaw": round(yaw, 2), "odo": round(odo, 4),
                          "ang": round(float(b.get("actual_angular", 0.0)), 3),
                          "charging": bool(b.get("charging") or b.get("charger_detected")), "cm": scan["cm"]})
        return True

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / time.strftime("drive-%Y%m%d-%H%M%S.jsonl.gz", time.localtime(self.started))
        with gzip.open(path, "wt") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")
        return path


def load(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


# --- building ----------------------------------------------------------------------------------------------

def scan_points(cm: list[int]) -> np.ndarray:
    """A scan as points in the robot's frame (metres: x ahead of the axle, y left). Angles go clockwise."""
    d = np.asarray(cm, float) / 100
    a = np.radians(np.arange(len(d)) * 360 / len(d))
    ok = (d >= MIN_M) & (d <= MAX_M)
    return np.c_[d[ok] * np.cos(a[ok]) - LIDAR_BEHIND_AXLE_M, -d[ok] * np.sin(a[ok])]


def _to_world(pts: np.ndarray, pose) -> np.ndarray:
    x, y, th = pose
    c, s = math.cos(th), math.sin(th)
    return np.c_[x + c * pts[:, 0] - s * pts[:, 1], y + s * pts[:, 0] + c * pts[:, 1]]


class Grid:
    """An occupancy grid in log-odds, big enough for a flat (40 m square), the start in the middle."""
    SIZE = 800
    HIT, MISS, CLAMP = 0.9, -0.4, 6.0

    def __init__(self) -> None:
        self.lo = np.zeros((self.SIZE, self.SIZE), np.float32)
        self.origin = np.array([-self.SIZE * RES / 2, -self.SIZE * RES / 2])   # world metres of cell (0, 0)
        self._field = None

    def cell(self, xy: np.ndarray) -> np.ndarray:
        return np.floor((xy - self.origin) / RES).astype(int)

    def add(self, pose, pts: np.ndarray) -> None:
        """Free along each beam, occupied at its end."""
        sensor = self.cell(np.array([[pose[0] - LIDAR_BEHIND_AXLE_M * math.cos(pose[2]),
                                      pose[1] - LIDAR_BEHIND_AXLE_M * math.sin(pose[2])]]))[0]
        ends = self.cell(_to_world(pts, pose))
        free = np.zeros(self.lo.shape, np.uint8)
        for e in ends:
            cv2.line(free, (int(sensor[0]), int(sensor[1])), (int(e[0]), int(e[1])), 1)
        hit = np.zeros(self.lo.shape, np.uint8)
        inside = (ends >= 0).all(1) & (ends < self.SIZE).all(1)
        hit[ends[inside, 1], ends[inside, 0]] = 1
        free[hit == 1] = 0
        self.lo += free * self.MISS + hit * self.HIT
        np.clip(self.lo, -self.CLAMP, self.CLAMP, out=self.lo)
        self._field = None

    def likelihood(self) -> np.ndarray:
        """Per cell, how close the nearest wall is (1 on a wall, falling off over SIGMA)."""
        if self._field is None:
            walls = (self.lo > 0.5).astype(np.uint8)              # seen once is enough to match against
            dist = cv2.distanceTransform(1 - walls, cv2.DIST_L2, 5) * RES
            self._field = np.exp(-(dist ** 2) / (2 * SIGMA ** 2)).astype(np.float32)
        return self._field

    def match(self, pose, pts: np.ndarray) -> tuple[tuple[float, float, float], float]:
        """The pose near `pose` where the scan best fits the walls so far: coarse, then fine. And how well
        (the share of points on a wall)."""
        field_ = self.likelihood()
        best, best_score = pose, -1.0
        for step_m, step_deg, span_m, span_deg in ((0.04, 1.0, SEARCH_M, SEARCH_DEG), (0.01, 0.25, 0.04, 1.0)):
            x0, y0, th0 = best
            offs = np.arange(-span_m, span_m + 1e-9, step_m)
            dx, dy = np.meshgrid(offs, offs)
            dx, dy = dx.ravel(), dy.ravel()
            for dth in np.radians(np.arange(-span_deg, span_deg + 1e-9, step_deg)):
                w = _to_world(pts, (x0, y0, th0 + dth))
                cx = np.floor((w[:, 0][None, :] + dx[:, None] - self.origin[0]) / RES).astype(int)
                cy = np.floor((w[:, 1][None, :] + dy[:, None] - self.origin[1]) / RES).astype(int)
                np.clip(cx, 0, self.SIZE - 1, out=cx)
                np.clip(cy, 0, self.SIZE - 1, out=cy)
                score = field_[cy, cx].mean(1)
                # A little pull towards where the wheels and gyro put it: ties don't wander.
                score -= 0.02 * ((dx + x0 - pose[0]) ** 2 + (dy + y0 - pose[1]) ** 2) / SEARCH_M ** 2
                i = int(np.argmax(score))
                if score[i] > best_score:
                    best_score, best = float(score[i]), (x0 + dx[i], y0 + dy[i], th0 + dth)
        return best, best_score


@dataclass
class Built:
    grid: Grid
    poses: list[tuple[float, float, float]] = field(default_factory=list)
    dock: tuple[float, float, float] | None = None
    used: int = 0
    fit: float = 0.0


def build(rows: list[dict]) -> Built:
    """The map from a recording. The first pose is the origin, heading along +x."""
    grid = Grid()
    out = Built(grid)
    pose = None
    fits = []
    for i, r in enumerate(rows):
        yaw = math.radians(r["yaw"])
        if pose is None:
            pose, prev = (0.0, 0.0, 0.0), r
            yaw0 = yaw
        else:
            # Moved since the last scan: the wheels' distance along the mean heading, the gyro's turn.
            dth = math.radians((r["yaw"] - prev["yaw"] + 180) % 360 - 180)
            ds = r["odo"] - prev["odo"]
            mid = pose[2] + dth / 2
            pose = (pose[0] + ds * math.cos(mid), pose[1] + ds * math.sin(mid), pose[2] + dth)
            prev = r
        pts = scan_points(r["cm"])
        if abs(r.get("ang", 0.0)) > FAST_TURN or len(pts) < 30:
            out.poses.append(pose)
            continue
        if out.used:
            pose, fit = grid.match(pose, pts)
            fits.append(fit)
        grid.add(pose, pts)
        out.used += 1
        out.poses.append(pose)
        if r.get("charging") and out.dock is None:
            out.dock = pose
    out.fit = float(np.median(fits)) if fits else 0.0
    return out


def save_grid(b: Built, path: Path) -> None:
    """The occupancy grid and the dock, for finding the robot on the map later (localize.py)."""
    np.savez_compressed(path, lo=b.grid.lo, origin=b.grid.origin,
                        dock=np.array(b.dock if b.dock is not None else (np.nan,) * 3))


def load_grid(path: Path) -> tuple[Grid, tuple[float, float, float] | None]:
    with np.load(path) as z:
        g = Grid()
        g.lo, g.origin = z["lo"].astype(np.float32), z["origin"]
        dock = tuple(float(v) for v in z["dock"])
    return g, None if math.isnan(dock[0]) else dock


# --- the picture -------------------------------------------------------------------------------------------

def render(b: Built, scale: int = 2) -> tuple[bytes, dict]:
    """The map as a PNG (walls dark, free floor light, unknown grey, the drive as a line, the dock as a dot),
    cropped to what was seen, and where it sits (metres) so a page can put things on it."""
    lo = b.grid.lo
    seen = np.abs(lo) > 0.3
    ys, xs = np.nonzero(seen)
    if not len(xs):
        x0 = y0 = 0
        x1 = y1 = 1
    else:
        x0, x1 = max(xs.min() - 4, 0), min(xs.max() + 5, lo.shape[1])
        y0, y1 = max(ys.min() - 4, 0), min(ys.max() + 5, lo.shape[0])
    crop = lo[y0:y1, x0:x1]
    img = np.full(crop.shape + (3,), 200, np.uint8)               # unknown
    img[crop < -0.3] = (246, 244, 240)                            # free
    img[crop > 1.0] = (60, 52, 46)                                # wall
    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)

    def px(x, y):
        c = (np.array([x, y]) - b.grid.origin) / RES - [x0, y0]
        return int(c[0] * scale), int(c[1] * scale)
    trail = [px(p[0], p[1]) for p in b.poses]
    for a, c in zip(trail, trail[1:]):
        cv2.line(img, a, c, (220, 140, 40), 1, cv2.LINE_AA)
    if b.dock is not None:
        cv2.circle(img, px(b.dock[0], b.dock[1]), 4 * scale, (60, 170, 60), -1, cv2.LINE_AA)
    img = cv2.flip(img, 0)                                        # north up: y grows upwards on screen
    ok, png = cv2.imencode(".png", img)
    info = {"res": RES / scale, "width": img.shape[1], "height": img.shape[0],
            "x0": float(b.grid.origin[0] + x0 * RES), "y0": float(b.grid.origin[1] + y0 * RES),
            "scans": b.used, "fit": round(b.fit, 2), "dock": b.dock is not None}
    return png.tobytes(), info


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording")
    ap.add_argument("--out", help="PNG to write (default: next to the recording)")
    args = ap.parse_args()
    t0 = time.monotonic()
    b = build(load(Path(args.recording)))
    png, info = render(b)
    out = Path(args.out or str(args.recording).replace(".jsonl.gz", ".png"))
    out.write_bytes(png)
    print(f"{b.used} scans in {time.monotonic() - t0:.1f} s, median fit {b.fit:.2f}; map {info['width']}x{info['height']} -> {out}")


if __name__ == "__main__":
    main()
