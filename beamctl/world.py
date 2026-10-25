"""A small virtual house for testing autonomy: floor plan, collisions, and a first-person camera.

Rendering is a vectorised raycaster (numpy): walls are coloured per room, and furniture is drawn as
labelled billboards, so a vision model can tell a couch from a fridge without photorealism. Coordinates
are metres, x to the right and y up on the map, heading in radians counter-clockwise from +x
(so a positive turn is a LEFT turn, matching the robot).

Only needs numpy; OpenCV (cv2) is used if present to label the furniture and encode JPEGs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:
    import cv2
except ImportError:          # the motor server doesn't need the renderer
    cv2 = None

CELL = 0.5                   # metres per map cell
WALL_H = 2.4                 # wall height (m)
CAM_H = 1.25                 # Beam+ head camera height (m)
FOV = math.radians(68)       # horizontal field of view of the head camera
HEAD_PITCH = math.radians(8) # head camera tilted slightly down (rendered by shifting the horizon)
# The floor ("nav") camera looks down at the floor just in front of the robot: this patch of floor,
# in metres ahead of the robot's centre and to its sides.
NAV_NEAR, NAV_FAR, NAV_HALF_WIDTH = 0.25, 1.75, 0.9
ROBOT_R = 0.22               # robot footprint radius (m)

# '#' wall, letters are floor cells belonging to a room, '.' is a doorway (floor, no room).
PLAN = """
#######################
#OOOOOOOO#LLLLLLLLLLLL#
#OOOOOOOO#LLLLLLLLLLLL#
#OOOOOOOO.LLLLLLLLLLLL#
#OOOOOOOO.LLLLLLLLLLLL#
#OOOOOOOO#LLLLLLLLLLLL#
####..####LLLLLLLLLLLL#
#BBBBBBBB####..########
#BBBBBBBB#KKKKKKKKKKKK#
#BBBBBBBB.KKKKKKKKKKKK#
#BBBBBBBB.KKKKKKKKKKKK#
#BBBBBBBB#KKKKKKKKKKKK#
#######################
"""

ROOMS = {"O": "office", "L": "living room", "K": "kitchen", "B": "bedroom"}
WALL_COLORS = {  # BGR, per room the wall faces into
    "O": (170, 190, 205), "L": (150, 200, 225), "K": (140, 215, 215), "B": (200, 170, 190), ".": (185, 185, 185),
}
FLOOR = (95, 120, 150)
CEILING = (225, 228, 230)
FLOOR_WALL = (52, 52, 56)    # how walls look from the floor camera: plain and dark


def floor_colors(codes: np.ndarray, wx: np.ndarray, wy: np.ndarray) -> np.ndarray:
    """Floor material per room, like a real home: tiles in the kitchen, carpet in the bedroom,
    wooden planks elsewhere. Returns float BGR (..., 3)."""
    out = np.empty(codes.shape + (3,), float)
    wood = np.array([70, 105, 150], float)
    plank = (np.floor(wx / 0.18) % 2 == 0)
    out[:] = wood
    out[plank] = wood * 0.9
    kitchen = codes == "K"
    tile = (np.floor(wx / 0.3) + np.floor(wy / 0.3)) % 2 == 0
    out[kitchen] = (205, 205, 210)
    out[kitchen & tile] = (170, 172, 178)
    out[codes == "B"] = (125, 105, 135)          # carpet
    return out


@dataclass
class Thing:
    name: str                 # shown on the billboard, e.g. "COUCH"
    x: float
    y: float
    w: float                  # width (m)
    h: float                  # height (m)
    color: tuple              # BGR
    shape: str = "box"        # box | ball
    z: float = 0.0            # height of its bottom above the floor
    solid: bool = True        # blocks the robot
    sprite: np.ndarray | None = field(default=None, repr=False)


# Rooms (x, y in metres): office 0.5-4.5 x 3.5-6.0, living room 5.0-11.0 x 3.0-6.0,
# bedroom 0.5-4.5 x 0.5-3.0, kitchen 5.0-11.0 x 0.5-2.5. Doorways: office-living x 4.5-5.0 y 4.0-5.0,
# office-bedroom x 2.0-3.0 y 3.0-3.5, living-kitchen x 6.5-7.5 y 2.5-3.0, bedroom-kitchen x 4.5-5.0 y 1.0-2.0.
def default_things() -> list[Thing]:
    return [
        Thing("RED BALL", 9.8, 5.3, 0.35, 0.35, (40, 40, 220), "ball"),     # living room, far corner
        Thing("COUCH", 9.6, 3.45, 1.8, 0.8, (150, 80, 40)),                 # living room
        Thing("TV", 10.85, 4.4, 0.2, 0.9, (40, 40, 40), z=0.6),             # living room, east wall
        Thing("FRIDGE", 10.6, 1.0, 0.7, 1.8, (235, 235, 235)),              # kitchen
        Thing("STOVE", 9.2, 0.85, 0.7, 0.9, (90, 90, 90)),                  # kitchen
        Thing("PLANT", 5.6, 2.2, 0.5, 1.1, (50, 150, 60)),                  # kitchen
        Thing("BED", 1.4, 1.2, 1.6, 0.6, (170, 120, 160)),                  # bedroom
        Thing("DESK", 1.0, 5.6, 1.4, 0.75, (60, 100, 140)),                 # office
    ]


class World:
    def __init__(self, plan: str = PLAN, things: list[Thing] | None = None):
        rows = [r for r in plan.strip("\n").split("\n")]
        # Row 0 of the text is the top of the map (largest y).
        self.grid = np.array([list(r) for r in rows[::-1]])
        self.rows, self.cols = self.grid.shape
        self.width, self.height = self.cols * CELL, self.rows * CELL
        self.wall = self.grid == "#"
        self.things = things if things is not None else default_things()
        for t in self.things:
            t.sprite = _make_sprite(t)

    # --- geometry ------------------------------------------------------------

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return int(math.floor(y / CELL)), int(math.floor(x / CELL))

    def room_at(self, x: float, y: float) -> str | None:
        r, c = self.cell(x, y)
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            return None
        return ROOMS.get(self.grid[r, c])

    def collides(self, x: float, y: float, radius: float = ROBOT_R) -> bool:
        """True if a robot of this radius at (x, y) would overlap a wall or a solid object."""
        r0, c0 = self.cell(x - radius, y - radius)
        r1, c1 = self.cell(x + radius, y + radius)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if not (0 <= r < self.rows and 0 <= c < self.cols) or self.wall[r, c]:
                    # nearest point of the cell to the circle centre
                    nx = min(max(x, c * CELL), (c + 1) * CELL)
                    ny = min(max(y, r * CELL), (r + 1) * CELL)
                    if (nx - x) ** 2 + (ny - y) ** 2 < radius ** 2:
                        return True
        for t in self.things:
            if t.solid and abs(x - t.x) < t.w / 2 + radius and abs(y - t.y) < max(t.w, 0.3) / 2 + radius:
                if t.shape == "ball":
                    if math.hypot(x - t.x, y - t.y) < t.w / 2 + radius:
                        return True
                else:
                    return True
        return False

    # --- camera --------------------------------------------------------------

    def render(self, x: float, y: float, heading: float, w: int = 320, h: int = 240) -> np.ndarray:
        """First-person view from the head camera, BGR uint8 (h, w, 3)."""
        img = np.empty((h, w, 3), np.uint8)
        f = (w / 2) / math.tan(FOV / 2)
        horizon = h / 2 - f * math.tan(HEAD_PITCH)       # tilting down moves the horizon up
        img[: int(horizon)] = CEILING

        # Rays: column c looks at heading - atan((c - w/2)/f) (left of the image = left of the robot).
        cols = np.arange(w)
        offs = np.arctan((cols - w / 2 + 0.5) / f)
        ang = heading - offs
        dx, dy = np.cos(ang), np.sin(ang)

        # Floor casting: each pixel below the horizon hits the floor at a known distance; colour it by
        # that spot's room material, darker with distance. Walls are drawn over it below.
        fy = np.arange(max(0, int(horizon) + 1), h)
        if len(fy):
            ahead = CAM_H * f / (fy - horizon + 0.5)                          # straight-ahead distance per row
            along = ahead[:, None] / np.cos(offs)[None, :]
            fx_w, fy_w = x + dx[None, :] * along, y + dy[None, :] * along
            rr = np.clip(np.floor(fy_w / CELL).astype(int), 0, self.rows - 1)
            cc = np.clip(np.floor(fx_w / CELL).astype(int), 0, self.cols - 1)
            col = floor_colors(self.grid[rr, cc], fx_w, fy_w)
            shade = np.clip(1.05 - along / 14, 0.45, 1.0)[..., None]
            img[fy[0]:] = (col * shade).astype(np.uint8)
        steps = np.arange(0.02, 14.0, 0.02)
        px = x + dx[:, None] * steps[None, :]
        py = y + dy[:, None] * steps[None, :]
        r = np.clip(np.floor(py / CELL).astype(int), 0, self.rows - 1)
        c = np.clip(np.floor(px / CELL).astype(int), 0, self.cols - 1)
        hit = self.wall[r, c]
        first = np.where(hit.any(axis=1), hit.argmax(axis=1), len(steps) - 1)
        dist = steps[first]
        perp = dist * np.cos(offs)                        # no fish-eye
        # Which room the ray was in just before the wall -> wall colour; x/y face -> shading.
        prev = np.maximum(first - 1, 0)
        room_cells = self.grid[r[cols, prev], c[cols, prev]]
        hx, hy = px[cols, first], py[cols, first]
        fx = np.abs(hx / CELL - np.round(hx / CELL))
        fyv = np.abs(hy / CELL - np.round(hy / CELL))
        vertical_face = fx < fyv
        zbuf = perp.copy()

        top = (horizon - f * (WALL_H - CAM_H) / np.maximum(perp, 1e-3)).astype(int)
        bot = (horizon + f * CAM_H / np.maximum(perp, 1e-3)).astype(int)
        for i in range(w):
            col = np.array(WALL_COLORS.get(room_cells[i], (180, 180, 180)), float)
            light = (0.95 if vertical_face[i] else 0.78) * max(0.45, 1 - perp[i] / 12)
            t, b = max(0, top[i]), min(h, bot[i])
            if b > t:
                img[t:b, i] = (col * light).astype(np.uint8)
                if bot[i] < h:
                    img[min(h - 1, bot[i])-1:min(h, bot[i]+1), i] = (40, 50, 60)    # skirting line

        # Furniture billboards, far to near, clipped by the wall depth buffer.
        fwd = np.array([math.cos(heading), math.sin(heading)])
        right = np.array([math.sin(heading), -math.cos(heading)])
        order = sorted(self.things, key=lambda t: -((t.x - x) ** 2 + (t.y - y) ** 2))
        for t in order:
            rel = np.array([t.x - x, t.y - y])
            depth = float(rel @ fwd)
            if depth < 0.15:
                continue
            sx = w / 2 + f * float(rel @ right) / depth
            sw = f * t.w / depth
            stop = horizon - f * (t.z + t.h - CAM_H) / depth
            sbot = horizon - f * (t.z - CAM_H) / depth
            x0, x1 = int(sx - sw / 2), int(sx + sw / 2)
            y0, y1 = int(stop), int(sbot)
            if x1 <= 0 or x0 >= w or y1 <= y0 or x1 - x0 < 1:
                continue
            spr = _resize(t.sprite, x1 - x0, y1 - y0)
            for i in range(max(0, x0), min(w, x1)):
                if depth >= zbuf[i]:
                    continue
                si = i - x0
                ys0, ys1 = max(0, y0), min(h, y1)
                if ys1 <= ys0:
                    continue
                seg = spr[ys0 - y0: ys1 - y0, si]
                mask = seg[:, 3] > 0
                dim = max(0.5, 1 - depth / 14)
                img[ys0:ys1, i][mask] = (seg[mask, :3] * dim).astype(np.uint8)
        return img

    def render_floor(self, x: float, y: float, heading: float, w: int = 320, h: int = 240) -> np.ndarray:
        """The floor camera: a top-down view of the floor just ahead (bottom edge = robot's front)."""
        fwd = np.array([math.cos(heading), math.sin(heading)])
        right = np.array([math.sin(heading), -math.cos(heading)])
        ahead = NAV_FAR - (np.arange(h) + 0.5) / h * (NAV_FAR - NAV_NEAR)          # top row = farthest
        side = ((np.arange(w) + 0.5) / w * 2 - 1) * NAV_HALF_WIDTH                  # left .. right
        A, S = np.meshgrid(ahead, side, indexing="ij")
        wx = x + A * fwd[0] + S * right[0]
        wy = y + A * fwd[1] + S * right[1]
        r = np.clip(np.floor(wy / CELL).astype(int), 0, self.rows - 1)
        c = np.clip(np.floor(wx / CELL).astype(int), 0, self.cols - 1)
        img = floor_colors(self.grid[r, c], wx, wy).astype(np.uint8)
        wall = self.wall[r, c]
        img[wall] = FLOOR_WALL
        labels = []
        for t in self.things:
            if t.shape == "ball":
                m = (wx - t.x) ** 2 + (wy - t.y) ** 2 <= (t.w / 2) ** 2
            else:
                m = (np.abs(wx - t.x) <= t.w / 2) & (np.abs(wy - t.y) <= max(t.w, 0.3) / 2)
            if m.any():
                img[m] = t.color
                ys, xs = np.nonzero(m)
                labels.append((t, int(xs.mean()), int(ys.mean())))
        if cv2 is not None:
            for t, cx, cy in labels:
                if t.shape != "ball":
                    tc = (0, 0, 0) if sum(t.color) > 400 else (255, 255, 255)
                    cv2.putText(img, t.name, (max(2, cx - 30), cy + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, tc, 1, cv2.LINE_AA)
            cv2.rectangle(img, (w // 2 - 40, h - 6), (w // 2 + 40, h - 1), (20, 20, 20), -1)   # robot's front edge
        return img

    def project(self, t: Thing, x: float, y: float, heading: float, w: int = 640, h: int = 480):
        """Ground-truth box of a thing in the head camera image (x0, y0, x1, y1 in pixels), or None.

        Same projection as render() (ignores occlusion by walls). Used to score models' box-drawing."""
        f = (w / 2) / math.tan(FOV / 2)
        horizon = h / 2 - f * math.tan(HEAD_PITCH)
        fwd = (math.cos(heading), math.sin(heading))
        right = (math.sin(heading), -math.cos(heading))
        rx, ry = t.x - x, t.y - y
        depth = rx * fwd[0] + ry * fwd[1]
        if depth < 0.15:
            return None
        sx = w / 2 + f * (rx * right[0] + ry * right[1]) / depth
        sw = f * t.w / depth
        x0, x1 = sx - sw / 2, sx + sw / 2
        y0 = horizon - f * (t.z + t.h - CAM_H) / depth
        y1 = horizon - f * (t.z - CAM_H) / depth
        if x1 <= 0 or x0 >= w or y1 <= 0 or y0 >= h:
            return None
        return (max(0, x0), max(0, y0), min(w, x1), min(h, y1))

    def camera_jpeg(self, x: float, y: float, heading: float, quality: int = 80, cam: str = "head") -> bytes:
        img = self.render(x, y, heading) if cam == "head" else self.render_floor(x, y, heading)
        if cv2 is None:
            raise RuntimeError("OpenCV is needed to encode camera frames")
        if cam == "head":
            img = cv2.resize(img, (640, 480), interpolation=cv2.INTER_LINEAR)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes()

    def map_image(self, x: float | None = None, y: float | None = None, heading: float | None = None,
                  trail: list[tuple[float, float]] | None = None, scale: int = 40) -> np.ndarray:
        """Top-down map (for people watching tests, never shown to the AI)."""
        img = np.full((self.rows * scale, self.cols * scale, 3), 255, np.uint8)
        for r in range(self.rows):
            for c in range(self.cols):
                ch = self.grid[r, c]
                color = (60, 60, 60) if ch == "#" else WALL_COLORS.get(ch, (230, 230, 230))
                y0 = (self.rows - 1 - r) * scale
                img[y0:y0 + scale, c * scale:(c + 1) * scale] = color
        to_px = lambda px, py: (int(px / CELL * scale), int((self.rows - py / CELL) * scale))
        if cv2 is not None:
            for code, name in ROOMS.items():
                ys, xs = np.where(self.grid == code)
                cx, cy = xs.mean() * CELL + CELL / 2, ys.mean() * CELL + CELL / 2
                cv2.putText(img, name.upper(), (to_px(cx, cy)[0] - 50, to_px(cx, cy)[1]), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (90, 90, 90), 1, cv2.LINE_AA)
            for t in self.things:
                p0 = to_px(t.x - t.w / 2, t.y + 0.15)
                p1 = to_px(t.x + t.w / 2, t.y - 0.15)
                cv2.rectangle(img, p0, p1, t.color, -1)
                cv2.putText(img, t.name, (p0[0], p0[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_AA)
            if trail:
                pts = np.array([to_px(px, py) for px, py in trail], np.int32)
                cv2.polylines(img, [pts], False, (0, 160, 255), 2)
            if x is not None:
                p = to_px(x, y)
                cv2.circle(img, p, int(ROBOT_R / CELL * scale), (0, 0, 200), -1)
                q = to_px(x + 0.5 * math.cos(heading), y + 0.5 * math.sin(heading))
                cv2.line(img, p, q, (0, 0, 0), 2)
        return img


def _make_sprite(t: Thing) -> np.ndarray:
    """Billboard texture with alpha: the object's shape in its colour, labelled with its name."""
    pw, ph = max(16, int(t.w * 160)), max(16, int(t.h * 160))
    spr = np.zeros((ph, pw, 4), np.uint8)
    if t.shape == "ball":
        yy, xx = np.mgrid[0:ph, 0:pw]
        inside = (xx - pw / 2) ** 2 / (pw / 2) ** 2 + (yy - ph / 2) ** 2 / (ph / 2) ** 2 <= 1
        spr[inside, :3] = t.color
        spr[inside, 3] = 255
    else:
        spr[:, :, :3] = t.color
        spr[:, :, 3] = 255
        spr[:4, :, :3] = spr[-4:, :, :3] = spr[:, :4, :3] = spr[:, -4:, :3] = (30, 30, 30)
    if cv2 is not None and t.shape != "ball":
        text_color = (0, 0, 0) if sum(t.color) > 400 else (255, 255, 255)
        scale = max(0.4, min(pw / (len(t.name) * 22), ph / 40))
        size, _ = cv2.getTextSize(t.name, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.putText(spr, t.name, ((pw - size[0]) // 2, (ph + size[1]) // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (*text_color, 255), 2, cv2.LINE_AA)
    return spr


def _resize(spr: np.ndarray, w: int, h: int) -> np.ndarray:
    w, h = max(1, w), max(1, h)
    if cv2 is not None:
        return cv2.resize(spr, (w, h), interpolation=cv2.INTER_NEAREST)
    ys = (np.arange(h) * spr.shape[0] / h).astype(int)
    xs = (np.arange(w) * spr.shape[1] / w).astype(int)
    return spr[ys][:, xs]


# Office, facing the living-room doorway (east), centred on it (the doorway spans y 4.0-5.0). Until
# 2026-10-25 this was y 4.75, leaving 3 cm between the robot and the door frame.
START = (2.0, 4.5, 0.0)
