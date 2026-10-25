"""AI driver: a vision model drives the robot, one tool call per step.

Each step the model sees the latest head-camera frame plus a short text history of its own previous
steps, and calls exactly one tool. Moves are executed closed-loop on the robot's telemetry (gyro heading
and wheel odometry), so "turn 30" really turns 30 degrees and "drive 1" stops after one metre, or
early if the robot is blocked.

Safety: every drive command goes through the motor server at brain priority, so any person on a
driving page takes over instantly; when that happens (server answers "denied"), the AI stops for good.
While it runs, the robot screen and every driving page show that the AI is in charge.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

import aiohttp

from .geometry import CameraModel, target_from_floor, target_from_head
from .grounding import locate
from .guides import Breadcrumbs, annotate_floor, annotate_head
from .llm import LLM, APIError

log = logging.getLogger("beambrain.driver")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()

SYSTEM = """You are the autonomous driver of a small telepresence robot in a home.
You get two camera images each step:
- HEAD camera: forward-facing at head height, about 68 degrees wide. It sees far, but things on the
  floor closer than about 1.5 m drop out of the bottom of this view.
- FLOOR camera: looks straight down at the floor just in front of the robot. The bottom edge is the
  robot's front (marked with a black bar); the top edge is about 1.75 m ahead; it is about 1.8 m wide.
  Use it for the last metre: anything close to you on the floor shows up here.
Each step you MUST call exactly one tool. After each move you get new images.

How to drive well:
- Turning: positive degrees = turn LEFT, negative = turn RIGHT. Distances are in meters.
- Something in the LEFT part of the image is to your left: turn left (positive) to face it.
- Doorways are openings in the walls; other rooms are through them. Walls block you.
- If the target isn't visible, look around first (e.g. turn 90 at a time) before driving.
- Drive in short hops (0.5 to 2 m) and look again. If a move reports "blocked", turn away and try another way.
- Remember where you've been from your step history; don't repeat the same moves in a loop.
- When you are close to the target (about 1 m, roughly facing it), call done.
- If the task really can't be done, call give_up with the reason."""

BIG_ACTIONS_HELP = """
You also have two big actions that do the aiming and distance-judging for you. Prefer them:
- approach(target): drives up to a thing you can see, e.g. approach("the red ball"), and stops about
  1 m from it, facing it. If it says it can't see the target, turn to look for it first.
- go_through_doorway(which): drives through a doorway you can see, e.g. go_through_doorway("the
  doorway on the left") or ("the doorway ahead"). Use it to move between rooms.
Use turn (e.g. 90 degrees at a time) to look around when what you need isn't in view."""

BIG_TOOLS = [
    {"type": "function", "function": {
        "name": "approach", "description": "Drive up to a visible thing and stop about 1 m from it, facing it.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "what to approach, e.g. 'the red ball'"},
            "why": {"type": "string"}}, "required": ["target", "why"]}}},
    {"type": "function", "function": {
        "name": "go_through_doorway", "description": "Drive through a visible doorway into the next room.",
        "parameters": {"type": "object", "properties": {
            "which": {"type": "string", "description": "which doorway, e.g. 'the doorway on the left'"},
            "why": {"type": "string"}}, "required": ["which", "why"]}}},
]

GUIDES_HELP = """
Guides drawn on your images (yellow):
- HEAD camera: vertical marks labelled +30, +15, 0, -15, -30 are directions in degrees. To face
  something at the +15 mark, call turn(15); between +15 and +30, about turn(22). 0 is straight ahead.
  Short marks at the image edges labelled 2m, 3m, 5m show how far away the floor is at that height.
- FLOOR camera: dashed lines show 0.5 m, 1 m and 1.5 m ahead of the robot.
- MAP: your own path so far, from your wheel odometry (not a floor plan: walls are not shown, only
  places you bumped into, marked X). Use it to remember where you've already been and explore new
  directions instead of circling."""

TOOLS = [
    {"type": "function", "function": {
        "name": "turn", "description": "Turn in place. Positive = LEFT, negative = RIGHT. -180..180 degrees.",
        "parameters": {"type": "object", "properties": {
            "degrees": {"type": "number"},
            "why": {"type": "string", "description": "a few words: why this move"}},
            "required": ["degrees", "why"]}}},
    {"type": "function", "function": {
        "name": "drive", "description": "Drive straight. Positive = forward, negative = backward. -1..3 meters.",
        "parameters": {"type": "object", "properties": {
            "meters": {"type": "number"},
            "why": {"type": "string", "description": "a few words: why this move"}},
            "required": ["meters", "why"]}}},
    {"type": "function", "function": {
        "name": "say", "description": "Say something out loud to people near the robot.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "The task is complete.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
    {"type": "function", "function": {
        "name": "give_up", "description": "The task can't be done.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
]


class Aborted(Exception):
    pass


@dataclass
class Step:
    n: int
    tool: str | None
    args: dict
    result: str
    seconds_thinking: float
    seconds_moving: float = 0.0
    finish: str | None = None          # the API's finish_reason (e.g. "length" = cut off while thinking)
    recovered: bool = False            # tool call parsed from plain text rather than a real tool call


_TEXT_CALL = re.compile(r"\b(turn|drive|say|done|give_up)\s*\(([^)]*)\)", re.S)
_ARG_NAMES = {"turn": "degrees", "drive": "meters", "say": "text", "done": "summary", "give_up": "reason"}


def parse_text_call(text: str) -> dict | None:
    """Recover a tool call a model wrote as text, e.g. 'turn(degrees=-25)' or 'drive(1.5)'."""
    m = _TEXT_CALL.search(text or "")
    if not m:
        return None
    name, inner = m.group(1), m.group(2).strip()
    args: dict = {}
    for part in [p for p in re.split(r",\s*(?=\w+\s*[=:])", inner) if p.strip()]:
        kv = re.match(r"\s*\"?(\w+)\"?\s*[=:]\s*(.+)", part)
        key, val = (kv.group(1), kv.group(2)) if kv else (_ARG_NAMES[name], part)
        val = val.strip().strip("\"'")
        try:
            args[key] = float(val)
        except ValueError:
            args[key] = val
    return {"name": name, "arguments": args}


@dataclass
class Outcome:
    outcome: str                       # done | gave_up | out_of_steps | timeout | aborted | error
    summary: str
    steps: list[Step] = field(default_factory=list)
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    blocked_moves: int = 0


def _wrap(deg: float) -> float:
    return (deg + 180) % 360 - 180


class AIDriver:
    # Generous token budget: thinking models deliberate before answering, and the deliberation counts.
    # Too small and they're cut off before choosing anything (finish_reason "length").
    MAX_TOKENS = 8000

    def __init__(self, brain, llm: LLM, task: str, max_steps: int = 20, time_limit_s: float = 900,
                 turn_speed: float = 0.8, drive_speed: float = 0.35, allow_say: bool = True,
                 guides: bool = False, big_actions: bool = False, camera: CameraModel | None = None):
        """guides: draw angle/distance marks on the images and add the breadcrumb map (see guides.py).
        Off by default: in the 2026-10-25 bake-off they made models aim too early, clip door frames and
        think much longer (4/24 successes vs 9/24 without). See docs/ai-driving-results.md.
        big_actions: add approach() and go_through_doorway(), which the driver executes from boxes the
        model draws (see grounding.py, geometry.py)."""
        self.brain, self.llm, self.task = brain, llm, task
        self.guides = guides
        self.big_actions = big_actions
        self.cam = camera or CameraModel()
        self._session = None
        self._out: Outcome | None = None
        self._motion_end = 0.0
        self.crumbs = None
        self.max_steps, self.time_limit_s = max_steps, time_limit_s
        self.turn_speed, self.drive_speed = turn_speed, drive_speed
        self.allow_say = allow_say
        self._abort_reason: str | None = None

    def abort(self, reason: str) -> None:
        self._abort_reason = reason

    # --- telemetry helpers ---------------------------------------------------------

    def _board(self) -> dict:
        t = self.brain.telemetry or {}
        return t.get("board") or {}

    def _check(self) -> None:
        if self._abort_reason:
            raise Aborted(self._abort_reason)
        if self.brain.human_took_over.is_set():
            raise Aborted("a person took over driving")
        drv = (self.brain.telemetry or {}).get("drive") or {}
        if drv.get("driver") == "human":
            raise Aborted("a person took over driving")
        if drv.get("estop"):
            raise Aborted("E-STOP was pressed")

    async def _cmd(self, linear: float, angular: float) -> None:
        await self.brain.send({"type": "drive", "linear": linear, "angular": angular})

    async def _settle(self) -> None:
        """Command zero until the robot has actually stopped (up to ~1 s)."""
        for _ in range(12):
            await self._cmd(0.0, 0.0)
            await asyncio.sleep(0.1)
            b = self._board()
            if abs(b.get("actual_linear", 0)) < 0.02 and abs(b.get("actual_angular", 0)) < 0.05:
                break
        self._motion_end = time.monotonic()

    # --- big actions: the model names the target, the driver aims and measures ----------------

    async def _look(self, cam: str = "head") -> str | None:
        """A frame from this camera taken after the last move ended."""
        for _ in range(40):
            f = self.brain.frames.get(cam)
            if f and f[1] > self._motion_end + 0.15:
                return f[0]
            await asyncio.sleep(0.1)
        f = self.brain.frames.get(cam)
        return f[0] if f else None

    async def _locate(self, jpeg: str, target: str):
        found, box, r = await locate(self.llm, self._session, jpeg, target)
        if self._out is not None:
            self._out.requests += 1
            self._out.tokens_in += r.prompt_tokens or 0
            self._out.tokens_out += r.completion_tokens or 0
        return found, box

    async def approach(self, target: str, stop_at: float = 1.0) -> str:
        """Turn toward and drive up to `target`, re-locating it after every move."""
        use_floor, adjustments = False, 0
        for _ in range(7):
            self._check()
            if use_floor:
                nav = await self._look("nav")
                found, box = await self._locate(nav, f"{target} (this image looks straight down at the floor)") if nav else (False, None)
                if found:
                    angle, edge = target_from_floor(box, self.cam)
                    if abs(angle) > 8:
                        await self.turn(angle)
                        adjustments += 1
                        continue
                    step = edge - stop_at
                    if step < 0.08:
                        return f"arrived: about {edge:.1f} m from {target}, facing it"
                    res, blocked = await self.drive(min(step, 1.5))
                    if blocked:
                        await self.drive(-0.2)
                        return f"stopped short of {target}: {res}. Something is in between"
                    adjustments += 1
                    continue
                use_floor = False                       # not in the floor camera: look with the head camera
            head = await self._look("head")
            found, box = await self._locate(head, target) if head else (False, None)
            if not found:
                return (f"can't see {target} from here; turn to look for it" if adjustments == 0
                        else f"lost sight of {target} after {adjustments} moves")
            angle, dist = target_from_head(box, self.cam)
            if abs(angle) > 3:
                await self.turn(angle)
                adjustments += 1
            if dist is None or dist < self.cam.head_min_floor_m + 0.3:
                use_floor = True                        # base is below the head camera's view: close in
                if dist is None:
                    continue
            step = min(dist - stop_at, 2.5)
            if step < 0.1:
                return f"arrived: about {dist:.1f} m from {target}, facing it"
            res, blocked = await self.drive(step)
            adjustments += 1
            if blocked:
                await self.drive(-0.2)
                return (f"stopped short of {target}: {res}. There is probably a wall or furniture in between; "
                        f"if it's in another room, go through the doorway first")
            if dist - step < 2.0:
                use_floor = True
        return f"got closer to {target} but didn't confirm arriving after {adjustments} moves"

    async def _align_on_floor_gap(self) -> float | None:
        """Up close, line up with the gap in the wall using the floor camera. Returns metres to the gap."""
        nav = await self._look("nav")
        if not nav:
            return None
        found, box = await self._locate(
            nav, "the gap in the wall: the place where the floor continues between the dark wall areas "
                 "(this image looks straight down at the floor; the dark areas are walls)")
        if not found:
            return None
        angle, near = target_from_floor(box, self.cam)
        if abs(angle) > 3:
            await self.turn(angle)
        return near

    async def go_through_doorway(self, which: str) -> str:
        """Find the doorway with the head camera and get close; line up on the floor camera's view of the
        gap; drive through. (Up close the doorway's threshold is below the head camera's view.)"""
        target = f"{which}: an open doorway (a gap in a wall that leads to another room)"
        for attempt in range(3):
            self._check()
            head = await self._look("head")
            found, box = await self._locate(head, target) if head else (False, None)
            if found:
                angle, dist = target_from_head(box, self.cam)
                if abs(angle) > 3:
                    await self.turn(angle)
                if dist is not None and dist > 1.2:
                    res, blocked = await self.drive(dist - 0.9)          # stop ~0.9 m before the threshold
                    if blocked:
                        await self.drive(-0.3)
                        continue
            gap = await self._align_on_floor_gap()
            if gap is None and not found:
                return f"can't see {which}; turn to look for it"
            res, blocked = await self.drive((gap if gap is not None else 0.9) + 1.0)
            if not blocked:
                return "went through the doorway into the next room"
            await self.drive(-0.3)                                      # clipped the frame: back off, re-aim
        return f"couldn't get through {which}: kept getting blocked by its frame"

    # --- moves -------------------------------------------------------------------

    async def turn(self, degrees: float) -> str:
        degrees = max(-180.0, min(180.0, float(degrees)))
        start = self._board().get("yaw_deg", 0.0)
        target = start + degrees
        t0 = time.monotonic()
        while True:
            self._check()
            err = _wrap(target - self._board().get("yaw_deg", 0.0))
            if abs(err) < 2.0:
                break
            if time.monotonic() - t0 > 4 + abs(degrees) / 30:
                await self._settle()
                return f"turn timed out, turned {_wrap(self._board().get('yaw_deg', 0) - start):.0f} of {degrees:.0f} degrees"
            w = max(0.25, min(self.turn_speed, abs(math.radians(err)) * 1.5))
            await self._cmd(0.0, math.copysign(w, err))
            await asyncio.sleep(0.1)
        await self._settle()
        return f"turned {degrees:.0f} degrees"

    async def drive(self, meters: float) -> tuple[str, bool]:
        meters = max(-1.0, min(3.0, float(meters)))
        sign = 1 if meters >= 0 else -1
        start = self._board().get("odometry_m", 0.0)
        t0 = time.monotonic()
        stalled_since = None
        while True:
            self._check()
            b = self._board()
            moved = (b.get("odometry_m", 0.0) - start) * sign
            remaining = abs(meters) - moved
            if remaining < 0.03:
                break
            now = time.monotonic()
            # Blocked: commanding motion but the wheels aren't turning (sim: bumped into something).
            if now - t0 > 0.8 and abs(b.get("actual_linear", 0.0)) < 0.02:
                stalled_since = stalled_since or now
                if now - stalled_since > 0.5:
                    await self._settle()
                    return f"BLOCKED after {moved:.2f} m of {abs(meters):.2f} m (something is in the way)", True
            else:
                stalled_since = None
            if now - t0 > 3 + abs(meters) / 0.12:
                await self._settle()
                return f"drive timed out after {moved:.2f} m", True
            v = max(0.12, min(self.drive_speed, remaining * 0.8))
            await self._cmd(sign * v, 0.0)
            await asyncio.sleep(0.1)
        await self._settle()
        return f"drove {meters:.2f} m", False

    # --- the loop ------------------------------------------------------------------

    async def _fresh_frame(self, after: float) -> str | None:
        for _ in range(40):
            if self.brain.last_jpeg and self.brain.last_frame_at > after:
                return self.brain.last_jpeg
            await asyncio.sleep(0.1)
        return self.brain.last_jpeg

    async def _label(self, active: bool, **extra) -> None:
        msg = {"type": "ai", "active": active, "model": self.llm.model, "task": self.task, **extra}
        await self.brain.send(msg)

    def _prompt(self, step: int, history: list[Step], jpeg_b64: str, nav_b64: str | None = None) -> list[dict]:
        b = self._board()
        lines = [f"Task: {self.task}", f"This is step {step} of at most {self.max_steps}.",
                 f"Heading: {b.get('yaw_deg', 0):.0f} degrees (compass of this robot; turning left increases it)."]
        if self.guides and self.crumbs is not None:
            lines.append(f"Position (from odometry): {self.crumbs.describe()}.")
            jpeg_b64 = _b64(annotate_head(base64.b64decode(jpeg_b64)))
            if nav_b64:
                nav_b64 = _b64(annotate_floor(base64.b64decode(nav_b64)))
        if history:
            lines.append("Your previous steps (oldest first):")
            for s in history[-8:]:
                why = f" (why: {s.args.get('why')})" if s.args.get("why") else ""
                lines.append(f"  {s.n}. {s.tool}({json.dumps({k: v for k, v in s.args.items() if k != 'why'})}){why} -> {s.result}")
        lines.append("Here are the camera views now. Call one tool.")
        content = [{"type": "text", "text": "\n".join(lines)},
                   {"type": "text", "text": "HEAD camera:"},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + jpeg_b64}}]
        if nav_b64:
            content += [{"type": "text", "text": "FLOOR camera (bottom edge = robot's front):"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + nav_b64}}]
        if self.guides and self.crumbs is not None:
            content += [{"type": "text", "text": "MAP of your path so far:"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(self.crumbs.render())}}]
        system = SYSTEM + (GUIDES_HELP if self.guides else "") + (BIG_ACTIONS_HELP if self.big_actions else "")
        return [{"role": "system", "content": system}, {"role": "user", "content": content}]

    async def run(self, session: aiohttp.ClientSession | None = None) -> Outcome:
        own_session = session is None
        session = session or aiohttp.ClientSession()
        out = Outcome("error", "")
        self._session, self._out = session, out
        t_start = time.monotonic()
        self.brain.human_took_over.clear()
        history: list[Step] = []
        last_move_end = time.monotonic()
        tools = TOOLS if self.allow_say else [t for t in TOOLS if t["function"]["name"] != "say"]
        if self.big_actions:
            tools = tools + BIG_TOOLS
        log.info("AI task (%s): %s", self.llm.model, self.task)
        await self._label(True, step=0, max_steps=self.max_steps, action="starting")
        # Breadcrumbs: dead reckoning from the robot's own telemetry, updated 10x a second.
        self.crumbs = Breadcrumbs()
        b0 = self._board()
        self.crumbs.update(b0.get("odometry_m", 0.0), b0.get("yaw_deg", 0.0))

        def track(m: dict) -> None:
            b = m.get("board") or {}
            if "odometry_m" in b:
                self.crumbs.update(b["odometry_m"], b.get("yaw_deg", 0.0))
        self.brain.telemetry_listeners.append(track)
        try:
            for n in range(1, self.max_steps + 1):
                self._check()
                if time.monotonic() - t_start > self.time_limit_s:
                    out.outcome, out.summary = "timeout", f"ran out of time after {n - 1} steps"
                    break
                jpeg = await self._fresh_frame(last_move_end + 0.15)
                if jpeg is None:
                    raise Aborted("no camera frames")
                await self._label(True, step=n, max_steps=self.max_steps, action="thinking")
                nav = self.brain.frames.get("nav")
                nav_b64 = nav[0] if nav and nav[1] > last_move_end else None   # only if taken after the move
                try:
                    r = await self.llm.chat(session, self._prompt(n, history, jpeg, nav_b64), tools=tools,
                                            max_tokens=self.MAX_TOKENS)
                except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                    out.requests += 1
                    history.append(Step(n, "error", {}, f"model call failed: {str(e)[:80]}", 0.0))
                    await asyncio.sleep(2)
                    continue
                out.requests += 1
                out.tokens_in += r.prompt_tokens or 0
                out.tokens_out += r.completion_tokens or 0
                self._check()
                finish = ((r.raw.get("choices") or [{}])[0]).get("finish_reason") if r.raw else None
                recovered = False
                if r.tool_calls:
                    call = r.tool_calls[0]
                else:
                    call = parse_text_call(r.text)          # wrote the call as text instead of calling it
                    recovered = call is not None
                if call is None:
                    why = ("you were cut off while still thinking, before choosing; decide faster, with less "
                           "deliberation" if finish == "length" else "no tool was called; you must call exactly one tool")
                    history.append(Step(n, None, {}, why, r.seconds, finish=finish))
                    continue
                name, args = call["name"], call["arguments"] or {}
                t_move = time.monotonic()
                if name == "turn":
                    await self._label(True, step=n, max_steps=self.max_steps,
                                      action=f"turning {'left' if float(args.get('degrees', 0)) > 0 else 'right'} "
                                             f"{abs(float(args.get('degrees', 0))):.0f}°", why=args.get("why"))
                    result = await self.turn(args.get("degrees", 0))
                elif name == "drive":
                    await self._label(True, step=n, max_steps=self.max_steps,
                                      action=f"driving {'forward' if float(args.get('meters', 0)) >= 0 else 'back'} "
                                             f"{abs(float(args.get('meters', 0))):.1f} m", why=args.get("why"))
                    result, blocked = await self.drive(args.get("meters", 0))
                    out.blocked_moves += blocked
                    if blocked:
                        self.crumbs.bump()
                elif name == "approach" and self.big_actions:
                    target = str(args.get("target", "the target"))[:80]
                    await self._label(True, step=n, max_steps=self.max_steps, action=f"approaching {target}",
                                      why=args.get("why"))
                    result = await self.approach(target)
                    if "BLOCKED" in result:
                        out.blocked_moves += 1
                        self.crumbs.bump()
                elif name == "go_through_doorway" and self.big_actions:
                    which = str(args.get("which", "the doorway ahead"))[:80]
                    await self._label(True, step=n, max_steps=self.max_steps, action=f"going through {which}",
                                      why=args.get("why"))
                    result = await self.go_through_doorway(which)
                elif name == "say":
                    text = str(args.get("text", ""))[:200]
                    await self._label(True, step=n, max_steps=self.max_steps, action="speaking")
                    await self.say(text)
                    result = "said it"
                elif name == "done":
                    history.append(Step(n, name, args, "finished", r.seconds, finish=finish, recovered=recovered))
                    out.outcome, out.summary = "done", str(args.get("summary", ""))[:300]
                    break
                elif name == "give_up":
                    history.append(Step(n, name, args, "gave up", r.seconds, finish=finish, recovered=recovered))
                    out.outcome, out.summary = "gave_up", str(args.get("reason", ""))[:300]
                    break
                else:
                    result = f"unknown tool {name!r}"
                last_move_end = time.monotonic()
                history.append(Step(n, name, args, result, r.seconds, last_move_end - t_move, finish, recovered))
                log.info("step %d: %s(%s) -> %s", n, name, json.dumps(args)[:120], result)
            else:
                out.outcome, out.summary = "out_of_steps", f"used all {self.max_steps} steps"
        except Aborted as e:
            out.outcome, out.summary = "aborted", str(e)
            log.warning("AI task aborted: %s", e)
        except Exception as e:
            out.outcome, out.summary = "error", f"{type(e).__name__}: {e}"
            log.exception("AI task failed")
        finally:
            if track in self.brain.telemetry_listeners:
                self.brain.telemetry_listeners.remove(track)
            try:
                await self._settle()
                await self.brain.send({"type": "stop"})      # release control
            except Exception:
                pass
            out.steps = history
            out.seconds = time.monotonic() - t_start
            await self._label(False, outcome=out.outcome, summary=out.summary, steps=len(history))
            if own_session:
                await session.close()
        log.info("AI task finished: %s (%s) in %d steps, %.0f s", out.outcome, out.summary, len(history), out.seconds)
        return out

    async def say(self, text: str) -> None:
        wav = None
        if self.brain.speaker is not None:
            try:
                wav = await asyncio.to_thread(self.brain.speaker.wav, text)
            except Exception:
                log.exception("speech failed")
        # Marked as the AI speaking, so nobody mistakes it for the person on the call.
        await self.brain.send({"type": "say", "text": text, "by": "ai",
                               "wav": base64.b64encode(wav).decode() if wav else None})
