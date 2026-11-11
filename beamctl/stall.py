"""Blocked wheels: the robot is told to move, but its wheels don't turn (a shoe under one, wedged against a low
step the sensors don't see). Then it stops trying in that direction, for every driver, until the driver lets go
or goes the other way.

Measured from the wheel encoders, with Beam's own formulas (protocol.py: linear = (L - R) * k1, angular =
-(L + R) * k2), the same counts the brain's map and odometry use. Not the board's actual_linear, which may not be
measured on a Beam+ (docs/motor-protocol.md, open questions).

A light thing pushed along (a shoe sliding on tiles) doesn't stall the wheels: this can't see that.
"""

from __future__ import annotations

import logging
import math
from collections import deque

from . import protocol as p

log = logging.getLogger("beamctl.stall")

MIN_LINEAR = 0.08     # m/s: slower than this isn't checked (the board may not move at a crawl on a thick rug)
MIN_ANGULAR = 0.30    # rad/s, turning on the spot
FRACTION = 0.25       # moving at less than this part of what it's told: not getting anywhere
HOLD_S = 1.0          # ...for this long: blocked (time for the board's own ramp and the robot to start)
WINDOW_S = 0.5        # wheel speed measured over this


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


class StallWatch:
    def __init__(self, hold_s: float = HOLD_S):
        self.hold_s = hold_s
        self._samples: deque[tuple[float, int, int]] = deque()
        self._since: dict[str, float | None] = {"linear": None, "angular": None}
        self.measured: tuple[float, float] | None = None     # (m/s, rad/s) from the encoders
        self.blocked: dict[str, int] = {}                    # {"linear": +1 forward / -1 back, "angular": ...}
        self.count = 0                                       # times it held, since start

    def update(self, now: float, enc_l: int, enc_r: int, linear: float, angular: float) -> None:
        """One board status: its encoder counts, and what the robot was told (what the server sent)."""
        s = self._samples
        s.append((now, enc_l, enc_r))
        while len(s) > 2 and now - s[1][0] >= WINDOW_S:
            s.popleft()
        t0, l0, r0 = s[0]
        dt = now - t0
        if dt < 0.6 * WINDOW_S:
            self.measured = None
            return
        dl, dr = enc_l - l0, enc_r - r0
        lin = (dl - dr) * p.ENCODER_LINEAR_SCALE / dt
        ang = -(dl + dr) * p.ENCODER_ANGULAR_SCALE / dt
        self.measured = (lin, ang)
        turning_on_spot = abs(linear) < 0.03
        for axis, told, got, least, check in (
                ("linear", linear, lin, MIN_LINEAR, True),
                ("angular", angular, ang, MIN_ANGULAR, turning_on_spot)):
            stuck = check and abs(told) >= least and (_sign(got) != _sign(told) or abs(got) < FRACTION * abs(told))
            if not stuck:
                self._since[axis] = None
                continue
            since = self._since[axis] = self._since[axis] or now
            if now - since >= self.hold_s and axis not in self.blocked:
                self.blocked[axis] = _sign(told)
                self.count += 1
                log.warning("wheels blocked: told to %s at %.2f, measured %.2f for %.1f s; holding that way",
                            "drive" if axis == "linear" else "turn", told, got, now - since)

    def limit(self, linear: float, angular: float) -> tuple[float, float]:
        """The driver's wish, minus the blocked way. Asking for nothing there, or the other way, clears it."""
        for axis in list(self.blocked):
            want = linear if axis == "linear" else angular
            if _sign(want) != self.blocked[axis]:
                del self.blocked[axis]
                self._since[axis] = None
                log.info("wheels: no longer held (%s)", axis)
            elif axis == "linear":
                linear = 0.0
            else:
                angular = 0.0
        return linear, angular

    def reset(self) -> None:
        """The link to the board was lost: the counts start over."""
        self._samples.clear()
        self.measured = None
        self._since = {"linear": None, "angular": None}

    def snapshot(self) -> dict:
        m = self.measured
        return {"measured_linear": None if m is None else round(m[0], 3),
                "measured_angular": None if m is None or not math.isfinite(m[1]) else round(m[1], 3),
                "blocked": dict(self.blocked) or None, "count": self.count}
