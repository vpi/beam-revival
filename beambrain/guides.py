"""Visual aids drawn onto what the AI sees: angle and distance marks, and a breadcrumb map.

Vision models are poor at turning "a bit to the left in the picture" into degrees, or judging how far
away the floor is. These overlays give them the numbers directly. Camera geometry is configurable so
the same code works for the simulator and (once measured) the real Beam+ cameras.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class HeadGeometry:
    fov_deg: float = 68.0            # horizontal field of view
    height_m: float = 1.25           # camera height above the floor
    pitch_down_deg: float = 8.0      # downward tilt


@dataclass
class FloorGeometry:
    near_m: float = 0.25             # bottom edge of the floor image, metres ahead of the robot centre
    far_m: float = 1.75              # top edge


ANGLES = (30, 15, 0, -15, -30)       # positive = left, like turn()
HEAD_DISTANCES = (2, 3, 5)
FLOOR_DISTANCES = (0.5, 1.0, 1.5)
GUIDE = (0, 255, 255)                # yellow (BGR): stands out from walls and floors


def _decode(jpeg: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def _encode(img: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def _label(img, text, org, scale=0.5):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, GUIDE, 1, cv2.LINE_AA)


def angle_x(angle_deg: float, width: int, fov_deg: float) -> int:
    """Image column of a direction `angle_deg` to the LEFT of straight ahead."""
    f = (width / 2) / math.tan(math.radians(fov_deg) / 2)
    return int(round(width / 2 - f * math.tan(math.radians(angle_deg))))


def annotate_head(jpeg: bytes, g: HeadGeometry = HeadGeometry()) -> bytes:
    img = _decode(jpeg)
    h, w = img.shape[:2]
    f = (w / 2) / math.tan(math.radians(g.fov_deg) / 2)
    tick = max(14, h // 18)
    for a in ANGLES:
        x = angle_x(a, w, g.fov_deg)
        # Short marks at top and bottom, a dotted line between: guidance without hiding the view.
        cv2.line(img, (x, 0), (x, tick), GUIDE, 2)
        cv2.line(img, (x, h - tick), (x, h), GUIDE, 2)
        for y in range(tick, h - tick, 12):
            cv2.line(img, (x, y), (x, y + 3), GUIDE, 1)
        _label(img, f"{a:+d}" if a else "0", (x - 14 if a else x - 5, tick + 16))
    # Floor distance marks at the image edges: rows where the floor is d metres ahead.
    horizon = h / 2 - f * math.tan(math.radians(g.pitch_down_deg))
    for d in HEAD_DISTANCES:
        y = int(horizon + f * g.height_m / d)
        if horizon < y < h - 4:
            cv2.line(img, (0, y), (22, y), GUIDE, 2)
            cv2.line(img, (w - 22, y), (w, y), GUIDE, 2)
            _label(img, f"{d}m", (26, y + 5), 0.45)
    return _encode(img)


def annotate_floor(jpeg: bytes, g: FloorGeometry = FloorGeometry()) -> bytes:
    img = _decode(jpeg)
    h, w = img.shape[:2]
    for d in FLOOR_DISTANCES:
        if g.near_m < d < g.far_m:
            y = int(h * (g.far_m - d) / (g.far_m - g.near_m))
            for x in range(0, w, 14):
                cv2.line(img, (x, y), (x + 7, y), GUIDE, 1)
            _label(img, f"{d:g} m ahead", (4, y - 4), 0.45)
    return _encode(img)


@dataclass
class Breadcrumbs:
    """Dead-reckoned path from the robot's own odometry and gyro (no map knowledge needed)."""
    x: float = 0.0
    y: float = 0.0
    heading_deg: float = 0.0            # relative to the heading at the start
    path: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 0.0)])
    bumps: list[tuple[float, float]] = field(default_factory=list)
    _odo: float | None = None
    _yaw0: float | None = None

    def update(self, odometry_m: float, yaw_deg: float) -> None:
        if self._odo is None:
            self._odo, self._yaw0 = odometry_m, yaw_deg
        self.heading_deg = (yaw_deg - self._yaw0 + 180) % 360 - 180
        ds = odometry_m - self._odo
        self._odo = odometry_m
        if abs(ds) > 1e-4:
            th = math.radians(self.heading_deg)
            self.x += ds * math.cos(th)
            self.y += ds * math.sin(th)
            px, py = self.path[-1]
            if math.hypot(self.x - px, self.y - py) > 0.1:
                self.path.append((self.x, self.y))

    def bump(self) -> None:
        self.bumps.append((self.x, self.y))

    def describe(self) -> str:
        """Position in words, in the start frame: 'ahead' = the way the robot faced at the start."""
        fwd, left = self.x, self.y
        parts = [f"{abs(fwd):.1f} m {'ahead of' if fwd >= 0 else 'behind'} the start",
                 f"{abs(left):.1f} m to its {'left' if left >= 0 else 'right'}"]
        return (f"{parts[0]} and {parts[1]} (directions as you faced at the start), facing "
                f"{self.heading_deg:+.0f}° from your starting direction")

    def render(self, size: int = 320) -> bytes:
        """Top-down sketch: start (S), path, bumps (X), robot and its direction. Up = starting direction."""
        img = np.full((size, size, 3), 250, np.uint8)
        pts = self.path + [(self.x, self.y)] + self.bumps
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        span = max(6.0, max(xs) - min(xs) + 2, max(ys) - min(ys) + 2)
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        k = size / span

        # Start frame: forward (x) is up the image, left (y) is to the left.
        def px(p):
            return int(size / 2 - (p[1] - cy) * k), int(size / 2 - (p[0] - cx) * k)

        for i in range(math.floor(cx - span), math.ceil(cx + span) + 1):      # 1 m grid: rows
            row = px((i, cy))[1]
            cv2.line(img, (0, row), (size, row), (225, 225, 225), 1)
        for j in range(math.floor(cy - span), math.ceil(cy + span) + 1):      # ...and columns
            col = px((cx, j))[0]
            cv2.line(img, (col, 0), (col, size), (225, 225, 225), 1)
        if len(self.path) > 1:
            cv2.polylines(img, [np.array([px(p) for p in self.path + [(self.x, self.y)]], np.int32)], False,
                          (220, 140, 0), 2)
        s = px((0, 0))
        cv2.putText(img, "S", (s[0] - 6, s[1] + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 0), 2)
        for b in self.bumps:
            q = px(b)
            cv2.line(img, (q[0] - 6, q[1] - 6), (q[0] + 6, q[1] + 6), (0, 0, 220), 2)
            cv2.line(img, (q[0] - 6, q[1] + 6), (q[0] + 6, q[1] - 6), (0, 0, 220), 2)
        r = px((self.x, self.y))
        th = math.radians(self.heading_deg)
        tip = px((self.x + 0.6 * math.cos(th), self.y + 0.6 * math.sin(th)))
        cv2.circle(img, r, 7, (40, 40, 40), -1)
        cv2.arrowedLine(img, r, tip, (40, 40, 40), 2, tipLength=0.35)
        cv2.putText(img, "1 square = 1 m. Up = your starting direction.", (6, size - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (90, 90, 90), 1, cv2.LINE_AA)
        cv2.putText(img, "S start  blue: your path  X: bumped  arrow: you", (6, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (90, 90, 90), 1, cv2.LINE_AA)
        return _encode(img)
