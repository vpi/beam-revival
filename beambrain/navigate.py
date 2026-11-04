"""Driving somewhere on the floor map (lidarmap.py), by where the robot is on it (localize.py).

Planning (Planner.plan): a shortest route over the map's 5 cm squares (A*) that keeps the robot's middle at least
CLEAR_M from anything the map has as a wall, furniture or never seen, and prefers PREFER_M: the cost of a square
grows as it gets nearer than that, so routes run down the middle of doorways and gaps rather than skimming a wall.
Near the start and the goal it may come closer (the dock is against a wall).

Following (go_to): chases a point LOOKAHEAD_M along the route (pure pursuit), turning on the spot first if that
point is well off to one side, at walking-slowly speed and slower near the goal. The pose comes from the localizer
every tenth of a second; drifting off the route plans again. Anything new in the way (the server's own lidar stop
holds the robot) gets a few seconds to move, then a new route round what the lidar sees; failing that it stops
and says so. It's the AI driver's driving: labelled as the AI, stopped by any human input.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import time

import cv2
import numpy as np

from .driver import WheelProgress
from .lidarmap import RES, Grid, scan_points

log = logging.getLogger("beambrain.navigate")

CLEAR_M = 0.30                     # the robot is 0.5 m wide: its middle never nearer a wall than this
PREFER_M = 0.55                    # ...and the route keeps this far off when it can
NEAR_ENDS_M, NEAR_ENDS_CLEAR_M = 0.6, 0.12   # within this of the start or goal it may come this close
SPEED, TURN_SPEED = 0.2, 0.5       # m/s, rad/s: slow, it is a home
LOOKAHEAD_M = 0.45
ARRIVED_M = 0.08
OFF_ROUTE_M = 0.4
TURN_FIRST_DEG = 50                # the route's next point further round than this: turn on the spot first
WAIT_BLOCKED_S = 3.0
CLOSE_ENOUGH_M = 0.25              # held up this near the spot: finish from there (turn, nudge) instead of re-planning
SETTLE_S = 1.5                     # at the spot, facing the saved way: time for a fresh look before the last nudge
FINAL_SIDE_M, FINAL_MAX_M = 0.15, 0.6   # the last nudge only straight forward or back, and only this far
OFF_CHARGER_M = 0.4                # starting on the charger: straight off it this far before turning
LOST_WAIT_S = 15.0


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class Planner:
    def __init__(self, grid: Grid):
        lo = grid.lo
        seen = np.argwhere(np.abs(lo) > 0.3)
        (self.r0, self.c0), (r1, c1) = seen.min(0) - 2, seen.max(0) + 3
        crop = lo[self.r0:r1, self.c0:c1]
        free = (crop < -0.3).astype(np.uint8)                  # walls and never-seen squares are both off limits
        self.clear = cv2.distanceTransform(free, cv2.DIST_L2, 5) * RES
        self.origin = grid.origin + np.array([self.c0, self.r0]) * RES

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return int((y - self.origin[1]) // RES), int((x - self.origin[0]) // RES)

    def point(self, rc) -> tuple[float, float]:
        return float(self.origin[0] + (rc[1] + 0.5) * RES), float(self.origin[1] + (rc[0] + 0.5) * RES)

    def clearance(self, x: float, y: float) -> float:
        r, c = self.cell(x, y)
        if not (0 <= r < self.clear.shape[0] and 0 <= c < self.clear.shape[1]):
            return 0.0
        return float(self.clear[r, c])

    def plan(self, start, goal, obstacles: np.ndarray | None = None) -> list[tuple[float, float]] | str:
        """Route from start (x, y) to goal (x, y), as points about 10 cm apart; or why there isn't one.
        obstacles: extra points (world metres) to keep CLEAR_M from, e.g. what the lidar sees now."""
        clear = self.clear
        if obstacles is not None and len(obstacles):
            mask = np.ones(clear.shape, np.uint8)
            for x, y in obstacles:
                r, c = self.cell(x, y)
                if 0 <= r < mask.shape[0] and 0 <= c < mask.shape[1]:
                    mask[r, c] = 0
            clear = np.minimum(clear, cv2.distanceTransform(mask, cv2.DIST_L2, 5) * RES)
        h, w = clear.shape
        s, g = self.cell(*start), self.cell(*goal)
        for name, (r, c) in (("start", s), ("goal", g)):
            if not (0 <= r < h and 0 <= c < w) or clear[r, c] < NEAR_ENDS_CLEAR_M:
                return f"the {name} isn't on clear floor on the map"
        near = int(NEAR_ENDS_M / RES)
        rr, cc = np.mgrid[0:h, 0:w]
        near_ends = ((rr - s[0]) ** 2 + (cc - s[1]) ** 2 <= near ** 2) | ((rr - g[0]) ** 2 + (cc - g[1]) ** 2 <= near ** 2)
        ok = (clear >= CLEAR_M) | (near_ends & (clear >= NEAR_ENDS_CLEAR_M))
        # Cost per square: 1, up to 4 right at the edge of what's allowed.
        weight = 1 + 3 * np.clip((PREFER_M - clear) / (PREFER_M - NEAR_ENDS_CLEAR_M), 0, 1)
        steps = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
                 (-1, -1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (1, 1, 1.414)]
        cost = {s: 0.0}
        came = {}
        todo = [(0.0, s)]
        while todo:
            _, cur = heapq.heappop(todo)
            if cur == g:
                break
            base = cost[cur]
            for dr, dc, length in steps:
                nxt = (cur[0] + dr, cur[1] + dc)
                if not (0 <= nxt[0] < h and 0 <= nxt[1] < w) or not ok[nxt]:
                    continue
                c = base + length * weight[nxt]
                if c < cost.get(nxt, math.inf):
                    cost[nxt], came[nxt] = c, cur
                    heapq.heappush(todo, (c + math.hypot(g[0] - nxt[0], g[1] - nxt[1]), nxt))
        else:
            return "no way through on the map"
        cells = [g]
        while cells[-1] != s:
            cells.append(came[cells[-1]])
        pts = np.array([self.point(rc) for rc in reversed(cells)])
        # Smoothed (the squares' zigzag), then every other point; the exact start and goal at the ends.
        if len(pts) > 7:
            k = np.ones(7) / 7
            pts[3:-3] = np.c_[np.convolve(pts[:, 0], k, "valid"), np.convolve(pts[:, 1], k, "valid")]
        route = [tuple(p) for p in pts[::2]]
        return [tuple(start)] + route[1:] + [tuple(goal)]


def route_length(route) -> float:
    return float(sum(math.dist(a, b) for a, b in zip(route, route[1:])))


async def go_to(brain, d, planner: Planner, goal: tuple[float, float, float | None], what: str = "there") -> str | None:
    """Drive the AIDriver `d` to goal (x, y, heading or None) on the map. None when there, else why not."""
    loc = brain.localizer
    route, i = None, 0
    blocked_since, retries = None, 0
    t0, limit = time.monotonic(), 120.0
    lost_since = None
    b = d._board()
    progress = WheelProgress(b, time.monotonic(), turns=True)
    if b.get("charging") or b.get("charger_detected"):
        # Straight off the contacts first: turning on the spot there scrapes them and swings its back round.
        msg, blocked = await d.drive(OFF_CHARGER_M)
        if blocked:
            return f"couldn't drive off the charger ({msg})"
    try:
        while True:
            d._check()
            if loc.state == "searching" or loc.pose is None:
                await d._cmd(0.0, 0.0)
                lost_since = lost_since or time.monotonic()
                if time.monotonic() - lost_since > LOST_WAIT_S:
                    return "lost track of where it is on the map"
                await asyncio.sleep(0.1)
                continue
            lost_since = None
            x, y, th = loc.pose
            if route is None:
                obstacles = None
                if retries:
                    pts = scan_points(((brain.telemetry or {}).get("scan") or {}).get("cm") or [])
                    c, s = math.cos(th), math.sin(th)
                    obstacles = np.c_[x + c * pts[:, 0] - s * pts[:, 1], y + s * pts[:, 0] + c * pts[:, 1]] if len(pts) else None
                route = await asyncio.to_thread(planner.plan, (x, y), goal[:2], obstacles)
                if isinstance(route, str):
                    return route
                i = 0
                limit = time.monotonic() - t0 + 30 + 3 * route_length(route) / SPEED
                log.info("navigate: a route to %s, %.1f m", what, route_length(route))
            if time.monotonic() - t0 > limit:
                return "took too long getting there"
            to_goal = math.hypot(goal[0] - x, goal[1] - y)
            if to_goal < ARRIVED_M:
                break
            # The nearest route point ahead (not behind: a route can pass near itself).
            window = route[i:i + 30]
            near = min(range(len(window)), key=lambda k: math.hypot(window[k][0] - x, window[k][1] - y))
            i += near
            if math.hypot(route[i][0] - x, route[i][1] - y) > OFF_ROUTE_M:
                log.info("navigate: off the route; planning again")
                route = None
                continue
            j = i
            while j < len(route) - 1 and math.hypot(route[j][0] - x, route[j][1] - y) < LOOKAHEAD_M:
                j += 1
            lx, ly = route[j]
            alpha = _wrap(math.atan2(ly - y, lx - x) - th)
            b = d._board()
            if abs(alpha) > math.radians(TURN_FIRST_DEG):
                v, w = 0.0, math.copysign(TURN_SPEED, alpha)
            else:
                v = SPEED * max(0.3, math.cos(alpha))
                v = min(v, 0.5 * to_goal + 0.05)
                L = max(math.hypot(lx - x, ly - y), 0.1)
                w = max(-0.8, min(0.8, 2 * v * math.sin(alpha) / L))
            # Something new in the way: the server's lidar stop holds the robot still while it's told to go.
            stopped = progress.stopped(b, time.monotonic()) or (
                abs(b.get("actual_linear", 0.0)) < 0.02 and abs(b.get("actual_angular", 0.0)) < 0.05)
            if v > 0 and stopped:
                blocked_since = blocked_since or time.monotonic()
                if time.monotonic() - blocked_since > WAIT_BLOCKED_S:
                    await d._settle()
                    if to_goal < CLOSE_ENOUGH_M:
                        # A spot by a counter or a wall: the obstacle stop holds it a few cm short. Re-planning
                        # can't help there; turn to the saved way here, and the final nudge does the rest.
                        log.info("navigate: held %.0f cm short of %s by something in front; finishing from here",
                                 to_goal * 100, what)
                        break
                    retries += 1
                    if retries > 2:
                        return "something is in the way"
                    log.info("navigate: something in the way; planning round it")
                    route, blocked_since = None, None
                    continue
            else:
                blocked_since = None
            await d._cmd(v, w)
            await asyncio.sleep(0.1)
    finally:
        await d._settle()
    if goal[2] is not None:
        await d.turn(math.degrees(_wrap(goal[2] - loc.pose[2])))
        # Facing the saved way, look again (a fresh match standing still) and drive straight forward or back onto
        # the spot: turning on the spot, and the last metre's tracking, can leave it some way off along its heading.
        for _ in range(2):
            await asyncio.sleep(SETTLE_S)
            along, side = _offset(loc.pose, goal)
            if abs(along) < ARRIVED_M or abs(side) > FINAL_SIDE_M or abs(along) > FINAL_MAX_M:
                break
            x, y, th = loc.pose
            steps = np.linspace(0, along, 8)
            if min(planner.clearance(x + s * math.cos(th), y + s * math.sin(th)) for s in steps) < NEAR_ENDS_CLEAR_M:
                break                                            # the map has something in the way: leave it
            await d.drive(along)
        along, side = _offset(loc.pose, goal)
        log.info("navigate: at %s, %.0f cm ahead of the spot, %.0f cm to its left", what, -along * 100, -side * 100)
    return None


def _offset(pose, goal) -> tuple[float, float]:
    """Where the goal is from the robot: metres ahead of it, and to its left."""
    x, y, th = pose
    dx, dy = goal[0] - x, goal[1] - y
    return dx * math.cos(th) + dy * math.sin(th), -dx * math.sin(th) + dy * math.cos(th)
