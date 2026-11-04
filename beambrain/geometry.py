"""Image boxes -> where things are on the floor, from the cameras' geometry.

The head camera: a point on the floor at image row y is d metres ahead (pinhole model with the
camera's height and downward tilt). The floor camera: a straight-down view of a known floor patch.
Defaults match the simulator. A real Beam+ is measured once with printed markers (docs/ai-driving.md,
"Measuring the cameras"); CameraModel.load() then uses that instead:
  head:  lens height, position, yaw/pitch/roll, focal length and barrel distortion (a ray per pixel)
  floor: the floor camera's fisheye, undone radially, then a homography onto the floor
All distances are from the robot's centre (its wheel axle, what it turns around).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .grounding import Box


@dataclass
class CameraModel:
    head_fov_deg: float = 68.0
    head_height_m: float = 1.25
    head_pitch_down_deg: float = 8.0
    head_w: int = 640
    head_h: int = 480
    floor_near_m: float = 0.25          # floor camera: bottom edge, metres ahead of the robot centre
    floor_far_m: float = 1.75           # top edge
    floor_half_width_m: float = 0.9
    # Measured on a real robot (None = the simulator's cameras above). See load().
    head: dict | None = field(default=None, repr=False)
    floor: dict | None = field(default=None, repr=False)
    axle_to_front_m: float = 0.0        # the measurements are from the base's front edge

    @classmethod
    def load(cls, path: str | Path) -> "CameraModel":
        """The robot's measured cameras, from a cameras.json written by `python3 -m beambrain.calibrate`."""
        c = json.loads(Path(path).read_text())
        return cls(head=c["head"], floor=c["floor"], axle_to_front_m=c["axle_to_front_m"],
                   head_w=c["head"]["w"], head_h=c["head"]["h"])

    def _head_ray(self, u: float, v: float) -> tuple[float, float, float]:
        """Measured head camera: the direction (ahead, left, up) seen at pixel (u, v)."""
        h = self.head
        xd, yd = (u - h["w"] / 2) / h["f"], (v - h["h"] / 2) / h["f"]
        rd = math.hypot(xd, yd)
        r = rd
        for _ in range(20):                              # undo the barrel: rd = r (1 + k1 r^2)
            r = rd / (1 + h["k1"] * r * r)
        s = r / rd if rd > 1e-12 else 1.0
        x, y = xd * s, yd * s
        cr, sr = math.cos(h["roll"]), math.sin(h["roll"])
        x, y = cr * x + sr * y, -sr * x + cr * y         # undo roll
        b0, b1, b2 = 1.0, -x, -y                         # camera (right, down, forward) -> (ahead, left, up)
        cp, sp = math.cos(h["pitch_down"]), math.sin(h["pitch_down"])
        a0, a1, a2 = cp * b0 + sp * b2, b1, -sp * b0 + cp * b2
        cy, sy = math.cos(h["yaw"]), math.sin(h["yaw"])
        return cy * a0 - sy * a1, sy * a0 + cy * a1, a2

    def head_floor_point(self, x_frac: float, y_frac: float) -> tuple[float, float] | None:
        """Measured head camera: (metres ahead of the robot centre, metres left) of the floor at that image
        point, or None if it's at or above the horizon."""
        ahead, left, up = self._head_ray(x_frac * self.head["w"], y_frac * self.head["h"])
        if up > -1e-3:
            return None
        t = self.head["height"] / -up
        return self.head["cam_x_from_front"] + t * ahead + self.axle_to_front_m, t * left

    def head_angle_left(self, x_frac: float) -> float:
        """Degrees to turn LEFT to face image column x (0..1)."""
        if self.head is not None:
            ahead, left, _ = self._head_ray(x_frac * self.head["w"], self.head["h"] / 2)
            return math.degrees(math.atan2(left, ahead))
        return -math.degrees(math.atan((x_frac - 0.5) * 2 * math.tan(math.radians(self.head_fov_deg / 2))))

    def head_floor_distance(self, y_frac: float) -> float | None:
        """Metres ahead to the floor point seen at image row y (0..1), or None if at/above the horizon."""
        if self.head is not None:
            p = self.head_floor_point(0.5, y_frac)
            return None if p is None else p[0]
        f = (self.head_w / 2) / math.tan(math.radians(self.head_fov_deg / 2))
        horizon = self.head_h / 2 - f * math.tan(math.radians(self.head_pitch_down_deg))
        y = y_frac * self.head_h
        if y <= horizon + 2:
            return None
        return self.head_height_m * f / (y - horizon)

    @property
    def head_min_floor_m(self) -> float:
        """The nearest floor the head camera can see (bottom edge of the image)."""
        return self.head_floor_distance(1.0)

    def floor_point(self, x_frac: float, y_frac: float) -> tuple[float, float]:
        """(metres ahead, metres to the LEFT) of the floor camera image point (0..1, 0..1)."""
        if self.floor is not None:
            fl = self.floor
            w, cx, cy = fl["w"], fl["centre"][0], fl["centre"][1]
            qx, qy = (x_frac * w - cx) / w, (y_frac * fl["h"] - cy) / w
            r2 = qx * qx + qy * qy
            g = 1 + fl["k"][0] * r2 + fl["k"][1] * r2 * r2
            u, v = cx + qx * w * g, cy + qy * w * g
            H = fl["H"]
            den = H[2][0] * u + H[2][1] * v + H[2][2]
            ahead = (H[0][0] * u + H[0][1] * v + H[0][2]) / den
            left = (H[1][0] * u + H[1][1] * v + H[1][2]) / den
            return ahead + self.axle_to_front_m, left
        ahead = self.floor_far_m - y_frac * (self.floor_far_m - self.floor_near_m)
        left = -(x_frac - 0.5) * 2 * self.floor_half_width_m
        return ahead, left


def target_from_head(box: Box, cam: CameraModel) -> tuple[float, float | None]:
    """(angle to turn left, distance to the target's base or None if its base is below the image)."""
    angle = cam.head_angle_left(box.cx)
    if cam.head is not None:                    # measured camera: the base's own floor point, straight-line away
        p = None if box.bottom >= 0.97 else cam.head_floor_point(box.cx, box.bottom)
        return angle, None if p is None else math.hypot(*p)
    dist = None if box.bottom >= 0.97 else cam.head_floor_distance(box.bottom)
    return angle, dist


def target_from_floor(box: Box, cam: CameraModel) -> tuple[float, float]:
    """(angle to turn left, distance from the robot centre to the target's near edge)."""
    ahead_c, left_c = cam.floor_point(box.cx, (box.y0 + box.y1) / 2)
    near_edge, _ = cam.floor_point(box.cx, box.y1)
    return math.degrees(math.atan2(left_c, ahead_c)), near_edge
