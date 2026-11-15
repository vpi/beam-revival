"""A simulated RoArm-M2-S on a pseudo-terminal, behaving like its firmware as read and measured (docs/arm.md).

The server opens the pty path exactly as it would the arm's /dev/serial/by-id name. Like the real arm it:
- starts with debug text on (InfoPrint 1): boot chatter, its start move (moveInit: base 0, shoulder 0, elbow 90
  degrees, gripper closed), then plays its boot mission, listing it first; **any byte arriving while the mission
  plays aborts the rest** ("[missionPlay abort.]"), as serialMissionAbort() does;
- echoes commands while InfoPrint is 1, drops bad JSON without a word;
- answers {"T":105} with a {"T":1051,...} line; moves joints toward their goals at the commanded speed, clamped
  to the firmware's limits;
- on {"T":0} goes limp for 10 s and reads nothing meanwhile (so a test can prove it's never sent).
Switches for tests: silent, garbage bytes, a wrong device (ultrasonic frames), slow replies.

Run standalone:  python3 -m beamctl.armsim --link /tmp/beam-arm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import time
import tty

log = logging.getLogger("beam.armsim")

LIMITS = {"b": (-math.pi, math.pi), "s": (-math.pi / 2, math.pi / 2), "e": (-math.pi / 4, math.pi),
          "t": (1.08, 3.14)}
JOINT_KEYS = {1: "b", 2: "s", 3: "e", 4: "t"}
START = {"b": 0.0, "s": 0.0, "e": math.pi / 2, "t": 3.14}      # moveInit
MAX_RAD_S = math.radians(180)
STEPS_PER_RAD = 4096 / (2 * math.pi)
INIT_RAD_S = 600 / STEPS_PER_RAD                             # moveInit's 600 steps/s
TICK_S = 0.02
# The radio steps every arm of ours carries; tests add the stow moves.
RADIO_MISSION = [{"T": 301, "mode": 0}, {"T": 300, "mode": 0, "mac": "00:00:00:00:00:00"}]


class SimulatedArm:
    def __init__(self, link: str | None = None, *, mission: list[dict] | None = None, boot_s: float = 0.5,
                 time_scale: float = 1.0, silent: bool = False, garbage: bool = False, wrong: bool = False,
                 reply_delay_s: float = 0.0, move_scale: float = 1.0, seed: int | None = None):
        """boot_s: how long its boot chatter takes before the start move. time_scale < 1 shortens the mission's
        waits and the 10 s of {"T":0}, and move_scale > 1 speeds every move up, so tests needn't wait."""
        self.mission = list(RADIO_MISSION if mission is None else mission)
        self.boot_s, self.time_scale = boot_s, time_scale
        self.silent, self.garbage, self.wrong, self.reply_delay_s = silent, garbage, wrong, reply_delay_s
        self.move_scale = move_scale
        self.rng = random.Random(seed)
        self._master, slave = os.openpty()
        tty.setraw(slave)
        self.slave_path = os.ttyname(slave)
        self._slave = slave                      # kept open so the pty stays up while the server reconnects
        self.link = link
        if link:
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(self.slave_path, link)
        self.path = link or self.slave_path
        self.pos = dict(START)
        self.goal = dict(START)
        self.speed = {k: MAX_RAD_S for k in START}      # rad/s toward the goal
        self.torque = {k: True for k in START}
        self.info_print = 1
        self.led = 0                             # the gripper's LED, 0..255
        self.espnow_mode = 3                     # follower: the firmware's default
        self.received: list[dict] = []           # every command it read, in order
        self.t0_received = 0                     # times {"T":0} came (must stay 0)
        self.aborted = False                     # a byte came while the boot mission played
        self.mission_done = False
        self.boots = 0
        self._buf = b""
        self._deaf_until = 0.0
        self._in_mission = False
        self._pending: list[dict] = []
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- life --

    async def start(self, boot: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self._master, self._readable)
        self._task = asyncio.create_task(self._run(boot), name="armsim")

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.remove_reader(self._master)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.link and os.path.islink(self.link):
            os.unlink(self.link)

    def reboot(self) -> None:
        """As after {"T":600} or a power cycle with the port kept: chatter, start move, mission."""
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._run(True), name="armsim")

    # -- out --

    def _say(self, text: str) -> None:
        if self.silent:
            return
        try:
            os.write(self._master, (text + "\n").encode())
        except OSError:
            pass

    def _feedback(self) -> str:
        p = self.pos
        x = 236.82 * math.cos(p["s"]) + 280.15 * math.sin(p["e"] + p["s"])   # rough, for show
        load = {k: (0 if not self.torque[k] else 50 if abs(self.goal[k] - p[k]) > 0.01 else 35) for k in p}
        return json.dumps({"T": 1051, "x": round(x * math.cos(p["b"]), 2), "y": round(x * math.sin(p["b"]), 2),
                           "z": round(126.06 - 236.82 * math.sin(p["s"]), 2),
                           "b": round(p["b"], 6), "s": round(p["s"], 6), "e": round(p["e"], 6), "t": round(p["t"], 6),
                           "torB": load["b"], "torS": load["s"], "torE": load["e"], "torH": load["t"],
                           "torswitchB": int(self.torque["b"]), "torswitchS": int(self.torque["s"]),
                           "torswitchE": int(self.torque["e"]), "torswitchH": int(self.torque["t"]), "v": 1232},
                          separators=(",", ":"))

    # -- in --

    def _readable(self) -> None:
        try:
            data = os.read(self._master, 4096)
        except OSError:
            return
        if time.monotonic() < self._deaf_until:
            return                                # blocked in its "emergency stop" delay
        if self._in_mission:
            self.aborted = True                   # serialMissionAbort(): Serial.available()
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            try:
                cmd = json.loads(raw)
            except ValueError:
                continue                          # dropped, like the firmware
            if isinstance(cmd, dict):
                self.received.append(cmd)
                if self._in_mission:
                    self._pending.append(cmd)     # the mission stops; the command waits in the buffer
                    continue
                self._handle(cmd)

    def _handle(self, cmd: dict) -> None:
        T = cmd.get("T")
        if self.info_print == 1:
            self._say(json.dumps(cmd, separators=(",", ":")))
        if self.wrong:
            return
        if T == 0:
            self.t0_received += 1
            for k in self.torque:
                self.torque[k] = False
            self._deaf_until = time.monotonic() + 10 * self.time_scale
            asyncio.get_running_loop().call_later(10 * self.time_scale, self._torque_back)
        elif T == 105:
            if self.reply_delay_s:
                asyncio.get_running_loop().call_later(self.reply_delay_s, lambda: self._say(self._feedback()))
            else:
                self._say(self._feedback())
        elif T == 102:
            spd = cmd.get("spd", 0)
            for k, key in (("b", "base"), ("s", "shoulder"), ("e", "elbow"), ("t", "hand")):
                if key in cmd:
                    self._goal(k, float(cmd[key]), spd / STEPS_PER_RAD if spd else MAX_RAD_S)
        elif T == 101 and cmd.get("joint") in JOINT_KEYS:
            spd = cmd.get("spd", 0)
            self._goal(JOINT_KEYS[cmd["joint"]], float(cmd.get("rad", 0)), spd / STEPS_PER_RAD if spd else MAX_RAD_S)
        elif T == 121 and cmd.get("joint") in JOINT_KEYS:
            spd = cmd.get("spd", 0)
            self._goal(JOINT_KEYS[cmd["joint"]], math.radians(float(cmd.get("angle", 0))),
                       math.radians(spd) if spd else MAX_RAD_S)
        elif T == 122:
            spd = cmd.get("spd", 0)
            for k, key in (("b", "b"), ("s", "s"), ("e", "e"), ("t", "h")):
                if key in cmd:
                    self._goal(k, math.radians(float(cmd[key])), math.radians(spd) if spd else MAX_RAD_S)
        elif T == 106:
            self._goal("t", float(cmd.get("cmd", 3.14)), MAX_RAD_S)
        elif T == 210:
            on = bool(cmd.get("cmd"))
            for k in self.torque:
                self.torque[k] = on
            if on:
                self.goal = dict(self.pos)        # torque on locks it where it is
        elif T == 605:
            self.info_print = int(cmd.get("cmd", 1))
        elif T == 114:
            self.led = int(cmd.get("led", 0))
        elif T == 301:
            self.espnow_mode = int(cmd.get("mode", 3))
        elif T == 405:
            self._say('{"ip":"192.168.4.1","rssi":0,"wifi_mode_on_boot":3}')
        elif T == 600:
            self.reboot()
        elif T == 602:
            self._list_mission()
        elif T == 222:
            try:
                self.mission.append(json.loads(cmd["step"]))
            except (KeyError, ValueError):
                pass

    def _goal(self, k: str, rad: float, speed: float) -> None:
        if not self.torque[k]:
            return
        lo, hi = LIMITS[k]
        self.goal[k] = min(max(rad, lo), hi)
        self.speed[k] = max(min(speed, MAX_RAD_S), 1e-3)

    def _torque_back(self) -> None:
        for k in self.torque:
            self.torque[k] = True
        self.goal = dict(self.pos)

    def _list_mission(self) -> None:
        self._say("---=== File Content ===---")
        self._say("reading file: [boot] starts:")
        self._say('{"name":"boot","intro":"these cmds run automatically at boot."}')
        for i, step in enumerate(self.mission, 1):
            self._say(f"[StepNum: {i} ] - {json.dumps(step, separators=(',', ':'))}")
        self._say("^^^ ^^^ ^^^ reading file: boot.mission ends. ^^^ ^^^ ^^^")

    # -- time --

    def _physics(self, dt: float) -> None:
        for k in self.pos:
            d = self.goal[k] - self.pos[k]
            step = self.speed[k] * self.move_scale * dt
            self.pos[k] = self.goal[k] if abs(d) <= step else self.pos[k] + math.copysign(step, d)

    async def _settle(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end and any(abs(self.goal[k] - self.pos[k]) > 1e-3 for k in self.pos):
            await asyncio.sleep(TICK_S)

    async def _run(self, boot: bool) -> None:
        physics = asyncio.create_task(self._tick())
        try:
            if boot:
                await self._boot()
            while True:
                if self.garbage:
                    try:
                        os.write(self._master, bytes(self.rng.randrange(256) for _ in range(20)))
                    except OSError:
                        pass
                if self.wrong:
                    d = self.rng.randrange(300, 2000)
                    try:
                        os.write(self._master, bytes([0xFF, d >> 8, d & 0xFF, (0xFF + (d >> 8) + (d & 0xFF)) & 0xFF]))
                    except OSError:
                        pass
                await asyncio.sleep(0.1)
        finally:
            physics.cancel()

    async def _tick(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            self._physics(now - last)
            last = now

    async def _boot(self) -> None:
        self.boots += 1
        self.info_print, self.espnow_mode, self.aborted, self.mission_done = 1, 3, False, False
        self._deaf_until = 0.0
        for k in self.torque:
            self.torque[k] = True
        self._say("RoArm-M2 starting")
        await asyncio.sleep(self.boot_s)
        self._say("Power up the servos.")
        for k in START:                                   # moveInit
            self.goal[k], self.speed[k] = START[k], INIT_RAD_S
        await self._settle(10)
        self._list_mission()
        self._say("---")
        self._say("[currentTimes: 1 ]")
        self._in_mission = True
        try:
            for step in self.mission:
                if self.aborted:
                    self._say("[missionPlay abort.]")
                    return
                self._say("[mission name]: boot")
                if step.get("T") == 111:                  # a wait
                    await asyncio.sleep(step.get("cmd", 0) / 1000 * self.time_scale)
                    continue
                self._say(f"[cmd]: {json.dumps(step, separators=(',', ':'))}")
                self._handle_mission(step)
                await self._settle(30)
            if self.aborted:
                self._say("[missionPlay abort.]")
                return
            self.mission_done = True
            self._say("[missionPlay finished.]")
        finally:
            self._in_mission = False
            # Commands that came during the mission waited in the buffer: its main loop reads them now.
            pending, self._pending = self._pending, []
            for cmd in pending:
                self._handle(cmd)

    def _handle_mission(self, step: dict) -> None:
        info, self.info_print = self.info_print, 0       # a mission step isn't echoed again
        try:
            self._handle(step)
        finally:
            self.info_print = info


async def _run(args: argparse.Namespace) -> None:
    mission = list(RADIO_MISSION)
    if args.stow:
        mission += [{"T": 121, "joint": 3, "angle": 180, "spd": 30, "acc": 10}, {"T": 111, "cmd": 4000},
                    {"T": 121, "joint": 1, "angle": 94, "spd": 20, "acc": 10}, {"T": 111, "cmd": 5500},
                    {"T": 121, "joint": 2, "angle": -90, "spd": 20, "acc": 10}]
    arm = SimulatedArm(args.link, mission=mission)
    await arm.start()
    print(f"simulated arm on {arm.path}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await arm.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated RoArm-M2-S on a pty")
    ap.add_argument("--link", default="/tmp/beam-arm", help="symlink to the pty (default /tmp/beam-arm)")
    ap.add_argument("--stow", action="store_true", help="boot mission with our stow moves, as on the robot")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
