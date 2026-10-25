"""AI driver against the virtual house, with a scripted stand-in for the model (no network)."""

import math

import pytest

pytest.importorskip("cv2")

from beamctl.world import START, World  # noqa: E402
from beambrain.bakeoff import TASKS, Rig  # noqa: E402
from beambrain.driver import AIDriver  # noqa: E402
from beambrain.llm import Reply  # noqa: E402


class ScriptedLLM:
    """Returns the given tool calls in order, like a model would."""

    model = "scripted"

    def __init__(self, calls):
        self.calls = list(calls)
        self.prompts = []

    async def chat(self, session, messages, tools=None, max_tokens=400, temperature=0.2):
        self.prompts.append(messages)
        name, args = self.calls.pop(0)
        return Reply("", [{"name": name, "arguments": args}], 0.01, 100, 10, {})


class GroundTruthLLM(ScriptedLLM):
    """Driver steps come from the script; "draw a box" questions are answered with exact boxes
    computed from the simulator, so these tests check the driver's geometry and motion, not a model."""

    def __init__(self, calls, rig):
        super().__init__(calls)
        self.rig = rig

    async def chat(self, session, messages, tools=None, max_tokens=400, temperature=0.2):
        text = messages[-1]["content"][0]["text"] if isinstance(messages[-1]["content"], list) else ""
        if not text.startswith("Find "):
            return await super().chat(session, messages, tools, max_tokens, temperature)
        import json as _json
        box = self._truth(text)
        reply = {"found": True, "box": box} if box else {"found": False}
        return Reply(_json.dumps(reply), [], 0.01, 100, 10, {})

    def _truth(self, text):
        b, w = self.rig.board, self.rig.world
        floor_view = "straight down" in text
        if "gap in the wall" in text:
            # Floor camera: the office->living doorway gap, x = 4.5, y 4.0..5.0.
            near, far, hw = 0.25, 1.75, 0.9
            pts = [self._robot_frame(4.5, yy) for yy in (4.0, 5.0)]
            if not all(near < a < far for a, _ in pts):
                return None
            xs = [0.5 - left / (2 * hw) for _, left in pts]
            ys = [(far - a) / (far - near) for a, _ in pts]
            return [max(0, min(xs)), min(ys) - 0.05, min(1, max(xs)), max(ys) + 0.05]
        if "doorway" in text:
            # The office->living doorway threshold, x = 4.5, y 4.0..5.0, as seen by the head camera.
            pts = [self._head_floor_px(4.5, yy) for yy in (4.05, 4.95)]
            if None in pts:
                return None
            (xa, ya), (xb, yb) = pts
            return [min(xa, xb), min(ya, yb) - 0.2, max(xa, xb), max(ya, yb)]
        thing = next(t for t in w.things if t.name.lower() in text.lower())
        if floor_view:
            ahead, left = self._robot_frame(thing.x, thing.y)
            near, far, hw = 0.25, 1.75, 0.9
            if not (near < ahead < far and abs(left) < hw):
                return None
            cx, cy, r = 0.5 - left / (2 * hw), (far - ahead) / (far - near), thing.w / 2
            return [cx - r / (2 * hw), cy - r / (far - near), cx + r / (2 * hw), cy + r / (far - near)]
        gt = w.project(thing, b.x, b.y, b.heading)
        return None if gt is None else [gt[0] / 640, gt[1] / 480, gt[2] / 640, gt[3] / 480]

    def _robot_frame(self, x, y):
        b = self.rig.board
        dx, dy = x - b.x, y - b.y
        return (dx * math.cos(b.heading) + dy * math.sin(b.heading),
                -dx * math.sin(b.heading) + dy * math.cos(b.heading))

    def _head_floor_px(self, x, y):
        from beambrain.geometry import CameraModel
        cam = CameraModel()
        ahead, left = self._robot_frame(x, y)
        if ahead < 0.3:
            return None
        f = 320 / math.tan(math.radians(cam.head_fov_deg / 2))
        horizon = 240 - f * math.tan(math.radians(cam.head_pitch_down_deg))
        return (0.5 - f * left / ahead / 640, (horizon + f * cam.head_height_m / ahead) / 480)


async def test_approach_drives_up_to_the_target():
    async with Rig(start=(6.0, 4.4, math.radians(25))) as rig:      # living room, ball ahead-left
        llm = GroundTruthLLM([("approach", {"target": "the red ball", "why": "go"}), ("done", {"summary": "x"})], rig)
        out = await AIDriver(rig.brain, llm, "test", allow_say=False, big_actions=True).run(rig.session)
        assert out.outcome == "done", out.summary
        ok, detail = TASKS["ball"]["check"](rig.board, rig.world)
        assert ok, f"{detail}; driver said: {out.steps[0].result}"
        assert out.requests > 2                                     # locate calls are counted


async def test_go_through_doorway_reaches_next_room():
    async with Rig() as rig:                                         # office, facing the living-room doorway
        llm = GroundTruthLLM([("go_through_doorway", {"which": "the doorway ahead", "why": "go"}),
                              ("done", {"summary": "x"})], rig)
        out = await AIDriver(rig.brain, llm, "test", allow_say=False, big_actions=True).run(rig.session)
        assert rig.world.room_at(rig.board.x, rig.board.y) == "living room", out.steps[0].result
        assert rig.board.bumps == 0


def test_geometry_round_trip():
    from beambrain.geometry import CameraModel
    from beambrain.grounding import parse_box
    cam = CameraModel()
    for d in (2.0, 3.0, 5.0):                                        # floor row <-> distance
        f = 320 / math.tan(math.radians(34))
        horizon = 240 - f * math.tan(math.radians(8))
        assert abs(cam.head_floor_distance((horizon + f * 1.25 / d) / 480) - d) < 1e-6
    assert abs(cam.head_angle_left(0.5)) < 1e-9 and cam.head_angle_left(0.2) > 0
    assert parse_box('{"found": true, "box": [100, 200, 300, 400]}')[1].x1 == 0.3       # 0..1000 style
    assert parse_box('Sure! {"found": false}') == (False, None)


def test_tool_calls_written_as_text_are_recovered():
    from beambrain.driver import parse_text_call
    assert parse_text_call("I'll do turn(degrees=-25) now") == {"name": "turn", "arguments": {"degrees": -25.0}}
    assert parse_text_call("drive(1.5)") == {"name": "drive", "arguments": {"meters": 1.5}}
    assert parse_text_call('done(summary="at the ball")')["arguments"]["summary"] == "at the ball"
    assert parse_text_call("I think we should look around") is None


def test_world_geometry():
    w = World()
    assert w.room_at(*START[:2]) == "office" and not w.collides(*START[:2])
    assert w.collides(4.7, 5.8)                    # wall between office and living room
    assert not w.collides(4.75, 4.5)               # ...but not in the doorway
    assert w.room_at(8, 1.5) == "kitchen" and w.room_at(2, 2) == "bedroom"
    img = w.render(*START)
    assert img.shape == (240, 320, 3) and img.std() > 10


async def test_turn_and_drive_are_accurate():
    async with Rig() as rig:
        llm = ScriptedLLM([("turn", {"degrees": 90, "why": "t"}), ("turn", {"degrees": -90, "why": "t"}),
                           ("drive", {"meters": 1.5, "why": "d"}), ("done", {"summary": "ok"})])
        out = await AIDriver(rig.brain, llm, "test", allow_say=False, guides=True).run(rig.session)
        assert out.outcome == "done" and len(out.steps) == 4
        assert abs(math.degrees(rig.board.heading)) < 4          # +90 then -90
        assert abs(rig.board.x - (START[0] + 1.5)) < 0.08         # drove 1.5 m east
        assert abs(rig.board.y - START[1]) < 0.05
        assert "Heading" in llm.prompts[1][1]["content"][0]["text"]
        assert "turned 90" in llm.prompts[1][1]["content"][0]["text"]   # step history goes back to the model
        # Guides: three images (head, floor, map) and a position in words from odometry.
        last = llm.prompts[3][1]["content"]
        assert sum(c["type"] == "image_url" for c in last) == 3
        assert "1.5 m ahead of the start" in last[0]["text"]
        assert "+30, +15, 0" in llm.prompts[0][0]["content"]             # guides explained to the model


def test_guides_geometry_and_breadcrumbs():
    import cv2
    import numpy as np
    from beambrain.guides import Breadcrumbs, angle_x, annotate_floor, annotate_head
    assert angle_x(0, 640, 68) == 320
    assert angle_x(30, 640, 68) < 320 < angle_x(-30, 640, 68)            # +30 (left) is left of centre
    assert abs((320 - angle_x(15, 640, 68)) - (angle_x(-15, 640, 68) - 320)) <= 1
    blank = cv2.imencode(".jpg", np.zeros((480, 640, 3), np.uint8))[1].tobytes()
    assert cv2.imdecode(np.frombuffer(annotate_head(blank), np.uint8), 1).max() > 0
    assert cv2.imdecode(np.frombuffer(annotate_floor(blank), np.uint8), 1).max() > 0
    c = Breadcrumbs()
    c.update(10.0, 90.0)                       # start: odometry 10 m, compass 90
    c.update(11.0, 90.0)                       # 1 m straight ahead
    c.update(11.0, 180.0)                      # turn left 90
    c.update(11.5, 180.0)                      # 0.5 m to the (start's) left
    assert abs(c.x - 1.0) < 1e-6 and abs(c.y - 0.5) < 1e-6 and round(c.heading_deg) == 90
    c.bump()
    assert "1.0 m ahead of the start and 0.5 m to its left" in c.describe()
    assert len(c.render()) > 1000


async def test_blocked_by_wall_is_reported_and_stops():
    async with Rig() as rig:
        llm = ScriptedLLM([("turn", {"degrees": 90, "why": "face north wall"}),
                           ("drive", {"meters": 3, "why": "into the wall"}), ("done", {"summary": "x"})])
        out = await AIDriver(rig.brain, llm, "test", allow_say=False).run(rig.session)
        assert "BLOCKED" in out.steps[1].result
        assert out.blocked_moves == 1 and rig.board.bumps >= 1
        assert rig.board.y < 6.0                                  # never went through the wall


async def test_human_takeover_aborts_the_ai():
    async with Rig() as rig:
        pilot = await rig.session.ws_connect(f"http://127.0.0.1:{rig.port}/ws")
        llm = ScriptedLLM([("drive", {"meters": 3, "why": "long drive"})] + [("done", {"summary": "x"})])
        driver = AIDriver(rig.brain, llm, "test", allow_say=False)
        import asyncio
        task = asyncio.create_task(driver.run(rig.session))
        await asyncio.sleep(1.5)
        for _ in range(5):                                        # a person presses a key
            await pilot.send_json({"type": "drive", "linear": 0.0, "angular": 0.3})
            await asyncio.sleep(0.1)
        out = await asyncio.wait_for(task, 10)
        assert out.outcome == "aborted" and "person" in out.summary
        # Everyone was told the AI was in charge, then that it stopped.
        seen_active = seen_done = False
        loop = asyncio.get_running_loop()
        end = loop.time() + 3                                       # telemetry keeps coming; read for a while
        while loop.time() < end and not (seen_active and seen_done):
            try:
                m = await asyncio.wait_for(pilot.receive_json(), 0.5)
            except asyncio.TimeoutError:
                break
            if m.get("type") == "ai":
                seen_active |= m["active"]
                seen_done |= (not m["active"]) and m.get("outcome") == "aborted"
        assert seen_active and seen_done
        await pilot.close()


async def test_task_checks_score_correctly():
    async with Rig(start=(9.8, 4.4, math.pi / 2)) as rig:           # 0.7 m south of the ball, facing it
        ok, detail = TASKS["ball"]["check"](rig.board, rig.world)
        assert ok, detail
    async with Rig(start=(8.0, 1.5, 0.0)) as rig:
        assert TASKS["kitchen"]["check"](rig.board, rig.world)[0]
    async with Rig() as rig:                                          # still in the office
        assert not TASKS["ball"]["check"](rig.board, rig.world)[0]
        assert not TASKS["kitchen"]["check"](rig.board, rig.world)[0]
