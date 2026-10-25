"""Image boxes -> where things are on the floor, from the cameras' geometry.

The head camera: a point on the floor at image row y is d metres ahead (pinhole model with the
camera's height and downward tilt). The floor camera: a straight-down view of a known floor patch.
Defaults match the simulator; for the real Beam+ these need measuring once (calibration).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

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

    def head_angle_left(self, x_frac: float) -> float:
        """Degrees to turn LEFT to face image column x (0..1)."""
        return -math.degrees(math.atan((x_frac - 0.5) * 2 * math.tan(math.radians(self.head_fov_deg / 2))))

    def head_floor_distance(self, y_frac: float) -> float | None:
        """Metres ahead to the floor point seen at image row y (0..1), or None if at/above the horizon."""
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
        ahead = self.floor_far_m - y_frac * (self.floor_far_m - self.floor_near_m)
        left = -(x_frac - 0.5) * 2 * self.floor_half_width_m
        return ahead, left


def target_from_head(box: Box, cam: CameraModel) -> tuple[float, float | None]:
    """(angle to turn left, distance to the target's base or None if its base is below the image)."""
    angle = cam.head_angle_left(box.cx)
    dist = None if box.bottom >= 0.97 else cam.head_floor_distance(box.bottom)
    return angle, dist


def target_from_floor(box: Box, cam: CameraModel) -> tuple[float, float]:
    """(angle to turn left, distance from the robot centre to the target's near edge)."""
    ahead_c, left_c = cam.floor_point(box.cx, (box.y0 + box.y1) / 2)
    near_edge, _ = cam.floor_point(box.cx, box.y1)
    return math.degrees(math.atan2(left_c, ahead_c)), near_edge
