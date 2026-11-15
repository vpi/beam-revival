"""The robot arm (Waveshare RoArm-M2-S) on its USB serial port; see docs/arm.md.

The arm's ESP32 talks JSON, one object per line, at 115200 baud ("T" says which command). It answers {"T":105}
with a feedback line {"T":1051, ...}: the joints in radians (b base, s shoulder, e elbow, t gripper), the
gripper's x/y/z in mm, each servo's load (torB/S/E/H), whether each has torque (torswitchB/S/E/H) and the supply
in volts x 100 (v). Plain-text debug lines come in between, and bad JSON is silently dropped by the arm.

Three things about the firmware shape this module (checked on ours, 2026-11-12/14):
- It plays its "boot" mission at every start: radio control off, then the stow moves. **Any byte that reaches it
  while the mission plays aborts the rest**, radio steps and all. So nothing is sent to a port that appeared less
  than BOOT_GRACE_S ago, or that has just printed boot text, until the arm says the mission finished.
- {"T":0}, which looks like an emergency stop, switches all torque off for 10 s (the arm falls) and makes the
  ESP32 deaf meanwhile. It is never sent: `line()` refuses it.
- Its USB adapter is a CP2102N, the same chip as the lidar's. So the arm is never looked for: it runs only on
  the port named by ARM_DEVICE (a /dev/serial/by-id name), and nothing else writes there.

This file is the protocol and the reader (find the arm, keep its state fresh). Moving it, with limits, owners
and stops, is ArmController's job (the server, phase 3).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import math
import os
import re
import struct
import termios
import time
from dataclasses import dataclass

from .serialport import SerialPort

log = logging.getLogger("beam.arm")

BAUD = 115200
FEEDBACK = 1051                 # the reply to {"T":105}
NEVER = {0}                     # {"T":0}: "emergency stop" = 10 s limp and deaf. Never sent.
POLL_HZ = 10
STALE_S = 0.5                   # feedback older than this isn't trusted
LOST_S = 2.0                    # no feedback for this long: the arm is lost (state "lost") until it answers again
BOOT_GRACE_S = 60.0             # a port younger than this, or that printed boot text this recently, is left alone...
MISSION_DONE = "missionPlay finished"     # ...until the arm prints this (with its debug output on)
LISTEN_S = 1.0                  # an older port: listen this long first, in case it's booting after all
IDENTIFY_S = 1.5                # a feedback line must come this soon after {"T":105}
IDENTIFY_TRIES = 3
RETRY_S = 2.0                   # look for the port this often while it isn't there
# Lines the firmware prints while it starts (InfoPrint 1): seeing one means it (re)booted.
BOOT_TEXT = ("mission", "StepNum", "Power up", "RoArm-M2", "boot")


# --- the protocol ------------------------------------------------------------------------

def line(obj: dict) -> bytes:
    """One command as the arm reads it. Refuses {"T":0} (see NEVER)."""
    if obj.get("T") in NEVER:
        raise ValueError(f'never sent to the arm: {{"T":{obj.get("T")}}} (10 s limp and deaf)')
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def read_state() -> bytes:
    return line({"T": 105})


def joints(base: float, shoulder: float, elbow: float, hand: float, spd: int = 0, acc: int = 10) -> bytes:
    """All four joints at once, radians. spd in servo steps/s (4096 a turn; 0 = as fast as it can)."""
    return line({"T": 102, "base": round(base, 4), "shoulder": round(shoulder, 4), "elbow": round(elbow, 4),
                 "hand": round(hand, 4), "spd": int(spd), "acc": int(acc)})


def joint_deg(joint: int, angle: float, spd: float = 10, acc: float = 10) -> bytes:
    """One joint (1 base, 2 shoulder, 3 elbow, 4 gripper), degrees; spd in degrees/s."""
    if joint not in (1, 2, 3, 4):
        raise ValueError(f"no joint {joint}")
    return line({"T": 121, "joint": joint, "angle": round(angle, 2), "spd": spd, "acc": acc})


def grip(rad: float, spd: int = 0, acc: int = 0) -> bytes:
    """The gripper: 1.57 open .. 3.14 closed."""
    return line({"T": 106, "cmd": round(min(max(rad, 1.57), 3.14), 3), "spd": int(spd), "acc": int(acc)})


def grip_force(tor: int) -> bytes:
    return line({"T": 107, "tor": int(min(max(tor, 50), 1000))})


def torque_limits(b: int, s: int, e: int, h: int) -> bytes:
    """Each servo's torque limit (lower: the arm gives way when pushed)."""
    return line({"T": 112, "mode": 1, "b": int(b), "s": int(s), "e": int(e), "h": int(h)})


def torque(on: bool) -> bytes:
    """Torque on (hold) or off (limp: the arm may drop)."""
    return line({"T": 210, "cmd": 1 if on else 0})


def light(level: int) -> bytes:
    """The LED on the gripper: 0 off .. 255 full."""
    return line({"T": 114, "led": int(min(max(level, 0), 255))})


def quiet() -> bytes:
    """Debug text and command echo off (the boot default is on)."""
    return line({"T": 605, "cmd": 0})


def radio_off() -> list[bytes]:
    """ESP-NOW off: by default it obeys any ESP32 nearby. Not saved in the arm, so sent at every connect too."""
    return [line({"T": 301, "mode": 0}), line({"T": 300, "mode": 0, "mac": "00:00:00:00:00:00"})]


@dataclass
class Feedback:
    b: float                    # joints, radians
    s: float
    e: float
    t: float
    loads: tuple[int, int, int, int]          # base, shoulder, elbow, gripper
    torque: tuple[bool, bool, bool, bool] | None
    volts: float | None
    at: float                   # when it came (monotonic)

    @classmethod
    def parse(cls, d: dict, at: float) -> Feedback | None:
        try:
            b, s, e, t = (float(d[k]) for k in ("b", "s", "e", "t"))
            loads = tuple(int(d.get(k, 0)) for k in ("torB", "torS", "torE", "torH"))
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (b, s, e, t)):
            return None
        sw = [d.get(k) for k in ("torswitchB", "torswitchS", "torswitchE", "torswitchH")]
        v = d.get("v")
        return cls(b, s, e, t, loads, tuple(bool(x) for x in sw) if None not in sw else None,
                   round(v / 100, 2) if isinstance(v, (int, float)) else None, at)

    def degrees(self) -> dict:
        return {k: round(math.degrees(getattr(self, k)), 1) for k in ("b", "s", "e", "t")}


class Lines:
    """Bytes in; the arm's JSON objects and its text lines out. Half lines wait for the rest; junk is dropped."""

    MAX = 4096

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> tuple[list[dict], list[str]]:
        self.buf += data
        *done, self.buf = self.buf.split(b"\n")
        if len(self.buf) > self.MAX:
            self.buf = b""
        objs, texts = [], []
        for raw in done:
            s = raw.decode("utf-8", "replace").strip()
            if not s:
                continue
            if s.startswith("{"):
                try:
                    d = json.loads(s)
                except ValueError:
                    d = None
                if isinstance(d, dict):
                    objs.append(d)
                    continue
            texts.append(s)
        return objs, texts


def is_boot_text(text: str) -> bool:
    return any(k in text for k in BOOT_TEXT)


def keep_lines(fd: int) -> None:
    """Clear DTR and RTS, which on many ESP32 boards reset the chip (not on ours, but cheap). Not on a pty."""
    try:
        fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
    except OSError:
        pass


# --- the reader --------------------------------------------------------------------------

class Arm:
    """The arm on its port: waits out its boot, checks it's the arm, turns its radio control off, and polls its
    state. States: "off" (no port), "booting" (left alone while it starts), "ready" (fresh feedback), "lost"
    (it was there and stopped answering). Sending moves is ArmController's; it goes through send()."""

    def __init__(self, device: str, claimed: set[str] | None = None, *, grace_s: float = BOOT_GRACE_S):
        if not device or device == "auto":
            raise ValueError("the arm runs only on a named port (ARM_DEVICE): its USB chip is the lidar's too")
        self.device = device
        self.claimed = claimed if claimed is not None else set()
        self.claimed.add(device)                 # first: no sensor ever opens it
        self.grace_s = grace_s
        self.port: SerialPort | None = None
        self.state = "off"
        self.feedback: Feedback | None = None
        self.identified = False                  # it has answered like the arm since the port opened
        self.boots = 0                           # how often it was seen starting
        self.sent: list[bytes] = []              # every line written, newest last (kept short; tests read it)
        self._lines = Lines()
        self._quiet_until = 0.0                  # nothing is written before this
        self._mission_done = False
        self._task: asyncio.Task | None = None

    # -- life --

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="arm")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.port is not None:
            self.port.close()
        self.state = "off"

    @property
    def present(self) -> bool:
        return self.port is not None and self.port.is_open

    @property
    def fresh(self) -> bool:
        return self.feedback is not None and time.monotonic() - self.feedback.at < STALE_S

    def send(self, data: bytes) -> bool:
        """Write one line (built by line() and friends). False if it can't be sent now: no port, not
        identified, or the arm is booting (a byte would abort its boot mission)."""
        if not self.present or not self.identified or self.booting:
            return False
        return self._write(data)

    @property
    def booting(self) -> bool:
        return time.monotonic() < self._quiet_until

    # -- inside --

    def _write(self, data: bytes) -> bool:
        if json.loads(data).get("T") in NEVER:             # line() refuses it; this catches hand-made bytes
            raise ValueError("never sent to the arm")
        try:
            self.port.write(data)
        except OSError as e:
            log.warning("arm write failed: %s", e)
            return False
        self.sent = (self.sent + [data])[-200:]
        return True

    def _open(self) -> bool:
        port = SerialPort(self.device, self._on_data, self._on_lost, baud=BAUD)
        try:
            port.open()
        except OSError as e:
            log.debug("arm port %s: %s", self.device, e)
            return False
        keep_lines(port._fd)
        self.port, self.identified, self._mission_done, self._lines = port, False, False, Lines()
        # Booting if the port is new (USB power just came: the arm starts with it): leave it alone a while.
        try:
            age = time.time() - os.stat(self.device).st_ctime
        except OSError:
            age = self.grace_s
        now = time.monotonic()
        self._quiet_until = now + (self.grace_s - age if age < self.grace_s else LISTEN_S)
        if age < self.grace_s:
            self._started("its port is new")
        return True

    def _started(self, why: str) -> None:
        self.boots += 1
        self.state, self.identified = "booting", False
        log.info("arm starting (%s): leaving it alone until its boot mission is done", why)

    def _on_data(self, data: bytes) -> None:
        objs, texts = self._lines.feed(data)
        now = time.monotonic()
        for t in texts:
            if MISSION_DONE in t:
                self._mission_done = True
                self._quiet_until = min(self._quiet_until, now + 0.5)
            elif is_boot_text(t):
                if self.identified:
                    self._mission_done = False         # it restarted while being polled (T:600, power)
                if not self._mission_done:
                    if self.state != "booting":
                        self._started("boot text")
                    self._quiet_until = max(self._quiet_until, now + self.grace_s)
        for d in objs:
            if d.get("T") == FEEDBACK:
                fb = Feedback.parse(d, now)
                if fb is not None:
                    self.feedback = fb

    def _on_lost(self, exc: Exception) -> None:
        log.warning("arm port gone: %s", exc)
        self.state, self.identified = "off", False

    async def _identify(self) -> bool:
        for attempt in range(IDENTIFY_TRIES):
            before = self.feedback.at if self.feedback else 0.0
            self._write(read_state())
            deadline = time.monotonic() + IDENTIFY_S
            while time.monotonic() < deadline and self.present and not self.booting:
                if self.feedback and self.feedback.at > before:
                    return True
                await asyncio.sleep(0.05)
            if not self.present or self.booting:
                return False
            log.info("arm: no answer yet (try %d)", attempt + 1)
        return False

    async def _run(self) -> None:
        told = False
        while True:
            if not self.present:
                self.state = "off"
                if not self._open():
                    if not told:
                        log.info("arm: no port at %s yet", self.device)
                        told = True
                    await asyncio.sleep(RETRY_S)
                    continue
                told = False
            if self.booting:
                await asyncio.sleep(0.1)
                continue
            if not self.identified:
                if await self._identify():
                    self.identified = True
                    for data in [quiet(), *radio_off()]:
                        self._write(data)
                    log.info("arm ready at %s: radio control off", self.device)
                elif not self.booting and self.present:
                    if self.state != "lost":
                        log.warning("arm at %s doesn't answer", self.device)
                    self.state = "lost"
                    await asyncio.sleep(RETRY_S)
                continue
            self._write(read_state())
            await asyncio.sleep(1 / POLL_HZ)
            age = time.monotonic() - self.feedback.at if self.feedback else math.inf
            if age > LOST_S:
                if self.state != "lost":
                    log.warning("arm stopped answering")
                self.state, self.identified = "lost", False
            elif not self.booting and self.identified:
                self.state = "ready"

    def snapshot(self) -> dict:
        fb = self.feedback if self.fresh else None
        return {"present": self.present, "state": self.state,
                "joints": fb.degrees() if fb else None, "loads": list(fb.loads) if fb else None,
                "torque": list(fb.torque) if fb and fb.torque else None, "volts": fb.volts if fb else None}


# --- the controller ----------------------------------------------------------------------

JOINT_NUM = {"base": 1, "shoulder": 2, "elbow": 3, "gripper": 4}
JOINT_KEY = {"base": "b", "shoulder": "s", "elbow": "e", "gripper": "t"}
LOAD_INDEX = {"b": 0, "s": 1, "e": 2, "t": 3}
# Where each joint may be sent, degrees: tighter than the firmware's, from what our mount has been tried in
# (2026-11-14: base 0..94, shoulder -90..+6, elbow 90..180), with a little room.
JOINT_BOX = {"base": (-10.0, 100.0), "shoulder": (-91.0, 15.0), "elbow": (60.0, 181.0), "gripper": (90.0, 180.0)}
STOW = {"base": 93.0, "shoulder": -90.0, "elbow": 180.0}   # along the mast; the boot mission ends here
STOW_TOL_DEG = 5.0              # this close to STOW in every joint: stowed (torque on or off)
STOW_ORDER = ("shoulder", "elbow", "base")   # the Stow button's order, after closing the gripper
GRIP_CLOSED = 180.0             # the gripper shut (3.14 rad); the stow closes it first if it was left open
GRIP_CLOSED_TOL = 5.0
ARRIVED_DEG = 3.5               # a joint this close to its goal has arrived (the servos stop a little short)
SETTLED_S = 0.6                 # ...or no longer moving for this long, within SETTLED_DEG
SETTLED_DEG = 6.0
LOAD_JUMP = 250                 # a joint's load this much bigger than where it started: it met something. By size:
                                # the servos report it signed, and it flips when a joint reverses (a wave)
SPEED_DEG = 20.0                # default speed
MAX_SPEED_DEG = 45.0
JOG_LEAD_DEG = 8.0              # a held button aims this far ahead of where the joint is
# Gestures: named lists of (joint, degrees, speed) from the stow, each ending stowed again. Only angles tried on
# our mount (2026-11-14). Rocky (the AI) may only name one of these: it can't send the arm anywhere else.
GESTURES = {
    "wave": [("elbow", 140, 40), ("elbow", 165, 40), ("elbow", 140, 40), ("elbow", 165, 40)],
    # Posed by the user with the Arm card (base 90, shoulder 10, elbow 75), reached the way the stow came down:
    # the shoulder up first with the elbow folded, then the elbow out; the bump is the elbow out to 60 and back.
    "fist_bump": [("shoulder", 10, 25), ("elbow", 75, 30), ("elbow", 60, 20), ("elbow", 75, 20)],
}
# Gestures taught from the driving page: the joint moves made with its buttons, replayed in the same order from
# the stow (the path the teacher watched), then the stow. Kept in the server's state folder.
TAUGHT_MAX = 12
TAUGHT_MAX_MOVES = 30
TAUGHT_NAME_LEN = 24
TAUGHT_NAME = re.compile(r"[a-z0-9][a-z0-9 ]*")
TAUGHT_SPEED = 25.0
JOG_DEADMAN_S = 0.5             # no jog message for this long (the button let go, the page gone): freeze


class ArmController:
    """Moves the arm one joint at a time and freezes it on any doubt. The server asks it whether the wheels may
    turn (drive_limit) and tells it about the E-stop, sleep, and drivers leaving. Moves are refused, with a reason,
    while it isn't ready, STOP is on, the robot is asleep, or the wheels turn with the arm stowed."""

    def __init__(self, arm: Arm, *, stow: dict | None = None, speed_deg: float = SPEED_DEG,
                 wheels_moving=lambda: False, taught_file: str | None = None):
        """taught_file: where gestures taught from the driving page are kept (None: only until a restart)."""
        self.arm = arm
        self.taught_file = taught_file
        self.taught: dict[str, list[tuple[str, float, float]]] = self._load_taught()
        self.recording: list[list] | None = None  # while teaching: [[joint, degrees], ...] in the order moved
        self._recording_by: object | None = None
        self.stow_pose = dict(STOW if stow is None else stow)
        self.speed = min(max(float(speed_deg), 1.0), MAX_SPEED_DEG)
        self.wheels_moving = wheels_moving
        self.estop = False
        self.asleep = False
        self.by: object | None = None            # who started the move running now
        self.by_kind: str | None = None          # "human" / "brain" / "sleep", for the pages' labels
        self.why: str | None = None              # the last refusal or freeze, for the page
        self._task: asyncio.Task | None = None
        self._jog: list | None = None            # [joint, direction, last message at, its load at the start]
        self.light_level = 0                     # the gripper's LED, 0..255, as last set
        self._light_by: object | None = None     # who switched it on: it goes off when they leave

    # -- state --

    def joints_deg(self) -> dict | None:
        fb = self.arm.feedback if self.arm.fresh else None
        if fb is None:
            return None
        d = fb.degrees()
        return {"base": d["b"], "shoulder": d["s"], "elbow": d["e"], "gripper": d["t"]}

    def stowed(self) -> bool:
        j = self.joints_deg()
        return j is not None and all(abs(j[k] - v) <= STOW_TOL_DEG for k, v in self.stow_pose.items())

    @property
    def moving(self) -> bool:
        return self._task is not None and not self._task.done()

    def limp(self) -> bool:
        fb = self.arm.feedback if self.arm.fresh else None
        return bool(fb and fb.torque and not any(fb.torque))

    def state(self) -> str:
        if self.arm.state != "ready":
            return self.arm.state                 # off, booting, lost
        if self.moving:
            return "moving"
        if self.stowed():
            return "stowed"
        return "limp" if self.limp() else "holding"

    def drive_limit(self) -> str | None:
        """None: the wheels may turn. "out": the arm isn't stowed (no driving). "unknown": where it is isn't
        known (starting up, lost, unplugged): slowly only."""
        st = self.state()
        if st == "stowed":
            return None
        return "unknown" if st in ("off", "booting", "lost") else "out"

    # -- orders --

    def _refuse(self, why: str) -> tuple[bool, str]:
        self.why = why
        return False, why

    def _check(self, leaving_stow: bool = True) -> tuple[bool, str] | None:
        if self.arm.state != "ready":
            return self._refuse({"off": "the arm isn't connected", "booting": "the arm is starting up",
                                 "lost": "the arm doesn't answer"}.get(self.arm.state, "the arm isn't ready"))
        if self.estop:
            return self._refuse("STOP is on: release it first")
        if self.asleep:
            return self._refuse("the robot is asleep")
        if leaving_stow and self.stowed() and self.wheels_moving():
            return self._refuse("the robot is moving: stop it before moving the arm")
        return None

    def move_joint(self, joint: str, deg: float, by: object, kind: str = "human",
                   speed: float | None = None) -> tuple[bool, str]:
        if joint not in JOINT_BOX:
            return self._refuse(f"no joint {joint!r}")
        bad = self._check()
        if bad:
            return bad
        lo, hi = JOINT_BOX[joint]
        if not (isinstance(deg, (int, float)) and math.isfinite(deg) and lo <= deg <= hi):
            return self._refuse(f"the {joint} can go from {lo:g} to {hi:g} degrees")
        self._start([(joint, float(deg))], by, kind, speed)
        return True, f"moving the {joint} to {deg:g} degrees"

    def jog(self, joint: str, direction: int, by: object, kind: str = "human") -> tuple[bool, str]:
        """Hold to move: the page sends this a few times a second while a button is held. Each one aims the joint
        JOG_LEAD_DEG further on (within its box); when they stop coming for JOG_DEADMAN_S, it freezes."""
        if joint not in JOINT_BOX or direction not in (-1, 1):
            return self._refuse("no such jog")
        lo, hi = JOINT_BOX[joint]
        j = self.joints_deg()
        jogging = self._jog is not None and self._jog[0] == joint and self.moving and self.by is by
        bad = self._check(leaving_stow=not jogging)
        if bad:
            return bad
        if j is None:
            return self._refuse("no news from the arm")
        goal = min(max(j[joint] + direction * JOG_LEAD_DEG, lo), hi)
        if direction * (goal - j[joint]) < 2.0:                # under 2 degrees left that way
            return self._refuse(f"the {joint} is at its limit")
        now = time.monotonic()
        if not jogging or self._jog[1] != direction:
            if self._task is not None:
                self._task.cancel()
            self.by, self.by_kind, self.why = by, kind, None
            fb = self.arm.feedback
            self._jog = [joint, direction, now, fb.loads[LOAD_INDEX[JOINT_KEY[joint]]]]
            self._task = asyncio.create_task(self._jogging(), name="arm-jog")
        self._jog[2] = now
        self.arm.send(joint_deg(JOINT_NUM[joint], goal, self.speed, 10))
        return True, f"moving the {joint}"

    async def _jogging(self) -> None:
        """Watches a jog: freezes it when the button's let go, or a load jumps."""
        while True:
            await asyncio.sleep(1 / POLL_HZ)
            joint, _, last, load0 = self._jog
            if time.monotonic() - last > JOG_DEADMAN_S:
                self._jog = None
                self._freeze_quiet()
                self._note_move(joint)
                return
            if not self.arm.fresh or self.arm.state != "ready":
                self._jog = None
                self._freeze("no news from the arm")
                return
            load = self.arm.feedback.loads[LOAD_INDEX[JOINT_KEY[joint]]]
            if abs(load) - abs(load0) > LOAD_JUMP:
                self._jog = None
                self._freeze(f"the {joint} met something", load)
                return

    def _freeze_quiet(self) -> None:
        fb = self.arm.feedback
        if fb is not None:
            self.arm.send(joints(fb.b, fb.s, fb.e, fb.t, spd=0, acc=10))

    def gestures(self) -> list[str]:
        return sorted(GESTURES) + sorted(self.taught)

    def gesture(self, name: str, by: object, kind: str = "human") -> tuple[bool, str]:
        """Play a named gesture (built in, or taught from the driving page). It starts from the stow and ends
        stowed."""
        moves = GESTURES.get(name) or self.taught.get(name)
        if moves is None:
            return self._refuse(f"no gesture {name!r}")
        if self.recording is not None:
            return self._refuse("a gesture is being taught: save it or cancel the recording first")
        bad = self._check()
        if bad:
            return bad
        if not self.stowed():
            return self._refuse("gestures start from the stow: stow the arm first")
        steps = [(j, float(d), float(s)) for j, d, s in moves]
        steps += [(k, self.stow_pose[k], None) for k in ("elbow", "base", "shoulder")]
        self._start(steps, by, kind, None)
        log.info("arm gesture %s (%s)", name, kind)
        return True, f"{name}"

    # -- teaching gestures from the driving page --

    def record(self, on: bool, by: object) -> tuple[bool, str]:
        """Start (from the stow) or cancel recording the joint moves made with the page's buttons (Save as… ends it
        too, keeping them)."""
        if not on:
            was, self.recording, self._recording_by = self.recording, None, None
            return True, "recording cancelled" + (f" ({len(was)} moves not saved)" if was else "")
        bad = self._check()
        if bad:
            return bad
        if not self.stowed():
            return self._refuse("teaching starts from the stow: stow the arm first")
        self.recording, self._recording_by = [], by
        log.info("arm: recording a gesture")
        return True, "recording: move the arm with − and +, then Save as…"

    def _note_move(self, joint: str) -> None:
        """While recording: a joint stopped. The same joint moved again straight after updates that step."""
        if self.recording is None or self.arm.feedback is None:
            return
        deg = round(math.degrees(getattr(self.arm.feedback, JOINT_KEY[joint])), 1)
        if self.recording and self.recording[-1][0] == joint:
            self.recording[-1][1] = deg
        elif len(self.recording) < TAUGHT_MAX_MOVES:
            self.recording.append([joint, deg])

    def save_taught(self, name: str, by: object) -> tuple[bool, str]:
        name = " ".join(str(name or "").lower().split())[:TAUGHT_NAME_LEN]
        if not TAUGHT_NAME.fullmatch(name):
            return self._refuse("a name of letters, digits and spaces, please")
        if name in GESTURES:
            return self._refuse(f"{name} is built in: pick another name")
        if not self.recording:
            return self._refuse("nothing recorded: press Record, then move the arm")
        if name not in self.taught and len(self.taught) >= TAUGHT_MAX:
            return self._refuse(f"at most {TAUGHT_MAX} taught gestures: forget one first")
        moves = [(j, d) for j, d in self.recording]
        self.taught[name] = [(j, d, TAUGHT_SPEED) for j, d in moves]
        self.recording, self._recording_by = None, None
        self._write_taught()
        log.info("arm: taught gesture %r saved (%d moves)", name, len(moves))
        return True, f"saved {name!r}: stow the arm, then try it"

    def forget_taught(self, name: str, by: object) -> tuple[bool, str]:
        if name not in self.taught:
            return self._refuse(f"no taught gesture {name!r}")
        del self.taught[name]
        self._write_taught()
        log.info("arm: taught gesture %r forgotten", name)
        return True, f"forgot {name!r}"

    def _load_taught(self) -> dict:
        if not self.taught_file:
            return {}
        try:
            d = json.loads(open(self.taught_file, encoding="utf-8").read())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("can't read the taught arm gestures: %s", e)
            return {}
        out = {}
        for name, moves in (d.get("gestures") or {}).items() if isinstance(d, dict) else []:
            try:
                ok = (isinstance(name, str) and TAUGHT_NAME.fullmatch(name) and name not in GESTURES
                      and 0 < len(moves) <= TAUGHT_MAX_MOVES
                      and all(j in JOINT_BOX and JOINT_BOX[j][0] <= float(deg) <= JOINT_BOX[j][1]
                              for j, deg in moves))
            except (TypeError, ValueError):
                ok = False
            if ok:
                out[name] = [(j, float(deg), TAUGHT_SPEED) for j, deg in moves]
            else:
                log.warning("taught arm gesture %r left out: not understood", name)
        return dict(list(out.items())[:TAUGHT_MAX])

    def _write_taught(self) -> None:
        if not self.taught_file:
            return
        try:
            tmp = self.taught_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"gestures": {n: [[j, d] for j, d, _ in m] for n, m in self.taught.items()}}, f)
            os.replace(tmp, self.taught_file)
        except OSError as e:
            log.warning("can't save the taught arm gestures: %s", e)

    def stow(self, by: object, kind: str = "human") -> tuple[bool, str]:
        """The gripper closed first (if it was left open), then the shoulder down, the elbow folded, the base.
        Shoulder before elbow: from some poses the elbow folding first swung the arm into the phone (the user,
        2026-11-15). Gestures still go back their own tested way (elbow first); so does the arm's boot mission."""
        bad = self._check(leaving_stow=False)
        if bad:
            return bad
        j = self.joints_deg()
        steps = []
        if j is not None and j["gripper"] < GRIP_CLOSED - GRIP_CLOSED_TOL:
            steps.append(("gripper", GRIP_CLOSED))
        if not self.stowed():
            steps += [(k, self.stow_pose[k]) for k in STOW_ORDER]
        if not steps:
            return True, "the arm is stowed"
        self._start(steps, by, kind, None)
        return True, "stowing the arm" if len(steps) > 1 else "closing the gripper"

    def hold(self, why: str) -> bool:
        """Freeze where it is: cancel the move and command exactly the angles it's at. True if that was sent."""
        was = self.moving
        jogged = self._jog[0] if self._jog else None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.by = self.by_kind = self._jog = None
        fb = self.arm.feedback
        if fb is None or not self.arm.send(joints(fb.b, fb.s, fb.e, fb.t, spd=0, acc=10)):
            return False
        if jogged:
            self._note_move(jogged)                  # a button let go: while teaching, that's a move
        if was and not jogged:
            self.why = why
            log.info("arm held: %s", why)
        return True

    def let_go(self, by: object) -> tuple[bool, str]:
        bad = self._check(leaving_stow=False)
        if bad:
            return bad
        self.hold("let go")
        if not self.arm.send(torque(False)):
            return self._refuse("the arm didn't take it")
        log.info("arm torque off (let go)")
        return True, "the arm is limp: it may drop"

    def hold_again(self, by: object) -> tuple[bool, str]:
        bad = self._check(leaving_stow=False)
        if bad:
            return bad
        if not self.arm.send(torque(True)):          # torque on locks it where it is: no jump
            return self._refuse("the arm didn't take it")
        log.info("arm torque on")
        return True, "the arm holds again"

    def set_light(self, level, by: object) -> tuple[bool, str]:
        """The gripper's LED, 0..255. Allowed with STOP on (it's only light); not while asleep."""
        if self.arm.state != "ready":
            return self._refuse("the arm isn't ready")
        if self.asleep:
            return self._refuse("the robot is asleep")
        if not (isinstance(level, (int, float)) and math.isfinite(level)):
            return self._refuse("light: a level from 0 to 255")
        level = int(min(max(level, 0), 255))
        if not self.arm.send(light(level)):
            return self._refuse("the arm didn't take it")
        self.light_level, self._light_by = level, (by if level else None)
        return True, f"light {round(level / 2.55)}%" if level else "light off"

    def _light_off(self, why: str) -> None:
        if self.light_level and self.arm.send(light(0)):
            log.info("arm light off: %s", why)
        self.light_level, self._light_by = 0, None

    # -- the robot around it --

    def set_estop(self, on: bool) -> None:
        self.estop = on
        if on:
            self.hold("STOP")

    def left(self, who: object) -> None:
        """A driving page went away: a move it started stops, and a light it switched on goes off."""
        if self.by is who and self.moving:
            self.hold("its driver left")
        if self._light_by is who:
            self._light_off("its driver left")
        if self._recording_by is who:
            self.recording, self._recording_by = None, None

    async def park(self, timeout: float = 30.0) -> None:
        """Going to sleep: stow (if it can), then torque off; torque off anyway if the stow fails."""
        self.asleep = False                           # let the stow through
        try:
            if self.arm.state == "ready" and not self.estop and not self.stowed():
                ok, _ = self.stow(self, "sleep")
                if ok and self._task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(self._task), timeout)
                    except asyncio.TimeoutError:
                        self.hold("sleep: the stow took too long")
            if self.arm.state == "ready":
                self._light_off("sleep")
                if not self.stowed():
                    log.warning("arm going limp for sleep without being stowed")
                self.hold("sleep")
                if self.arm.send(torque(False)):
                    log.info("arm parked for sleep: torque off")
        finally:
            self.asleep = True

    def wake(self) -> None:
        self.asleep = False
        if self.arm.state == "ready" and self.arm.send(torque(True)):
            log.info("arm awake: torque on (it stays where it is)")

    # -- moving --

    def _start(self, steps: list[tuple[str, float]], by: object, kind: str, speed: float | None) -> None:
        if self._task is not None:
            self._task.cancel()
        self._jog = None
        self.by, self.by_kind, self.why = by, kind, None
        spd = min(max(speed or self.speed, 1.0), MAX_SPEED_DEG)
        self._task = asyncio.create_task(self._run(steps, spd), name="arm-move")

    async def _run(self, steps: list[tuple], spd: float) -> None:
        for step in steps:
            joint, goal = step[0], step[1]
            own = step[2] if len(step) > 2 and step[2] else None      # a gesture's own speed for this step
            if not await self._one(joint, goal, min(own, MAX_SPEED_DEG) if own else spd):
                return

    async def _one(self, joint: str, goal: float, spd: float) -> bool:
        key = JOINT_KEY[joint]
        fb = self.arm.feedback
        if fb is None:
            return False
        start = math.degrees(getattr(fb, key))
        load0 = fb.loads[LOAD_INDEX[key]]
        if not self.arm.send(joint_deg(JOINT_NUM[joint], goal, spd, 10)):
            self._freeze("the arm didn't take the move")
            return False
        deadline = time.monotonic() + abs(goal - start) / spd + 4.0
        last, still_since = start, None
        while True:
            await asyncio.sleep(1 / POLL_HZ)
            if not self.arm.fresh or self.arm.state != "ready":
                self._freeze("no news from the arm")
                return False
            fb = self.arm.feedback
            now_deg = math.degrees(getattr(fb, key))
            # The gripper closing on something it holds is closed, not blocked: its grip force is set on its own.
            gripping = joint == "gripper" and goal >= GRIP_CLOSED - GRIP_CLOSED_TOL
            load = fb.loads[LOAD_INDEX[key]]
            if not gripping and abs(load) - abs(load0) > LOAD_JUMP:
                self._freeze(f"the {joint} met something", load)
                return False
            if abs(now_deg - goal) <= ARRIVED_DEG:
                return True
            if abs(now_deg - last) < 0.3:
                still_since = still_since or time.monotonic()
                if time.monotonic() - still_since > SETTLED_S and (gripping or abs(now_deg - goal) <= SETTLED_DEG):
                    return True
            else:
                still_since = None
            last = now_deg
            if time.monotonic() > deadline:
                self._freeze(f"the {joint} didn't get there")
                return False

    def _freeze(self, why: str, load: int | None = None) -> None:
        fb = self.arm.feedback
        if fb is not None:
            self.arm.send(joints(fb.b, fb.s, fb.e, fb.t, spd=0, acc=10))
        self.why = why
        log.warning("arm frozen: %s%s", why, f" (load {load})" if load is not None else "")

    def snapshot(self) -> dict:
        s = self.arm.snapshot()
        return {"present": s["present"], "state": self.state(), "by": self.by_kind if self.moving else None,
                "joints": self.joints_deg(), "loads": s["loads"], "torque": s["torque"], "volts": s["volts"],
                "why": self.why, "drive": self.drive_limit(), "light": self.light_level,
                "gestures": self.gestures(), "taught": sorted(self.taught),
                "recording": None if self.recording is None else len(self.recording)}
