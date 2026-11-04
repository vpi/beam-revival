import math

import numpy as np
import pytest

from beambrain import dock
from beambrain.calibrate import head_project
from beambrain.geometry import CameraModel

# Close to our Beam+'s measured head camera (98 degrees wide, 1.315 m up, just behind the front edge).
HEAD = {"w": 640, "h": 480, "height": 1.315, "cam_x_from_front": -0.105, "yaw": 0.01,
        "pitch_down": math.radians(0.8), "roll": 0.005, "f": 278.0, "k1": -0.05}
CAM = CameraModel(head=HEAD, axle_to_front_m=0.095, head_w=640, head_h=480)


def seen(pose, ids=(20, 21, 22), noise=0.0, seed=0):
    p = [HEAD[k] for k in ("cam_x_from_front", "yaw", "pitch_down", "roll", "f", "k1")]
    uv = head_project(p, dock.place_sheet(pose, dock.sheet_points(ids)), HEAD["height"], 640, 480)
    uv = uv + np.random.default_rng(seed).normal(0, noise, uv.shape)
    return {i: uv[4 * k:4 * k + 4] for k, i in enumerate(ids)}


@pytest.mark.parametrize("pose", [(2.0, 0.0, 0.0), (2.5, 0.6, math.radians(-25)), (1.4, -0.3, math.radians(15))])
def test_sighting_recovers_where_the_sheet_is(monkeypatch, pose):
    monkeypatch.setattr(dock, "find_markers", lambda im: seen(pose))
    s = dock.sight_dock(np.zeros((480, 640, 3), np.uint8), CAM, use=(20, 21, 22))
    assert s.ahead == pytest.approx(pose[0] + 0.095, abs=0.005)
    assert s.left == pytest.approx(pose[1], abs=0.005)
    assert s.facing_deg == pytest.approx(math.degrees(pose[2]), abs=0.5)
    assert s.markers == [20, 21, 22] and s.err_px < 0.1
    assert dock.sight_dock(np.zeros((480, 640, 3), np.uint8), CAM).markers == [20]     # by default the big one only


def test_noisy_corners_still_place_the_sheet(monkeypatch):
    pose = (2.0, 0.3, math.radians(10))
    monkeypatch.setattr(dock, "find_markers", lambda im: seen(pose, noise=0.5))
    s = dock.sight_dock(np.zeros((480, 640, 3), np.uint8), CAM)
    assert s.distance == pytest.approx(math.hypot(2.095, 0.3), abs=0.05)
    assert s.bearing_deg == pytest.approx(math.degrees(math.atan2(0.3, 2.095)), abs=0.5)


def test_no_big_marker_no_dock(monkeypatch):
    monkeypatch.setattr(dock, "find_markers", lambda im: seen((2.0, 0, 0), ids=(21, 22)))
    assert dock.sight_dock(np.zeros((480, 640, 3), np.uint8), CAM) is None


def test_finds_markers_in_a_drawn_sheet():
    import cv2
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    page = np.full((300, 240), 255, np.uint8)
    for i, (cx, cy, half) in dock.SHEET_MARKERS.items():
        side = int(2 * half)
        page[int(cy - half) + 20:int(cy - half) + 20 + side, int(cx - half) + 10:int(cx - half) + 10 + side] = \
            cv2.aruco.generateImageMarker(d, i, side)
    found = dock.find_markers(cv2.cvtColor(page, cv2.COLOR_GRAY2BGR))
    assert sorted(found) == [20, 21, 22]


def project_sheet(cam_xy, cam_z, look, pitch_deg=25.0, f=450.0, size=(640, 480), ids=(20, 21, 22)):
    """The dock sheet's marker corners as a pinhole camera sees them. World: the sheet in the plane x = 0, facing
    +x, its middle at y = 0 (its right, as you face it, is +y). The camera at (cam_xy, cam_z), looking `look`
    radians from +x, tilted down `pitch_deg`. Markers not wholly in the picture aren't found."""
    th = math.radians(pitch_deg)
    fwd = np.array([math.cos(look) * math.cos(th), math.sin(look) * math.cos(th), -math.sin(th)])
    right = np.array([math.sin(look), -math.cos(look), 0.0])
    down = np.cross(fwd, right)
    P = np.array([cam_xy[0], cam_xy[1], cam_z])
    found = {}
    for i in ids:
        pts = dock.sheet_points([i])
        corners = []
        for x, y in pts:
            v = np.array([0.0, x, y]) - P
            depth = v @ fwd
            if depth < 0.05:
                break
            corners.append((size[0] / 2 + f * (v @ right) / depth, size[1] / 2 + f * (v @ down) / depth))
        else:
            c = np.array(corners)
            if (c >= 0).all() and (c[:, 0] < size[0]).all() and (c[:, 1] < size[1]).all():
                found[i] = c
    return found


def test_rear_view_finds_the_camera_from_the_markers():
    found = project_sheet((0.9, 0.07), 1.0, math.pi + math.radians(6))
    assert sorted(found) == [20, 21, 22]
    v = dock.rear_view(found, 450.0, (640, 480))
    assert v.heading == pytest.approx(-6.0, abs=0.3)
    assert (v.right, v.out) == (pytest.approx(0.07, abs=0.005), pytest.approx(0.9, abs=0.01))


def test_robot_pose_from_the_rear_camera():
    """A phone 12 cm left of the axle and 5 cm in front of it, 0.9 m up, looking 8 degrees off straight back."""
    def view(x, y, yaw):
        cam = (x + 0.05 * math.cos(yaw) - 0.12 * math.sin(yaw), y + 0.05 * math.sin(yaw) + 0.12 * math.cos(yaw))
        return dock.rear_view(project_sheet(cam, 0.9, yaw + math.pi + math.radians(8), pitch_deg=40), 450.0, (640, 480))
    docked = view(dock.AXLE_TO_SHEET_DOCKED_M, 0.0, 0.0)
    turned, right, out = dock.robot_pose(view(0.9, 0.06, math.radians(-7)), docked)
    assert out == pytest.approx(0.9, abs=0.01) and right == pytest.approx(0.06, abs=0.01)
    assert turned == pytest.approx(7.0, abs=0.3)          # turning right turns the rear view left
