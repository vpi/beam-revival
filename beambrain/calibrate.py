"""Measure a real Beam+'s cameras from printed markers, for the AI driver (docs/ai-driving.md).

  python3 -m beambrain.calibrate --head-height 1.315 --axle-to-front 0.095

Reads the snapshots the brain saved (`kill -USR1` it; ~/beam-brain/calibration/) and placements.txt there,
one line per snapshot:  <stamp> <name> <metres ahead> <metres left> [upright]
  - flat:    docs/calibration-sheet-letter.pdf (four 80 mm markers) lying on the floor, its arrow edge
             <metres ahead> of the base's front edge, its middle <metres left> of the centre line
  - upright: docs/calibration-head-letter.pdf (one 180 mm marker) standing on the floor, facing the robot
Flat sheets measure the floor camera, upright ones the head camera. Writes ~/beam-brain/data/cameras.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np

# docs/calibration-sheet-letter.pdf: marker centres (mm from the page's top-left), 80 mm markers
SHEET_H, SHEET_MID, SMALL_HALF = 279.4, 107.95, 40
SHEET_CENTRES = {0: (58, 60), 1: (158, 60), 2: (58, 160), 3: (158, 160)}
# docs/calibration-head-letter.pdf: one 180 mm marker, id 10, its centre 159.4 mm above the floor
BIG_ID, BIG_HALF, BIG_CENTRE_Z = 10, 0.09, 0.1594
CORNERS = [(-1, -1), (1, -1), (1, 1), (-1, 1)]     # ArUco order: top-left, top-right, bottom-right, bottom-left


def minimise(cost, x0, steps):
    """Nelder-Mead (no scipy on a plain brain host)."""
    n = len(x0)
    pts = [np.array(x0, float)] + [np.array(x0, float) + np.eye(n)[i] * steps[i] for i in range(n)]
    vals = [cost(p) for p in pts]
    for _ in range(4000):
        order = np.argsort(vals)
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < 1e-12:
            break
        cen = np.mean(pts[:-1], axis=0)
        xr = cen + (cen - pts[-1]); fr = cost(xr)
        if fr < vals[0]:
            xe = cen + 2 * (cen - pts[-1]); fe = cost(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = cen + 0.5 * (pts[-1] - cen); fc = cost(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [pts[0] + 0.5 * (p - pts[0]) for p in pts[1:]]
                vals = [cost(p) for p in pts]
    return pts[int(np.argmin(vals))]


def markers(path: Path) -> dict[int, np.ndarray]:
    im = cv2.imread(str(path))
    if im is None:
        return {}
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    found, ids, _ = det.detectMarkers(im)
    grey = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    out = {}
    for c, i in zip(found, [] if ids is None else ids.flatten()):
        cv2.cornerSubPix(grey, c, (3, 3), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
        out[int(i)] = c[0]
    return out


# --- floor camera: radial undistortion + homography onto the floor -------------------------------------

def _undistort(uv, k, c, w):
    q = (uv - c) / w
    r2 = (q ** 2).sum(1, keepdims=True)
    return c + q * w * (1 + k[0] * r2 + k[1] * r2 ** 2)


def _floor_fit(p, uv, xy, w, h):
    k, c = p[:2], np.array([w / 2 + p[2], h / 2 + p[3]])
    H, _ = cv2.findHomography(_undistort(uv, k, c, w), xy)
    pred = cv2.perspectiveTransform(_undistort(uv, k, c, w).reshape(-1, 1, 2), H).reshape(-1, 2)
    return H, k, c, pred


def fit_floor(uv, xy, w, h):
    p = minimise(lambda p: float(((_floor_fit(p, uv, xy, w, h)[3] - xy) ** 2).sum()), [0, 0, 0, 0], [0.05, 0.05, 5, 5])
    return p, _floor_fit(p, uv, xy, w, h)


# --- head camera: position, angles, focal length, barrel ------------------------------------------------

def head_project(p, X, height, w, h):
    cam_x, yaw, pitch, roll, f, k1 = p
    v = X - np.array([cam_x, 0, height])
    cy, sy, cp, sp, cr, sr = map(float, (np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch), np.cos(roll), np.sin(roll)))
    a = np.c_[cy * v[:, 0] + sy * v[:, 1], -sy * v[:, 0] + cy * v[:, 1], v[:, 2]]
    b = np.c_[cp * a[:, 0] - sp * a[:, 2], a[:, 1], sp * a[:, 0] + cp * a[:, 2]]
    x, y, z = -b[:, 1], -b[:, 2], b[:, 0]
    x, y = cr * x - sr * y, sr * x + cr * y
    nx, ny = x / z, y / z
    g = 1 + k1 * (nx ** 2 + ny ** 2)
    return np.c_[w / 2 + f * nx * g, h / 2 + f * ny * g]


def fit_head(uv, X, height, w, h):
    cost = lambda p: float(((head_project(p, X, height, w, h) - uv) ** 2).sum())
    return minimise(cost, [-0.1, 0.0, math.radians(5), 0.0, w / 2, 0.0], [0.05, 0.05, 0.05, 0.05, 40, 0.05])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.path.expanduser("~/beam-brain/calibration"))
    ap.add_argument("--out", default=os.path.expanduser("~/beam-brain/data/cameras.json"))
    ap.add_argument("--head-height", type=float, required=True, help="floor to the head camera's lens, metres")
    ap.add_argument("--axle-to-front", type=float, required=True, help="wheel axle to the base's front edge, metres")
    args = ap.parse_args()
    d = Path(args.dir)
    flat, upright = [], []
    for line in (d / "placements.txt").read_text().splitlines():
        p = line.split()
        if len(p) < 4 or line.startswith("#"):
            continue
        (upright if p[4:5] == ["upright"] else flat).append((p[0], p[1], float(p[2]), float(p[3])))

    uv, xy, names, size = [], [], [], None
    for stamp, name, ahead, left in flat:
        img = d / f"{stamp}-nav.jpg"
        size = cv2.imread(str(img)).shape[1::-1]
        for i, c in markers(img).items():
            if i not in SHEET_CENTRES:
                continue
            cx, cy = SHEET_CENTRES[i]
            for (u, v), (sx, sy) in zip(c, CORNERS):
                px, py = cx + sx * SMALL_HALF, cy + sy * SMALL_HALF
                uv.append((u, v)); xy.append((ahead + (SHEET_H - py) / 1000, left + (SHEET_MID - px) / 1000)); names.append(name)
    uv, xy, names = np.array(uv, float), np.array(xy, float), np.array(names)
    w, h = size
    p, (H, k, c, pred) = fit_floor(uv, xy, w, h)
    err = np.linalg.norm(pred - xy, axis=1)
    print(f"floor camera: {len(uv)} points, fit error median {np.median(err) * 100:.1f} cm, max {err.max() * 100:.1f} cm")
    for n in sorted(set(names)):
        m = names != n
        p2, _ = fit_floor(uv[m], xy[m], w, h)
        H2, k2, c2, _ = _floor_fit(p2, uv[m], xy[m], w, h)
        e = np.linalg.norm(cv2.perspectiveTransform(_undistort(uv[~m], k2, c2, w).reshape(-1, 1, 2), H2).reshape(-1, 2) - xy[~m], axis=1)
        print(f"  without {n}: predicts it to {np.median(e) * 100:.1f} cm (max {e.max() * 100:.1f})")
    floor = {"w": w, "h": h, "k": k.tolist(), "centre": c.tolist(), "H": H.tolist(),
             "seen": {"ahead": [round(float(xy[:, 0].min()), 2), round(float(xy[:, 0].max()), 2)],
                      "left": [round(float(xy[:, 1].min()), 2), round(float(xy[:, 1].max()), 2)]}}

    uv, X, names = [], [], []
    for stamp, name, ahead, left in upright:
        img = d / f"{stamp}-head.jpg"
        size = cv2.imread(str(img)).shape[1::-1]
        c = markers(img).get(BIG_ID)
        if c is None:
            print(f"  {name}: marker not found, skipped")
            continue
        for (u, v), (sx, sy) in zip(c, CORNERS):
            uv.append((u, v)); X.append((ahead, left - sx * BIG_HALF, BIG_CENTRE_Z - sy * BIG_HALF)); names.append(name)
    uv, X, names = np.array(uv, float), np.array(X, float), np.array(names)
    w, h = size
    p = fit_head(uv, X, args.head_height, w, h)
    e = np.linalg.norm(head_project(p, X, args.head_height, w, h) - uv, axis=1)
    print(f"head camera: lens {p[0]:+.3f} m from the front edge, yaw {math.degrees(p[1]):.1f}, pitch down "
          f"{math.degrees(p[2]):.1f}, roll {math.degrees(p[3]):.1f} degrees, field of view "
          f"{math.degrees(2 * math.atan(w / 2 / p[4])):.0f} degrees; fit error median {np.median(e):.1f} px")
    for n in sorted(set(names)):
        m = names != n
        p2 = fit_head(uv[m], X[m], args.head_height, w, h)
        e = np.linalg.norm(head_project(p2, X[~m], args.head_height, w, h) - uv[~m], axis=1)
        print(f"  without {n}: predicts it to {np.median(e):.1f} px")
    head = {"w": w, "h": h, "height": args.head_height, "cam_x_from_front": p[0], "yaw": p[1],
            "pitch_down": p[2], "roll": p[3], "f": p[4], "k1": p[5]}

    Path(args.out).write_text(json.dumps({"head": head, "floor": floor, "axle_to_front_m": args.axle_to_front},
                                         indent=1) + "\n")
    print("saved", args.out)


if __name__ == "__main__":
    main()
