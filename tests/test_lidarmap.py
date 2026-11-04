"""The lidar map, from a simulated drive through a small flat with imperfect wheels and gyro."""

import math

import cv2
import numpy as np
import pytest

from beambrain import lidarmap as lm

# Two rooms joined by a doorway, a sofa in the first (metres; the drive starts at the origin facing +x).
WALLS = [((-1, -1.5), (4, -1.5)), ((4, -1.5), (4, 1.5)), ((4, 1.5), (-1, 1.5)), ((-1, 1.5), (-1, -1.5)),
         ((4, -1.5), (7, -1.5)), ((7, -1.5), (7, 3.5)), ((7, 3.5), (4, 3.5)), ((4, 3.5), (4, 1.5)),
         ((2.0, 0.9), (3.2, 0.9)), ((3.2, 0.9), (3.2, 1.5)), ((2.0, 0.9), (2.0, 1.5))]
DOOR = (0.2, 1.2)                  # the gap in the x = 4 wall (y from, to)


def segments():
    segs = []
    for (ax, ay), (bx, by) in WALLS:
        if ax == bx == 4 and min(ay, by) < DOOR[1] and max(ay, by) > DOOR[0]:
            lo, hi = min(ay, by), max(ay, by)
            if lo < DOOR[0]:
                segs.append(((4, lo), (4, DOOR[0])))
            if hi > DOOR[1]:
                segs.append(((4, DOOR[1]), (4, hi)))
        else:
            segs.append(((ax, ay), (bx, by)))
    return np.array(segs, float)


SEGS = segments()


def lidar(pose, rng, mask=(135, 210), segs=None):
    """A turn of the lidar from the true pose: 1 degree steps clockwise from ahead, cm, 0 where hidden."""
    x, y, th = pose
    ox, oy = x - lm.LIDAR_BEHIND_AXLE_M * math.cos(th), y - lm.LIDAR_BEHIND_AXLE_M * math.sin(th)
    a = np.radians(np.arange(360))
    dirs = np.c_[np.cos(th - a), np.sin(th - a)]                 # clockwise from the robot's heading
    segs = SEGS if segs is None else segs
    p, q = segs[:, 0], segs[:, 1]
    e = q - p
    best = np.full(360, np.inf)
    for k in range(len(segs)):
        den = dirs[:, 0] * e[k, 1] - dirs[:, 1] * e[k, 0]
        w = p[k] - [ox, oy]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (w[0] * e[k, 1] - w[1] * e[k, 0]) / den
            u = (w[0] * dirs[:, 1] - w[1] * dirs[:, 0]) / den
        hit = (den != 0) & (t > 0) & (u >= 0) & (u <= 1)
        best = np.where(hit & (t < best), t, best)
    d = best + rng.normal(0, 0.01, 360)
    cm = np.where(np.isfinite(d), np.round(d * 100), 0).astype(int)
    cm[(np.arange(360) >= mask[0]) & (np.arange(360) <= mask[1])] = 0
    return cm.tolist()


def drive(waypoints, rng, odo_scale=1.03, gyro_drift_deg_per_m=0.8):
    """Telemetry rows along the waypoints: the true path, the wheels reading 3 % long, the gyro drifting."""
    rows, pose, odo, gyro = [], (0.0, 0.0, 0.0), 0.0, 0.0
    truth = []
    for wx, wy in waypoints:
        while True:
            x, y, th = pose
            want = math.atan2(wy - y, wx - x)
            turn = (want - th + math.pi) % (2 * math.pi) - math.pi
            dist = math.hypot(wx - x, wy - y)
            if dist < 0.03:
                break
            if abs(turn) > math.radians(3):
                step_th, step = math.copysign(min(abs(turn), math.radians(3)), turn), 0.0
                ang = 0.3
            else:
                step_th, step, ang = turn, min(0.04, dist), 0.0
            pose = (x + step * math.cos(th + step_th), y + step * math.sin(th + step_th), th + step_th)
            odo += step * odo_scale
            gyro += math.degrees(step_th) + gyro_drift_deg_per_m * step
            truth.append(pose)
            rows.append({"yaw": gyro, "odo": odo, "ang": ang, "charging": len(rows) == 0, "cm": lidar(pose, rng)})
    return rows, truth


def test_map_of_a_drive_has_the_walls_where_they_are():
    rng = np.random.default_rng(0)
    rows, truth = drive([(2.5, 0), (3.5, 0.0), (5.5, 0.7), (6.0, 2.5), (5.0, 2.8), (5.5, 0.6), (3.0, -0.6),
                         (0.5, -0.8), (0.0, 0.0)], rng)
    b = lm.build(rows)
    # Where it ends: back at the start, which the wheels and gyro alone get wrong by a lot more.
    end = b.poses[-1]
    assert math.hypot(end[0] - truth[-1][0], end[1] - truth[-1][1]) < 0.08
    # The walls on the map are on the real walls.
    walls = np.argwhere(b.grid.lo > 1.0)                           # (row, col) = (y, x)
    xy = b.grid.origin + (walls[:, ::-1] + 0.5) * lm.RES
    near = np.zeros(len(xy))
    for i, (px, py) in enumerate(xy):
        p, q = SEGS[:, 0], SEGS[:, 1]
        t = np.clip(((px - p[:, 0]) * (q[:, 0] - p[:, 0]) + (py - p[:, 1]) * (q[:, 1] - p[:, 1]))
                    / ((q - p) ** 2).sum(1), 0, 1)
        near[i] = np.hypot(p[:, 0] + t * (q[:, 0] - p[:, 0]) - px, p[:, 1] + t * (q[:, 1] - p[:, 1]) - py).min()
    assert len(xy) > 300 and np.mean(near < 0.10) > 0.95
    assert b.dock is not None and b.fit > 0.5


def test_recorder_keeps_a_scan_every_few_centimetres():
    r = lm.Recorder()
    t = {"board": {"yaw_deg": 0.0, "odometry_m": 0.0}, "scan": {"turn": 1, "cm": [100] * 360}}
    assert r.feed(t)
    assert not r.feed(t)                                           # the same turn again
    t = {"board": {"yaw_deg": 0.0, "odometry_m": 0.01}, "scan": {"turn": 2, "cm": [100] * 360}}
    assert not r.feed(t)                                           # hardly moved
    t = {"board": {"yaw_deg": 0.0, "odometry_m": 0.08}, "scan": {"turn": 3, "cm": [100] * 360}}
    assert r.feed(t) and len(r.rows) == 2 and r.metres == pytest.approx(0.08)


def test_render_is_a_picture_of_what_was_seen():
    rows, _ = drive([(2.0, 0.0)], np.random.default_rng(1))
    png, info = lm.render(lm.build(rows))
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] == info["width"] and info["scans"] > 10


async def test_brain_records_while_driven_and_sends_the_map(tmp_path):
    import asyncio

    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore

    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    sent = []
    brain.send = lambda m: asyncio.sleep(0, sent.append(m))
    brain.maps_dir = tmp_path
    brain.telemetry = {"board": {}}
    await brain.on_command({"kind": "map_start"})
    assert brain.mapping is None and "lidar isn't sending" in sent[-1]["text"]   # asleep: no scans

    rows, _ = drive([(2.0, 0.0), (2.0, 0.5)], np.random.default_rng(2))
    for i, r in enumerate(rows):
        brain.telemetry = {"board": {"yaw_deg": r["yaw"], "odometry_m": r["odo"], "actual_angular": r["ang"],
                                     "charging": r["charging"]}, "scan": {"turn": i, "cm": r["cm"]}}
        if i == 0:
            await brain.on_command({"kind": "map_start"})
        brain.mapping.feed(brain.telemetry)
    await brain.on_command({"kind": "map_stop"})
    assert brain.mapping is None
    assert list(tmp_path.glob("drive-*.jsonl.gz")) and (tmp_path / "map.png").exists()
    m = [s for s in sent if s.get("type") == "map"][-1]
    assert m["png"] and m["scans"] > 10 and m["dock"]


async def test_a_map_must_start_on_the_charger_and_never_replaces_one_without_the_dock(tmp_path):
    import asyncio

    from beambrain.brain import Brain
    from beambrain.greeter import Greeter
    from beambrain.people import PeopleStore

    brain = Brain(None, PeopleStore(":memory-not-used:"), None, Greeter())
    sent = []
    brain.send = lambda m: asyncio.sleep(0, sent.append(m))
    brain.maps_dir = tmp_path
    rows, _ = drive([(2.0, 0.0)], np.random.default_rng(3))
    brain.telemetry = {"board": {"yaw_deg": 0.0, "odometry_m": 0.0, "charging": False}, "scan": {"turn": 0, "cm": rows[0]["cm"]}}
    await brain.on_command({"kind": "map_start"})
    assert brain.mapping is None and "on its charger" in sent[-1]["text"]
    # Started on the charger after all, but the recording somehow lacks it (an old recorder, a glitch): kept out.
    (tmp_path / "map.json").write_text('{"keep": true}')
    brain.telemetry["board"]["charging"] = True
    await brain.on_command({"kind": "map_start"})
    for i, r in enumerate(rows[1:]):
        brain.mapping.feed({"board": {"yaw_deg": r["yaw"], "odometry_m": r["odo"], "actual_angular": r["ang"],
                                      "charging": False}, "scan": {"turn": i + 1, "cm": r["cm"]}})
    brain.mapping.rows[0]["charging"] = False
    await brain.on_command({"kind": "map_stop"})
    assert (tmp_path / "map.json").read_text() == '{"keep": true}' and "map in use is kept" in sent[-1]["text"]
