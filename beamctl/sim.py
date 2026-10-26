"""Simulated Beam+ motor board on a pseudo-terminal.

The motor server connects to the pty path exactly as it would to /dev/ttyACM0.
The simulator speaks the real protocol (docs/motor-protocol.md), models a
differential-drive base with the Beam+ limits, and acts out the failure modes
the server must survive: the delayed power-cycle "watchdog", corrupted bytes,
and a board that stops answering.

Run standalone:  python3 -m beamctl.sim --link /tmp/beam-sim
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import time
import tty
from dataclasses import dataclass, field

from . import protocol as p

log = logging.getLogger("beam.sim")

# Beam+ ("cleveland") limits from Communicator::getLimits: linear 1.0, angular 1.0.
LIMITER_TAG_MAX_AGE = 2000 << 32           # ~1 s of limiter clock (the real limit is unknown)
LIMITER_CLOCK_START = 729_000_000
BEAMPLUS_LINEAR_LIMIT = 1.0
BEAMPLUS_ANGULAR_LIMIT = 1.0
LINEAR_ACCEL = 1.0        # m/s^2, assumed board-side ramp
ANGULAR_ACCEL = 2.0       # rad/s^2
PHYSICS_HZ = 100


@dataclass
class PowerEvent:
    at: float
    kind: int


@dataclass
class SimStats:
    commands: int = 0
    velocity_commands: int = 0
    bad_packets: int = 0
    power_commands: list[tuple[float, int, int]] = field(default_factory=list)   # (time, delay, kind)
    power_events: list[PowerEvent] = field(default_factory=list)
    last_velocity_cmd: tuple[float, float] = (0.0, 0.0)
    last_tag: int = 0


class SimulatedBoard:
    def __init__(self, link: str | None = None, *, corrupt_rate: float = 0.0, drop_rate: float = 0.0,
                 reboot_seconds: float = 3.0, seed: int | None = None, time_scale: float = 1.0,
                 world=None, start: tuple[float, float, float] | None = None):
        """time_scale only speeds up the power-cycle countdown, so tests needn't wait 300 s.

        world: a beamctl.world.World; walls and furniture then block the robot, and camera_jpeg()
        renders what its head camera would see.
        """
        self.world = world
        self.bumps = 0                      # times the robot ran into something
        self.trail: list[tuple[float, float]] = []
        self.link = link
        self.corrupt_rate = corrupt_rate
        self.drop_rate = drop_rate
        self.reboot_seconds = reboot_seconds
        self.time_scale = time_scale
        self.rng = random.Random(seed)
        self.stats = SimStats()

        self.unresponsive = False          # tests can flip this to simulate a hung board
        self.docked = False

        self._master: int | None = None
        self.slave_path: str | None = None
        self._rx = bytearray()
        self._t0 = time.monotonic()
        self._powered = True
        self._pending_power: PowerEvent | None = None
        self._tasks: list[asyncio.Task] = []

        # Robot state
        self.cmd_linear = 0.0
        self.cmd_angular = 0.0
        self.linear = 0.0
        self.angular = 0.0
        self.x = self.y = self.heading = 0.0
        if start is not None:
            self.x, self.y, self.heading = start
        self._blocked = False
        self.enc_l = 0.0
        self.enc_r = 0.0
        self.capacity_max = 100.0
        self.capacity = 80.0
        self.tag = 0
        self.timed_out = True
        self.light: int | None = None     # last base light colour set (None = never)

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> str:
        self._master, slave = os.openpty()
        tty.setraw(slave)
        self.slave_path = os.ttyname(slave)
        # Keep our own slave fd open so the master never sees EIO between server reconnects.
        self._slave_keepalive = slave
        os.set_blocking(self._master, False)
        loop = asyncio.get_running_loop()
        loop.add_reader(self._master, self._readable)
        if self.link:
            try:
                os.unlink(self.link)
            except FileNotFoundError:
                pass
            os.symlink(self.slave_path, self.link)
        self._tasks.append(asyncio.create_task(self._physics()))
        log.info("simulated motor board on %s%s", self.slave_path, f" (link {self.link})" if self.link else "")
        return self.link or self.slave_path

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._master is not None:
            asyncio.get_running_loop().remove_reader(self._master)
            os.close(self._master)
            os.close(self._slave_keepalive)
            self._master = None
        if self.link:
            try:
                os.unlink(self.link)
            except FileNotFoundError:
                pass

    # --- clocks --------------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self._t0

    def limiter_clock(self) -> int:
        # The real Beam+ board counts ~2000 per second in the upper 32 bits of its limiter clock, and that
        # count was already ~730 million on ours (it doesn't restart at power-on).
        return (LIMITER_CLOCK_START + int(self.now() * 2000)) << 32

    @property
    def powered(self) -> bool:
        return self._powered

    # --- receive -------------------------------------------------------------

    def _readable(self) -> None:
        try:
            data = os.read(self._master, 4096)
        except (BlockingIOError, OSError):
            return
        if not self._powered or self.unresponsive:
            return                       # a powered-off / hung board drops everything
        self._rx += data
        magic = bytes([0xAA, 0xAA, 0x55, 0x55])
        while True:
            i = self._rx.find(magic)
            if i < 0:
                del self._rx[:-3]
                return
            if i:
                del self._rx[:i]
            if len(self._rx) < p.CMD_SIZE:
                return
            pkt = bytes(self._rx[:p.CMD_SIZE])
            try:
                cmd = p.decode_command(pkt)
            except ValueError:
                self.stats.bad_packets += 1
                del self._rx[0]
                continue
            del self._rx[:p.CMD_SIZE]
            self._handle(cmd)

    def _handle(self, cmd: p.Command) -> None:
        self.stats.commands += 1
        if cmd.code == p.CMD_VELOCITY:
            self.stats.velocity_commands += 1
            # Like the real board: only a recent value of our limiter clock counts as a fresh command.
            age = self.limiter_clock() - cmd.tag
            self.timed_out = not 0 <= age <= LIMITER_TAG_MAX_AGE
            if self.timed_out:
                self.cmd_linear = self.cmd_angular = 0.0
            else:
                self.cmd_linear = p.from_fixed(cmd.a)
                self.cmd_angular = p.from_fixed(cmd.b)
            self.tag = cmd.tag
            self.stats.last_velocity_cmd = (self.cmd_linear, self.cmd_angular)
            self.stats.last_tag = cmd.tag
            self._reply(p.encode_status(self.status()))
        elif cmd.code == p.CMD_GET_STATUS:
            if cmd.arg0 == 0x10000:
                self._reply(_blank_reply(p.CONTROLLER_STATUS_MAGIC, 128))
            else:
                self._reply(p.encode_status(self.status()))
        elif cmd.code == p.CMD_GET_DOCK_ID:
            self._reply(_blank_reply(p.DOCK_ID_MAGIC, 64))
        elif cmd.code == p.CMD_POWER:
            delay, kind = cmd.a, cmd.b
            self.stats.power_commands.append((self.now(), delay, kind))
            # A new power command replaces any pending one.
            self._pending_power = PowerEvent(self.now() + delay / self.time_scale, kind)
        elif cmd.code == p.CMD_CHARGING_COLOR:
            self.light = cmd.a & 0xFFFF   # the base light; fire-and-forget on the real board too
        elif cmd.code == p.CMD_GYRO_MODE:
            pass
        else:
            log.warning("unknown command code %#x", cmd.code)

    def _reply(self, pkt: bytes) -> None:
        if self.rng.random() < self.drop_rate:
            return
        if self.rng.random() < self.corrupt_rate:
            b = bytearray(pkt)
            b[self.rng.randrange(4, len(b))] ^= 1 << self.rng.randrange(8)
            pkt = bytes(b)
        try:
            os.write(self._master, pkt)
        except OSError:
            pass

    # --- model ---------------------------------------------------------------

    def status(self) -> p.Status:
        flags = 0
        if self._powered:
            flags |= p.FLAG_MOTORS_ENABLED
        if self.docked:
            flags |= p.FLAG_CHARGER_DETECTED | p.FLAG_CHARGING
        lim_lin = _clamp(self.cmd_linear, BEAMPLUS_LINEAR_LIMIT)
        lim_ang = _clamp(self.cmd_angular, BEAMPLUS_ANGULAR_LIMIT)
        frac = self.capacity / self.capacity_max
        return p.Status(
            flags=flags,
            battery_voltage=22.0 + 3.2 * frac,
            battery_current=(2.0 if self.docked else -0.4 - abs(self.linear) * 1.5),
            charge_voltage=29.0 if self.docked else 0.0,
            charge_current=2.0 if self.docked else 0.0,
            capacity=self.capacity, capacity_max=self.capacity_max,
            timestamp_us=int(self.now() * 1e6) & 0xFFFFFFFF,
            encoder_l=int(self.enc_l), encoder_r=int(self.enc_r),
            limiter_tag=self.limiter_clock(),
            actual_linear=self.linear, actual_angular=self.angular,
            limited_linear=lim_lin, limited_angular=lim_ang,
            linear_limit=BEAMPLUS_LINEAR_LIMIT, angular_limit=BEAMPLUS_ANGULAR_LIMIT,
            integrated_yaw=int((self.heading % (2 * math.pi)) / (2 * math.pi) * (1 << 24)) & 0xFFFFFF,
        )

    async def _physics(self) -> None:
        dt = 1.0 / PHYSICS_HZ
        while True:
            await asyncio.sleep(dt)
            self._power_tick()
            if not self._powered:
                self.cmd_linear = self.cmd_angular = 0.0
            tgt_lin = _clamp(self.cmd_linear, BEAMPLUS_LINEAR_LIMIT)
            tgt_ang = _clamp(self.cmd_angular, BEAMPLUS_ANGULAR_LIMIT)
            self.linear = _approach(self.linear, tgt_lin, LINEAR_ACCEL * dt)
            self.angular = _approach(self.angular, tgt_ang, ANGULAR_ACCEL * dt)
            d = self.linear * dt
            dth = self.angular * dt
            nx, ny = self.x + d * math.cos(self.heading), self.y + d * math.sin(self.heading)
            if self.world is not None and d and self.world.collides(nx, ny):
                # Bumped into a wall or furniture: the wheels stall (no motion, no encoder counts).
                if not self._blocked:
                    self.bumps += 1
                    log.info("bump at (%.2f, %.2f)", self.x, self.y)
                self._blocked = True
                self.linear, d = 0.0, 0.0
            else:
                self._blocked = False
                self.x, self.y = nx, ny
            self.heading += dth
            if self.world is not None and (not self.trail or math.hypot(self.x - self.trail[-1][0], self.y - self.trail[-1][1]) > 0.1):
                self.trail.append((self.x, self.y))
            # Invert Beam's decoding: lin = (L-R)*k1, ang = -(L+R)*k2
            a = d / p.ENCODER_LINEAR_SCALE
            b = -dth / p.ENCODER_ANGULAR_SCALE
            self.enc_l += (a + b) / 2
            self.enc_r += (b - a) / 2
            # % per second: idle ~2 days to empty, driving ~2-3 hours (rough Beam+ figures).
            drain = (0.0005 + abs(self.linear) * 0.015) * dt
            self.capacity = min(self.capacity_max, self.capacity + (0.05 * dt if self.docked else -drain))

    def camera_jpeg(self, cam: str = "head") -> bytes:
        """The head (or "nav" floor) camera's view of the virtual world from the current pose."""
        return self.world.camera_jpeg(self.x, self.y, self.heading, cam=cam)

    def pose(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3), "heading_deg": round(math.degrees(self.heading) % 360, 1),
                "room": self.world.room_at(self.x, self.y) if self.world else None, "bumps": self.bumps}

    def _power_tick(self) -> None:
        ev = self._pending_power
        if ev is None or self.now() < ev.at:
            return
        self._pending_power = None
        self.stats.power_events.append(PowerEvent(self.now(), ev.kind))
        kinds = {p.POWER_CYCLE: "POWER CYCLE", p.POWER_OFF_UNTIL_CHARGE: "POWER OFF UNTIL CHARGE",
                 p.POWER_OFF: "POWER OFF"}
        log.warning("board executed %s (the host stopped refreshing the watchdog)", kinds.get(ev.kind, ev.kind))
        self._powered = False
        self.linear = self.angular = 0.0
        if ev.kind == p.POWER_CYCLE:
            asyncio.get_running_loop().call_later(self.reboot_seconds, self._power_on)

    def _power_on(self) -> None:
        log.info("board powered back on")
        self._powered = True


def _blank_reply(magic: int, size: int) -> bytes:
    body = magic.to_bytes(4, "little") + bytes(size - 8)
    return body + p.crc32_stm32(body).to_bytes(4, "little")


def _clamp(v: float, lim: float) -> float:
    return max(-lim, min(lim, v))


def _approach(cur: float, tgt: float, step: float) -> float:
    if cur < tgt:
        return min(tgt, cur + step)
    return max(tgt, cur - step)


async def serve_world(board: SimulatedBoard, port: int):
    """Local HTTP endpoints for the virtual world: /camera.jpg, /map.png, /pose, /reset."""
    from aiohttp import web
    from .world import START

    async def camera(_):
        return web.Response(body=board.camera_jpeg(), content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def nav(_):
        return web.Response(body=board.camera_jpeg("nav"), content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def map_png(_):
        import cv2
        img = board.world.map_image(board.x, board.y, board.heading, trail=board.trail, scale=30)
        return web.Response(body=cv2.imencode(".png", img)[1].tobytes(), content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    async def pose(_):
        return web.json_response(board.pose())

    async def reset(request):
        q = request.query
        board.x = float(q.get("x", START[0]))
        board.y = float(q.get("y", START[1]))
        board.heading = math.radians(float(q.get("heading_deg", math.degrees(START[2]))))
        board.trail.clear()
        board.bumps = 0
        return web.json_response(board.pose())

    app = web.Application()
    app.router.add_get("/camera.jpg", camera)
    app.router.add_get("/nav.jpg", nav)
    app.router.add_get("/map.png", map_png)
    app.router.add_get("/pose", pose)
    app.router.add_get("/reset", reset)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("virtual world camera on http://127.0.0.1:%d/camera.jpg", port)
    return runner


async def _run(args: argparse.Namespace) -> None:
    world = None
    if args.world:
        from .world import START, World
        world = World()
    board = SimulatedBoard(args.link, corrupt_rate=args.corrupt_rate, drop_rate=args.drop_rate,
                           time_scale=args.time_scale, world=world, start=START if world else None)
    path = await board.start()
    print(f"Simulated Beam+ motor board ready: {path}", flush=True)
    if world is not None:
        await serve_world(board, args.world_port)
    try:
        while True:
            await asyncio.sleep(2)
            log.info("pos=(%.2f, %.2f) heading=%.0f deg  v=%.2f w=%.2f  battery=%.1f%%  %s",
                     board.x, board.y, math.degrees(board.heading) % 360, board.linear, board.angular,
                     100 * board.capacity / board.capacity_max, "" if board.powered else "POWERED OFF")
    finally:
        await board.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated Beam+ motor board on a pty")
    ap.add_argument("--link", default="/tmp/beam-sim", help="symlink to create for the pty (default %(default)s)")
    ap.add_argument("--corrupt-rate", type=float, default=0.0, help="fraction of replies with a flipped bit")
    ap.add_argument("--drop-rate", type=float, default=0.0, help="fraction of replies never sent")
    ap.add_argument("--time-scale", type=float, default=1.0, help="speed up the power-cycle countdown")
    ap.add_argument("--world", action="store_true", help="put the robot in the virtual house (walls, camera)")
    ap.add_argument("--world-port", type=int, default=8091, help="local port for /camera.jpg etc. (with --world)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
