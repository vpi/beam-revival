"""Find the charging dock from its printed marker sheet (docs/dock-marker-letter.pdf) in the head camera.

The sheet stands upright behind the dock: one 160 mm marker (id 20) for the head camera, from across the
room, and two 60 mm ones (21, 22) above it for the phone's rear camera while backing in. Seen by the
robot's measured head camera (beambrain/calibrate.py), the markers' corners give where the sheet is and
which way it faces: everything the robot needs to line up in front of the dock.

  python3 -m beambrain.dock [snapshot-head.jpg ...]

prints what it sees in each image (default: the newest snapshot in ~/beam-brain/calibration/, saved with
`kill -USR1` on the brain).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .calibrate import head_project, minimise
from .geometry import CameraModel

log = logging.getLogger("beambrain.dock")

# The sheet (Letter, mm from its top-left): marker id -> (centre x, centre y, half the side)
SHEET_MID_MM = 107.95
SHEET_MARKERS = {20: (107.95, 172.0, 80.0), 21: (62.95, 48.0, 30.0), 22: (152.95, 48.0, 30.0)}
SHEET_TOP_M = 0.612             # as mounted: the big marker's bottom edge 36 cm above the floor
CORNERS = [(-1, -1), (1, -1), (1, 1), (-1, 1)]     # ArUco order: top-left, top-right, bottom-right, bottom-left


def find_markers(image: np.ndarray) -> dict[int, np.ndarray]:
    """The dock sheet's markers in an image: id -> 4x2 corner pixels."""
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    found, ids, _ = det.detectMarkers(image)
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    out = {}
    for c, i in zip(found, [] if ids is None else ids.flatten()):
        if int(i) in SHEET_MARKERS:
            cv2.cornerSubPix(grey, c, (3, 3), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
            out[int(i)] = c[0].astype(float)
    return out


def sheet_points(ids) -> np.ndarray:
    """The markers' corners on the sheet: (metres right of its middle, metres above the floor)."""
    pts = []
    for i in ids:
        cx, cy, half = SHEET_MARKERS[i]
        for sx, sy in CORNERS:
            pts.append(((cx + sx * half - SHEET_MID_MM) / 1000, SHEET_TOP_M - (cy + sy * half) / 1000))
    return np.array(pts)


def place_sheet(pose, pts: np.ndarray) -> np.ndarray:
    """Sheet points in the robot's frame (ahead of the base's front edge, left, up) for a sheet whose middle
    is at (x, y) and that is turned by phi (0 = facing the robot square on, positive = turned anticlockwise
    seen from above)."""
    x, y, phi = pose
    right = np.array([math.sin(phi), -math.cos(phi)])           # the sheet's right, as the robot sees it
    return np.c_[x + pts[:, 0] * right[0], y + pts[:, 0] * right[1], pts[:, 1]]


@dataclass
class Sighting:
    ahead: float         # metres from the robot's centre (wheel axle) to the sheet's middle, ahead
    left: float          # ... and to the left
    facing_deg: float    # the sheet's turn: 0 = square on to the robot
    markers: list[int]
    err_px: float        # median reprojection error
    seen_at_deg: float = 0.0     # which way the big marker's middle is (degrees left), straight from its pixel:
                                 # right even when the fit isn't (at the picture's edge)

    @property
    def distance(self) -> float:
        return math.hypot(self.ahead, self.left)

    @property
    def bearing_deg(self) -> float:
        """Degrees to turn left to face the sheet."""
        return math.degrees(math.atan2(self.left, self.ahead))

    def __str__(self) -> str:
        return (f"dock sheet {self.distance:.2f} m away, {abs(self.bearing_deg):.1f} deg "
                f"{'left' if self.bearing_deg >= 0 else 'right'} ({self.ahead:+.2f} ahead, {self.left:+.2f} left), "
                f"turned {self.facing_deg:+.1f} deg; markers {self.markers}, fit {self.err_px:.1f} px")


def sight_dock(image: np.ndarray, cam: CameraModel, use=(20,)) -> Sighting | None:
    """Where the dock sheet is, from the measured head camera, or None if its big marker isn't in view.
    By default from the big marker alone: across the room the small ones are only a dozen pixels wide, and
    their rough corners swung the sheet's angle by 10 degrees from one frame to the next."""
    if cam.head is None:
        raise ValueError("needs the robot's measured head camera (python3 -m beambrain.calibrate)")
    found = find_markers(image)
    if 20 not in found:
        return None
    hd = cam.head
    p = [hd["cam_x_from_front"], hd["yaw"], hd["pitch_down"], hd["roll"], hd["f"], hd["k1"]]
    h, w = image.shape[:2]
    sx, sy = w / hd["w"], h / hd["h"]                 # in case the frame isn't the size it was measured at
    ids = sorted(i for i in found if i in use)
    uv = np.concatenate([found[i] for i in ids]) / [sx, sy]
    pts = sheet_points(ids)

    # Start from the big marker's centre: its ray, out to the height we know it's at.
    u, v = found[20].mean(0) / [sx, sy]
    ra, rl, ru = cam._head_ray(u, v)
    t = (SHEET_TOP_M - 0.172 - hd["height"]) / ru if ru < -1e-3 else 2.0
    x0 = [hd["cam_x_from_front"] + t * ra, t * rl, 0.0]

    cost = lambda q: float(((head_project(p, place_sheet(q, pts), hd["height"], hd["w"], hd["h"]) - uv) ** 2).sum())
    q = minimise(cost, x0, [0.05, 0.05, 0.1])
    err = np.linalg.norm(head_project(p, place_sheet(q, pts), hd["height"], hd["w"], hd["h"]) - uv, axis=1)
    return Sighting(float(q[0]) + cam.axle_to_front_m, float(q[1]), math.degrees(q[2]), ids, float(np.median(err)),
                    cam.head_angle_left(u / hd["w"]))


# --- backing onto the charger ------------------------------------------------------------------------------

AXLE_TO_SHEET_DOCKED_M = 0.355     # contacts 0.40 m behind the front edge (0.305 behind the axle) + the 5 cm plate
EDGE_DEG, BAD_FIT_PX = 25.0, 5.0   # head sightings this far to the side, or fitting this badly, aren't trusted
APPROACH_M = 1.45                  # axle to sheet where it turns round: the rear camera reads all three markers
MAP_VIEW_M, MAP_VIEW_DEG = (0.9, 2.2), 40.0   # out of sight, the map takes it somewhere this far from the sheet
MAP_ROOMY_M = 0.45                 # ...and this far round in front of it, the roomier the better up to this
LINEUP_OFF_M = 0.15               # turned round this far off the centre line or less, backing straight in fixes it
LINEUP_BLOCKED_M = 0.25           # ...and this far, when there's no room in front to line up first
BACK_SPEED, BACK_SLOW = 0.08, 0.03
SLOW_FOR_M = 0.45                  # gently for the last 40 cm or so (it touches the contacts about 5 cm early)
LINE_GAIN = 100.0                  # backing: degrees to angle in per metre off the centre line ...
MAX_ANGLE_IN = 20.0                # ... at most (the markers must stay in the rear picture)
SQUARE_FOR_M = 0.15                # straight and square for the last bit onto the contacts
TURN_GAIN = 1.5                    # turn rate (1/s) per radian of heading error
MAX_TURN = 0.2                     # rad/s
AIM_FIRST_DEG = 6.0                # this far from the angle backing wants: turn there standing still first
TOO_FAR_OFF_M = 0.08              # this close to the contacts and still this far off the line: stop, don't scrape


def _wrap(deg: float) -> float:
    return (deg + 180) % 360 - 180


def _mean_deg(angles) -> float:
    r = np.radians(angles)
    return math.degrees(math.atan2(np.sin(r).mean(), np.cos(r).mean()))


def decode(jpeg_b64: str) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(base64.b64decode(jpeg_b64), np.uint8), cv2.IMREAD_COLOR)


@dataclass
class RearView:
    """Where the phone's rear camera is, relative to the sheet (from its markers): which way it looks (degrees,
    0 = square at the sheet), how far right of the sheet's middle it is and how far out from the sheet (metres),
    "right" as you face the sheet."""
    heading: float
    right: float
    out: float


def rear_view(found: dict[int, np.ndarray], f: float, size: tuple[int, int]) -> RearView | None:
    """The rear camera's pose from the sheet's markers it sees, for a camera with focal length `f` pixels (measured
    when taught) and the picture's middle as its centre. Needs two markers: one alone can look turned either way."""
    ids = sorted(i for i in found if i in SHEET_MARKERS)
    if len(ids) < 2:
        return None
    obj = np.c_[sheet_points(ids), np.zeros(4 * len(ids))]
    img = np.concatenate([found[i] for i in ids]).astype(float)
    K = np.array([[f, 0, size[0] / 2], [0, f, size[1] / 2], [0, 0, 1]], float)
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    R, _ = cv2.Rodrigues(rvec)
    C = -R.T @ tvec.ravel()                      # the camera, in the sheet's frame (x right, y up, z out of it)
    a = R.T @ np.array([0.0, 0.0, 1.0])          # the way it looks
    if C[2] <= 0:
        return None
    return RearView(math.degrees(math.atan2(a[0], -a[2])), float(C[0]), float(C[2]))


def robot_pose(view: RearView, docked: RearView) -> tuple[float, float, float]:
    """The robot's (degrees turned from square, axle metres right of the dock's centre line, axle metres out from
    the sheet), from where its rear camera is now and where it was when docked (taught: square, centred, axle
    AXLE_TO_SHEET_DOCKED_M out). The phone can sit anywhere on the robot, looking any way."""
    turned = view.heading - docked.heading
    ox, oz = docked.right, docked.out - AXLE_TO_SHEET_DOCKED_M          # the camera from the axle, docked
    t = math.radians(turned)
    ox, oz = math.cos(t) * ox - math.sin(t) * oz, math.sin(t) * ox + math.cos(t) * oz
    return turned, view.right - ox, view.out - oz


def _median_view(views: list[RearView]) -> RearView:
    return RearView(*(float(np.median([getattr(v, k) for v in views])) for k in ("heading", "right", "out")))


class Docking:
    """Backs the robot onto its charger: line up in front of the dock with the head camera, turn round, then
    reverse slowly, steering by the markers in the phone's rear camera, until the board reports the charger.

    Runs on an AIDriver's moves (closed-loop, labelled as the AI, stopped by any human input). Backing, every rear
    frame gives the camera's full pose against the sheet (rear_view), so the robot knows how turned and how far off
    the centre line it is however the head camera lined it up. Where the camera is when docked, and its focal
    length, are taught once (teach())."""

    def __init__(self, brain, driver, ref_path: Path):
        self.brain, self.d, self.ref_path = brain, driver, Path(ref_path)

    def _taught(self) -> tuple[float, RearView] | None:
        try:
            t = json.loads(self.ref_path.read_text())
            return float(t["rear_f"]), RearView(*t["docked_view"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def _frame(self, cam: str, after: float, wait_s: float = 3.0) -> np.ndarray | None:
        """The newest frame from this camera received after `after` (monotonic), decoded, or None."""
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            f = self.brain.frames.get(cam)
            if f and f[0] and f[1] > after:
                return decode(f[0])
            await asyncio.sleep(0.05)
        return None

    async def _sight(self) -> Sighting | None:
        """Five head-camera sightings after the robot stopped, combined (median)."""
        seen, t = [], time.monotonic() + 0.2
        for _ in range(5):
            im = await self._frame("head", t)
            t = time.monotonic()
            s = None if im is None else sight_dock(im, self.d.cam)
            if s:
                seen.append(s)
        if not seen:
            return None
        med = lambda k: float(np.median([getattr(s, k) for s in seen]))
        return Sighting(med("ahead"), med("left"), med("facing_deg"), seen[0].markers, med("err_px"), med("seen_at_deg"))

    async def _rear_markers(self, n: int = 5) -> tuple[list[dict], tuple[int, int]] | None:
        """The markers in `n` fresh rear frames (only frames with at least two), and the picture's size."""
        out, size, t = [], None, time.monotonic() + 0.2
        for _ in range(n):
            im = await self._frame("rear", t, wait_s=5.0)
            t = time.monotonic()
            if im is None:
                continue
            found = find_markers(im)
            if len(found) >= 2:
                out.append(found)
                size = (im.shape[1], im.shape[0])
        return (out, size) if len(out) * 2 > n else None

    async def _focal_guess(self) -> float | str:
        """The rear camera's focal length, roughly: how far the sheet moves in the picture when the robot turns
        4 degrees (turns back afterwards)."""
        d = self.d
        before = await self._rear_markers(3)
        y0 = d._board().get("yaw_deg", 0.0)
        await d.turn(4)
        after = await self._rear_markers(3)
        turned = _wrap(d._board().get("yaw_deg", 0.0) - y0)
        await d.turn(-turned)
        if before is None or after is None or abs(turned) < 1:
            return "lost the dock markers while checking the rear camera"
        mid = lambda ms: np.median([np.concatenate(list(m.values())).mean(0) for m in ms], axis=0)
        px = float(np.linalg.norm(mid(after[0]) - mid(before[0])))
        if px < 4:
            return "the rear camera doesn't seem to turn with the robot"
        return px / math.radians(abs(turned))

    def _charging(self) -> bool:
        b = self.d._board()
        return bool(b.get("charging") or b.get("charger_detected"))

    async def teach(self, out_m: float = 0.5) -> str:
        """Robot on the charger: look at the sheet with the rear camera, drive straight off `out_m`, check how far
        the picture moves for a small turn, look again, then back onto the charger by what it learnt (a first test
        of the last part of docking). The known `out_m` corrects the focal length: the two views must be that far
        apart."""
        d = self.d
        if not self._charging():
            return "put the robot on the charger first"
        await self.brain.send({"type": "rear_feed", "on": True})
        near = await self._rear_markers()
        if near is None:
            return "can't see two of the dock markers in the rear camera"
        msg, blocked = await d.drive(out_m)
        if blocked:
            return f"couldn't drive off the dock: {msg}"
        for _ in range(30):                          # the board takes a moment to notice it left the charger
            if not self._charging():
                break
            await asyncio.sleep(0.1)
        else:
            return "drove off but the board still reports the charger"
        f = await self._focal_guess()
        if isinstance(f, str):
            return f
        far = await self._rear_markers()
        if far is None:
            return "lost the dock markers driving off"
        size = near[1]
        for _ in range(3):                           # distances scale with the focal length: make them 0.5 m apart
            vn = _median_view([v for v in (rear_view(m, f, size) for m in near[0]) if v])
            vf = _median_view([v for v in (rear_view(m, f, size) for m in far[0]) if v])
            f *= out_m / max(vf.out - vn.out, 0.05)
        docked = _median_view([v for v in (rear_view(m, f, size) for m in near[0]) if v])
        self.ref_path.write_text(json.dumps({"rear_f": round(f, 1), "docked_view": [
            round(docked.heading, 2), round(docked.right, 4), round(docked.out, 4)]}) + "\n")
        log.info("docking taught: rear camera focal length %.0f px; docked it looks %+.1f deg, %.3f m right, %.3f m out",
                 f, docked.heading, docked.right, docked.out)
        result = await self._back_in(f, docked)
        return f"taught the docked rear view; backing on again: {result}"

    async def run(self) -> str:
        d = self.d
        if self._charging():
            return "already on the charger"
        taught = self._taught()
        if taught is None:
            return "not taught yet: teach the docked rear view first, with the robot on the charger"

        f, docked = taught
        # 1. Find the dock with the head camera and head straight for it (the way to a dock is open), to about
        # APPROACH_M out. Only the head camera's distance and direction are used: its reading of which way the sheet
        # faces has been 10-30 degrees out.
        by_map = False
        for attempt in range(5):
            s = await self._sight()
            if s is None and not by_map:
                # Out of sight (another room, facing away): drive to in front of the dock by the floor map first.
                by_map = True
                why = await self._come_by_map()
                if why:
                    return why
                continue
            if s is None:
                return "can't see the dock marker with the head camera"
            # Near the picture's edge the camera model is least sure, and a sighting there can be far out (54
            # degrees off, a 42 px fit, once): face the marker first, and look again from straight on.
            if abs(s.seen_at_deg) > EDGE_DEG or s.err_px > BAD_FIT_PX:
                log.info("docking: %s; turning to face it first", s)
                await d.turn(s.seen_at_deg)
                continue
            log.info("docking: %s", s)
            if abs(s.bearing_deg) > 1.5:
                await d.turn(s.bearing_deg)
            go = s.distance - APPROACH_M
            if go < 0.1:
                break
            msg, blocked = await d.drive(go)
            if blocked:
                return "something is in the way between the robot and the dock"
        else:
            return "can't get a clear look at the dock marker with the head camera"

        # 2. Turn round: from here the rear camera measures exactly where the robot is. Too far off the dock's
        # centre line to back in, it drives forward onto the line and turns round again.
        await self.brain.send({"type": "rear_feed", "on": True})
        await d.turn(180)
        for fix in range(3):
            pose = await self._rear_pose(f, docked)
            if pose is None:
                return "can't see two of the dock markers in the rear camera"
            turned, right, out = pose
            log.info("docking: turned round, %+.1f deg from square, %+.1f cm off the line, %.2f m out",
                     turned, right * 100, out)
            if abs(right) <= LINEUP_OFF_M:
                break
            if fix == 2:
                return f"couldn't get onto the dock's centre line (still {right * 100:+.0f} cm off)"
            # A spot on the line a little further out, so it drives mostly forward. In the sheet's frame the robot
            # faces (-sin t, cos t) when `turned` is t, and turning left lowers `turned`.
            vx, vz = -right, min(out + 0.15, APPROACH_M + 0.3) - out
            theta = math.degrees(math.atan2(-vx, vz))
            await d.turn(turned - theta)
            msg, blocked = await d.drive(math.hypot(vx, vz))
            if blocked:
                # Little room in front (it faces away from the dock here). Not far off: back in from here, angling
                # onto the line; the TOO_FAR_OFF_M check still stops it short of the contacts if that fails.
                await d.turn(theta)                  # back to the dock first: turned off, it can't see the markers
                pose = await self._rear_pose(f, docked)
                if pose is None:
                    return "can't see two of the dock markers in the rear camera"
                turned, right, out = pose
                if abs(right) > LINEUP_BLOCKED_M:
                    return "something is in the way to the dock's centre line"
                log.info("docking: no room to line up first; backing in from %+.1f cm off the line", right * 100)
                break
            await d.turn(theta)                      # back to the dock, square

        # 3. Aim in place before backing: the backing loop's first move is to swing to the angle that brings it onto
        # the line, and a big swing while reversing loses the markers from the rear picture (12-14 degrees off
        # square after turning round, from a map spot 20 degrees off the dock's line, lost them twice in a row).
        want = -max(-MAX_ANGLE_IN, min(MAX_ANGLE_IN, LINE_GAIN * right))
        if abs(turned - want) > AIM_FIRST_DEG:
            log.info("docking: aiming %+.1f deg in place before backing", turned - want)
            await d.turn(turned - want)
        return await self._back_in(f, docked)

    async def _come_by_map(self) -> str | None:
        """Drive by the floor map to where the head camera can see the dock, facing it; None when there, else why
        not. Not necessarily straight out in front (ours has a door 1.5 m out): docking copes with side-on starts,
        so the roomiest spot within MAP_VIEW_DEG of straight on, nearer straight on breaking ties."""
        from .navigate import CLEAR_M, Planner, go_to
        loc = getattr(self.brain, "localizer", None)
        if loc is None or loc.dock is None:
            return "can't see the dock marker with the head camera, and there's no map with the dock on it"
        if loc.state == "searching":
            return "can't see the dock marker with the head camera, and doesn't know where it is on the map"
        planner = await asyncio.to_thread(Planner, loc.grid)
        x, y, th = loc.dock
        sx, sy = x - AXLE_TO_SHEET_DOCKED_M * math.cos(th), y - AXLE_TO_SHEET_DOCKED_M * math.sin(th)
        best, spot = None, None
        for r in np.arange(MAP_VIEW_M[0], MAP_VIEW_M[1] + 1e-9, 0.05):
            for a in np.radians(np.arange(-MAP_VIEW_DEG, MAP_VIEW_DEG + 1e-9, 2.0)):
                gx, gy = sx + r * math.cos(th + a), sy + r * math.sin(th + a)
                room = planner.clearance(gx, gy)
                if room < CLEAR_M:
                    continue
                score = min(room, MAP_ROOMY_M) - 0.1 * abs(a) / math.radians(MAP_VIEW_DEG) - 0.05 * abs(r - APPROACH_M)
                if best is None or score > best:
                    best, spot = score, (gx, gy, math.atan2(sy - gy, sx - gx))
        if spot is None:
            return "no clear spot on the map in front of the dock"
        log.info("docking: the dock is out of sight; driving to it by the map")
        await self.d._label(True, action="docking: driving to the dock by the map")
        why = await go_to(self.brain, self.d, planner, spot, "the dock")
        if not why and loc.pose is not None:              # where the map says the sheet is, to compare with the camera
            px, py, pth = loc.pose
            dx, dy = sx - px, sy - py
            log.info("docking: by the map the sheet is %.2f m away, %+.1f deg left (fit %.2f, %s)", math.hypot(dx, dy),
                     _wrap(math.degrees(math.atan2(dy, dx) - pth)), loc.fit, loc.state)
        await self.d._label(True, action="docking: looking for the dock")
        return f"couldn't drive to the dock by the map: {why}" if why else None

    async def _rear_pose(self, f: float, docked: RearView, n: int = 5) -> tuple[float, float, float] | None:
        """The robot's (degrees from square, metres off the centre line, metres out), from `n` rear frames (median)."""
        got = await self._rear_markers(n)
        if got is None:
            return None
        markers, size = got
        poses = [robot_pose(v, docked) for v in (rear_view(m, f, size) for m in markers) if v]
        if len(poses) * 2 <= n:
            return None
        return tuple(float(x) for x in np.median(poses, axis=0))

    async def _back_in(self, f: float, docked: RearView) -> str:
        """Reverse onto the charger by the rear camera: each frame says how turned and how far off the centre line
        the robot is (smoothed, and carried between frames by the gyro and wheels). It angles back onto the line,
        at most MAX_ANGLE_IN, and comes in square for the last SQUARE_FOR_M."""
        d = self.d
        t0 = last_seen = time.monotonic()
        last_frame = 0.0
        squares, rights = [], []                     # recent (turned + gyro yaw), and axle offsets
        out_at = None                                # (axle metres out, odometry) at the last view
        next_log = None
        while True:
            d._check()
            if self._charging():
                await d._settle()
                return "docked: on the charger"
            b = d._board()
            yaw, odo = b.get("yaw_deg", 0.0), b.get("odometry_m", 0.0)
            fr = self.brain.frames.get("rear")
            if fr and fr[0] and fr[1] > last_frame and time.monotonic() - fr[1] < 0.6:
                last_frame = fr[1]
                im = decode(fr[0])
                v = None if im is None else rear_view(find_markers(im), f, (im.shape[1], im.shape[0]))
                if v is not None:
                    last_seen = time.monotonic()
                    turned, right, out = robot_pose(v, docked)
                    # Turning left turns the rear camera's view the other way: turned + yaw stays put.
                    squares, rights = (squares + [turned + yaw])[-5:], (rights + [right])[-3:]
                    out_at = (out, odo)
            if out_at is None:
                if time.monotonic() - t0 > 3:
                    await d._settle()
                    return "can't see two of the dock markers in the rear camera"
                await d._cmd(0.0, 0.0)
                await asyncio.sleep(0.1)
                continue
            turned = _wrap(_mean_deg(squares) - yaw)
            right = float(np.mean(rights))
            remaining = out_at[0] - (out_at[1] - odo) - AXLE_TO_SHEET_DOCKED_M
            if next_log is None or remaining <= next_log:          # numbers only, to see how it went
                log.info("docking: %.2f m to go, turned %+.1f deg, %+.1f cm off the line", remaining, turned, right * 100)
                next_log = remaining - 0.15
            if remaining < -0.08:
                await d._settle()
                return "backed past where the contacts should be, but no charger: missed them"
            if remaining < 0.25 and abs(right) > TOO_FAR_OFF_M:
                await d._settle()          # it would scrape the dock's side: better to stop and say so
                return f"too far off the dock's centre line to back on ({right * 100:+.0f} cm)"
            if remaining > 0.2 and time.monotonic() - last_seen > 1.5:
                await d._settle()          # close in, the markers may leave the picture: carry on, on the gyro
                return f"lost sight of the dock markers {remaining:.2f} m out"
            if time.monotonic() - t0 > 20 + 2 / BACK_SLOW:
                await d._settle()
                return "backing in took too long"
            want = 0.0 if remaining < SQUARE_FOR_M else -max(-MAX_ANGLE_IN, min(MAX_ANGLE_IN, LINE_GAIN * right))
            turn_rate = max(-MAX_TURN, min(MAX_TURN, TURN_GAIN * math.radians(turned - want)))
            await d._cmd(-(BACK_SLOW if remaining < SLOW_FOR_M else BACK_SPEED), turn_rate)
            await asyncio.sleep(0.1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="*")
    ap.add_argument("--cameras", default=os.path.expanduser("~/beam-brain/data/cameras.json"))
    args = ap.parse_args()
    cam = CameraModel.load(args.cameras)
    images = args.images or sorted(Path(os.path.expanduser("~/beam-brain/calibration")).glob("*-head.jpg"))[-1:]
    if not images:
        raise SystemExit("no snapshot: `kill -USR1` the brain first")
    for path in images:
        im = cv2.imread(str(path))
        s = None if im is None else sight_dock(im, cam)
        print(f"{Path(path).name}: {s or 'no dock marker in view'}")


if __name__ == "__main__":
    main()
