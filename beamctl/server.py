"""Beam+ motor server: owns the motor board, serves the driving page.

Safety rules, in order of precedence:
  1. E-stop (latched until released by the driver).
  2. Dead-man: no drive message for `deadman_s` -> target speed 0.
  3. Speed limits and acceleration ramps (server side, on top of the board's own).
  4. Board link loss -> stop commanding, reopen the port, and keep the power watchdog refreshed.

The board's "watchdog" is a delayed power-cycle order that Beam re-sent every 5 s
(see docs/motor-protocol.md section 6). We must keep refreshing it from the moment
the port opens, or the base power-cycles the robot ~5 minutes after boot.

Run:  python3 -m beamctl.server --device /dev/ttyACM0
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import logging
import math
import os
import secrets
import struct
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web

from . import protocol as p
from .serialport import SerialPort
from .signaling import MediaHub
from .sleep import Backlight, SleepManager

log = logging.getLogger("beam.server")
WEB_DIR = Path(__file__).parent / "web"
try:        # written by build-image.sh / deploy-robot.sh: which release this robot runs
    VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
except OSError:
    VERSION = "development"


@dataclass
class Config:
    device: str = "/dev/ttyACM0"
    command_interval_s: float = 0.010          # Beam: config/st001/command_interval = 10 ms
    reply_timeout_s: float = 0.100             # Beam: 100 ms per velocity command
    watchdog_refresh_s: float = 5.0            # Beam: config/st001/watchdog_refresh_interval
    watchdog_delay_s: int = 300                # Beam: config/st001/watchdog_delay
    no_comm_reset_s: float = 2.0               # Beam: config/st001/no_comm_reset_timeout
    reopen_interval_s: float = 1.0
    deadman_s: float = 0.5                     # Beam used 5 s; much too long for a home
    max_linear: float = 0.6                    # m/s (Beam+ board limit is 1.0)
    max_angular: float = 1.0                   # rad/s
    linear_accel: float = 0.8                  # m/s^2
    linear_decel: float = 1.5                  # m/s^2 (stopping is allowed to be quicker)
    angular_accel: float = 2.5                 # rad/s^2
    tag_mode: str = "limiter"                  # limiter | board_time | zero | counter  (see docs section 7)
    gyro_mode: bool = True
    telemetry_hz: float = 10.0
    token: str | None = None
    # Shared secret for the "brain" (face recognition etc. on another machine). None = brain disabled.
    brain_key: str | None = None
    brain_fps: float = 3.0              # camera frames per second sent to the brain
    # Written by the robot image's setup scripts (Tailscale login link, addresses); shown on the robot screen.
    setup_file: str = "/run/beam/setup.json"
    # Sleep mode (beamctl/sleep.py): nightly window like "23:00-07:00" in local time, and the screen backlight
    # to switch off ("auto" = the first one in /sys/class/backlight; None = leave the screen alone).
    sleep_hours: str | None = None
    backlight: str | None = None
    sleep_state: str | None = None             # file that remembers sleep across restarts
    # The light in the base that shows charging: the board's "charging colour" (command 0x19), set on connect.
    # 0x0F00 is the green Beam showed (the board's colour values aren't plain RGB); 0 turns it off.
    light_color: int = 0x0F00


PRIORITY_BRAIN = 0          # autonomy: drives only while no human is driving
PRIORITY_HUMAN = 1          # a person on a driving page always wins


@dataclass
class DriveState:
    target_linear: float = 0.0
    target_angular: float = 0.0
    last_input: float = 0.0
    owner: object | None = None
    owner_priority: int = PRIORITY_HUMAN
    estop: bool = False


@dataclass
class LinkState:
    connected: bool = False
    last_reply: float = 0.0
    replies: int = 0
    timeouts: int = 0
    reconnects: int = 0
    last_error: str | None = None
    watchdog_last_refresh: float = 0.0
    status: p.Status | None = None
    commanded_linear: float = 0.0
    commanded_angular: float = 0.0


class MotorController:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.drive = DriveState()
        self.link = LinkState()
        self._reader = p.PacketReader()
        self._replies: asyncio.Queue[p.Status] = asyncio.Queue()
        self._port = SerialPort(cfg.device, self._on_data, self._on_lost)
        self._counter = 0
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._light_pending = True               # (re)send the charging light colour at the next cycle
        self.sleep = SleepManager(cfg.sleep_hours, Backlight(cfg.backlight), cfg.sleep_state)

    # --- driver input ----------------------------------------------------------

    def set_target(self, linear: float, angular: float, owner: object, priority: int = PRIORITY_HUMAN) -> bool:
        """Returns False if another client currently holds control.

        A higher-priority client (a human) takes over immediately from a lower one (the brain);
        between equals, whoever is actively driving keeps control until their dead-man lapses.
        """
        if self.sleep.asleep:
            return False
        now = time.monotonic()
        d = self.drive
        active = d.owner is not None and d.owner is not owner and now - d.last_input < self.cfg.deadman_s
        if active and priority <= d.owner_priority:
            return False
        if not (math.isfinite(linear) and math.isfinite(angular)):
            linear = angular = 0.0
        d.owner = owner
        d.owner_priority = priority
        d.target_linear = _clamp(linear, self.cfg.max_linear)
        d.target_angular = _clamp(angular, self.cfg.max_angular)
        d.last_input = now
        return True

    def release(self, owner: object) -> None:
        if self.drive.owner is owner:
            self.drive.owner = None
            self.drive.target_linear = self.drive.target_angular = 0.0

    def set_estop(self, on: bool) -> None:
        self.drive.estop = on
        if on:
            self.drive.target_linear = self.drive.target_angular = 0.0
            log.warning("E-STOP engaged")
        else:
            log.info("E-stop released")

    def go_to_sleep(self, reason: str) -> None:
        self.drive.owner = None
        self.drive.target_linear = self.drive.target_angular = 0.0
        self.sleep.sleep(reason)

    def wake_up(self, reason: str) -> None:
        self.sleep.wake(reason)

    def _effective_target(self, now: float) -> tuple[float, float]:
        d = self.drive
        if d.estop or now - d.last_input > self.cfg.deadman_s:
            return 0.0, 0.0
        return d.target_linear, d.target_angular

    # --- serial callbacks ------------------------------------------------------

    def _on_data(self, data: bytes) -> None:
        for magic, pkt in self._reader.feed(data):
            if magic == p.STATUS_MAGIC:
                try:
                    self._replies.put_nowait(p.decode_status(pkt))
                except ValueError as e:
                    self.link.last_error = str(e)

    def _on_lost(self, exc: Exception) -> None:
        log.error("motor board link lost: %s", exc)
        self.link.connected = False
        self.link.last_error = str(exc)
        self.link.commanded_linear = self.link.commanded_angular = 0.0

    # --- main loop -------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="motor-loop")

    async def stop(self) -> None:
        self._stopping = True
        if self._port.is_open:
            # Leave the board stopped. The pending power-cycle stays armed on purpose:
            # if nothing takes over, the robot reboots itself (Beam's own recovery behaviour).
            for _ in range(3):
                try:
                    self._port.write(p.velocity(0.0, 0.0, self._tag()))
                except OSError:
                    break
                await asyncio.sleep(self.cfg.command_interval_s)
            self._port.close()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopping:
            if not self._port.is_open:
                if not await self._open():
                    await asyncio.sleep(self.cfg.reopen_interval_s)
                    continue
            await self._cycle()

    async def _open(self) -> bool:
        try:
            self._port.open()
        except OSError as e:
            if self.link.last_error != str(e):
                log.warning("cannot open %s: %s", self.cfg.device, e)
            self.link.last_error = str(e)
            return False
        log.info("opened motor board at %s", self.cfg.device)
        self.link.reconnects += 1
        self.link.last_reply = time.monotonic()        # grace period before no-comm reset
        self._reader = p.PacketReader()
        self._drain_replies()
        self._send_watchdog()                          # first thing, always
        self._send(p.gyro_mode(self.cfg.gyro_mode))
        self._light_pending = True                     # a reconnected board may have reset its light
        return True

    async def _cycle(self) -> None:
        cfg, link = self.cfg, self.link
        t0 = time.monotonic()

        if t0 - link.watchdog_last_refresh >= cfg.watchdog_refresh_s:
            self._send_watchdog()

        dt = cfg.command_interval_s
        tgt_lin, tgt_ang = self._effective_target(t0)
        lin = _ramp(link.commanded_linear, tgt_lin, cfg.linear_accel * dt, cfg.linear_decel * dt)
        ang = _ramp(link.commanded_angular, tgt_ang, cfg.angular_accel * dt, cfg.angular_accel * dt)

        if self._light_pending and self._send(p.charging_color(cfg.light_color)):
            self._light_pending = False
        self._drain_replies()                          # Beam flushes input before every command
        if self._send(p.velocity(lin, ang, self._tag())):
            link.commanded_linear, link.commanded_angular = lin, ang
            try:
                status = await asyncio.wait_for(self._replies.get(), cfg.reply_timeout_s)
            except asyncio.TimeoutError:
                link.timeouts += 1
            else:
                link.status = status
                link.replies += 1
                link.last_reply = time.monotonic()
                link.connected = True

        if self._port.is_open and time.monotonic() - link.last_reply > cfg.no_comm_reset_s:
            log.error("no reply from motor board for %.1f s; reopening port", cfg.no_comm_reset_s)
            link.connected = False
            # Never resume at speed after an outage.
            link.commanded_linear = link.commanded_angular = 0.0
            self._port.close()
            return

        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.0, cfg.command_interval_s - elapsed))

    def _send(self, pkt: bytes) -> bool:
        try:
            self._port.write(pkt)
            return True
        except OSError as e:
            self.link.last_error = str(e)
            return False

    def _send_watchdog(self) -> None:
        if self._send(p.power(self.cfg.watchdog_delay_s, p.POWER_CYCLE)):
            self.link.watchdog_last_refresh = time.monotonic()

    def _drain_replies(self) -> None:
        while not self._replies.empty():
            self._replies.get_nowait()

    def _tag(self) -> int:
        mode = self.cfg.tag_mode
        if mode == "zero":
            return 0
        if mode == "counter":
            self._counter += 1
            return self._counter
        if mode == "board_time":
            return self.link.status.timestamp_us if self.link.status else 0
        # limiter: echo the board's limiter clock. The only tag the real board drives on (docs section 7).
        return self.link.status.limiter_tag if self.link.status else 0

    # --- telemetry -------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.monotonic()
        link, drive = self.link, self.drive
        return {
            "type": "status",
            "version": VERSION,
            "link": {
                "connected": link.connected and now - link.last_reply < 0.5,
                "device": self.cfg.device,
                "replies": link.replies,
                "timeouts": link.timeouts,
                "reconnects": link.reconnects,
                "crc_errors": self._reader.crc_errors,
                "last_error": link.last_error,
                "watchdog_age_s": round(now - link.watchdog_last_refresh, 1) if link.watchdog_last_refresh else None,
            },
            "drive": {
                "estop": drive.estop,
                "deadman_active": now - drive.last_input > self.cfg.deadman_s,
                "driver": (None if drive.owner is None or now - drive.last_input > self.cfg.deadman_s
                           else "brain" if drive.owner_priority == PRIORITY_BRAIN else "human"),
                "commanded_linear": round(link.commanded_linear, 3),
                "commanded_angular": round(link.commanded_angular, 3),
                "max_linear": self.cfg.max_linear,
                "max_angular": self.cfg.max_angular,
            },
            "board": link.status.to_dict() if link.status else None,
            "sleep": self.sleep.snapshot(),
        }


def visitor_name(request: web.Request) -> str | None:
    """The driver's first name from Tailscale, e.g. "Alex", or None if unknown.

    `tailscale serve` adds Tailscale-User-Name for tailnet visitors. Trust it only on connections that
    arrive through that local proxy; a direct LAN client could send the header itself.
    """
    if request.remote not in ("127.0.0.1", "::1"):
        return None
    raw = request.headers.get("Tailscale-User-Name", "").strip()
    if not raw:
        return None
    if raw.startswith("=?"):                       # non-ASCII names arrive RFC 2047-encoded
        from email.header import decode_header, make_header
        try:
            raw = str(make_header(decode_header(raw)))
        except Exception:
            return None
    first = raw.split()[0] if raw.split() else ""
    first = "".join(ch for ch in first if ch.isalnum() or ch in "-'.")[:24]
    return first or None


def visitor_login(request: web.Request) -> str | None:
    """The driver's Tailscale login (unique, unlike the name), trusted only as visitor_name trusts its header."""
    if request.remote not in ("127.0.0.1", "::1"):
        return None
    return request.headers.get("Tailscale-User-Login", "").strip()[:120] or None


CAMERA_ROLES = {"25a1:0006": "head", "25a1:0007": "nav"}


def _cameras(sysfs: str) -> list[tuple[str, str]]:
    """(device name, role) of each camera, in /dev/video* order."""
    cams = []
    for d in sorted(Path(sysfs).glob("video*"), key=lambda d: int(d.name[5:]) if d.name[5:].isdigit() else 0):
        try:
            if (d / "index").read_text().strip() != "0":      # each camera also has a metadata node
                continue
            usb = (d / "device").resolve().parent
            vid_pid = f"{(usb / 'idVendor').read_text().strip()}:{(usb / 'idProduct').read_text().strip()}"
        except OSError:
            vid_pid = ""
        cams.append((d.name, CAMERA_ROLES.get(vid_pid, "other")))
    return cams


def camera_roles(sysfs: str = "/sys/class/video4linux") -> list[str]:
    """The role of each camera, in /dev/video* order: the robot page's fallback guess at which is which.

    Both Beam+ cameras call themselves "M034-WDR" and Chromium shows no USB id, so the page can't tell
    them apart by label; which one becomes /dev/video0 depends on which firmware loaded first. Chromium
    doesn't always list them in that order, or only them, so the page asks cameras_in_use() instead.
    """
    return [role for _, role in _cameras(sysfs)]


VIDIOC_REQBUFS = 0xC0145608      # _IOWR('V', 8, struct v4l2_requestbuffers)


def cameras_in_use(sysfs: str = "/sys/class/video4linux", dev: str = "/dev") -> list[str]:
    """Roles of the cameras some program (the robot page) is capturing from right now. The page opens one
    camera at a time and asks, so it learns exactly which is which. A camera streams to one program at a
    time: asking it for zero buffers fails with EBUSY while another program has it, and changes nothing
    otherwise."""
    busy = []
    for name, role in _cameras(sysfs):
        try:
            fd = os.open(f"{dev}/{name}", os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            fcntl.ioctl(fd, VIDIOC_REQBUFS, bytearray(struct.pack("<IIII4x", 0, 1, 1, 0)))  # count, capture, mmap
        except OSError as e:
            if e.errno == errno.EBUSY:
                busy.append(role)
        finally:
            os.close(fd)
    return busy


def read_setup(path: str) -> dict | None:
    """Setup notes from the robot image (e.g. {"tailscale_login_url": ..., "url": ...}); None if absent."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _clamp(v: float, lim: float) -> float:
    return max(-lim, min(lim, v))


def _ramp(cur: float, tgt: float, up: float, down: float) -> float:
    """Move cur toward tgt; 'down' (toward zero / reversing) may be faster than 'up'."""
    if cur == tgt:
        return tgt
    slowing = abs(tgt) < abs(cur) or cur * tgt < 0
    step = down if slowing else up
    if cur < tgt:
        return min(tgt, cur + step)
    return max(tgt, cur - step)


# --- web -------------------------------------------------------------------------

def build_app(ctrl: MotorController, *, allow_remote_robot: bool = False,
              world_url: str | None = None) -> web.Application:
    """allow_remote_robot lets the robot media page connect from another machine (development only).

    world_url: the simulator's virtual-world endpoint (dev only); the robot test page then shows its camera.
    """
    cfg = ctrl.cfg
    # The phone's "Rocky listening" switch is remembered next to the sleep state.
    hub = MediaHub(cfg.brain_fps, os.path.join(os.path.dirname(cfg.sleep_state), "ears.json") if cfg.sleep_state else None)
    app = web.Application()

    def authorized(request: web.Request) -> bool:
        return cfg.token is None or secrets.compare_digest(request.query.get("token", ""), cfg.token)

    def is_local(request: web.Request) -> bool:
        # Behind `tailscale serve` (or any reverse proxy) every visitor arrives from 127.0.0.1,
        # so a forwarded request is never "the robot itself".
        proxied = any(h in request.headers for h in ("X-Forwarded-For", "Forwarded", "Tailscale-User-Login"))
        return request.remote in ("127.0.0.1", "::1") and not proxied

    robot_page_served: list[float] = []         # when the robot screen last loaded its page (kiosk watchdog)

    def page(name: str):
        async def handler(request: web.Request) -> web.StreamResponse:
            if not authorized(request):
                raise web.HTTPUnauthorized(text="Add ?token=... to the URL")
            if name == "robot.html":
                robot_page_served[:] = [time.monotonic()]
            return web.FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-cache"})
        return handler

    async def status(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        snap = ctrl.snapshot()
        snap["media"] = hub.snapshot()
        snap["setup"] = read_setup(cfg.setup_file)
        snap["cameras"] = camera_roles()
        snap["robot_page_age_s"] = round(time.monotonic() - robot_page_served[0], 1) if robot_page_served else None
        return web.json_response(snap)

    async def cameras_busy(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        return web.json_response({"in_use": await asyncio.to_thread(cameras_in_use)})

    async def robot_ws(request: web.Request) -> web.WebSocketResponse:
        # The robot page owns the cameras and microphone, so only the robot itself may take that role.
        if not (is_local(request) or allow_remote_robot):
            raise web.HTTPForbidden(text="the robot media page must run on the robot")
        ws = web.WebSocketResponse(heartbeat=5.0)
        await ws.prepare(request)
        await hub.attach_robot(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await hub.audio_from_robot(msg.data)      # the microphone, only while listening is allowed
                    continue
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data.startswith('{"type":"frame"'):
                    await hub.frame_from_robot(msg.data)      # fast path: relayed as-is, not parsed
                    continue
                try:
                    m = json.loads(msg.data)
                    if m.get("type") == "frame":              # same thing, differently formatted JSON
                        await hub.frame_from_robot(msg.data)
                    elif m.get("type") == "rtc":
                        await hub.from_robot(str(m["to"]), m["data"])
                    elif m.get("type") == "video_stats":
                        log.info("video to a driver: captured %s, sent %s", m.get("captured"), m.get("sent"))
                    elif m.get("type") == "volumes":
                        await hub.volumes_from_robot(m)
                    elif m.get("type") == "cameras":
                        log.info("robot page cameras: head=#%s nav=#%s, found by opening each: %s, roles %s, labels %s",
                                 m.get("head"), m.get("nav"), m.get("found"), m.get("roles"), m.get("all"))
                except (ValueError, TypeError, KeyError):
                    pass
        finally:
            await hub.detach_robot(ws)
        return ws

    async def panel_ws(request: web.Request) -> web.WebSocketResponse:
        # The phone on the robot (beam-setup phone forwards its localhost:8080 over USB): its camera is a
        # media source, so like the robot page it must be on the robot itself.
        if not (is_local(request) or allow_remote_robot):
            raise web.HTTPForbidden(text="the panel must run on the phone attached to the robot")
        ws = web.WebSocketResponse(heartbeat=5.0)
        await ws.prepare(request)
        await hub.attach_panel(ws)

        async def push() -> None:
            while not ws.closed:
                snap = ctrl.snapshot()
                snap["media"] = hub.snapshot()
                try:
                    await ws.send_json(snap)
                except ConnectionError:
                    return
                await asyncio.sleep(0.5)
        pusher = asyncio.create_task(push())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                    kind = m.get("type")
                    if kind == "sleep":
                        ctrl.go_to_sleep("someone at the robot")
                    elif kind == "wake":
                        ctrl.wake_up("someone at the robot")
                    else:
                        await hub.from_panel(m)
                except (ValueError, TypeError, AttributeError):
                    pass
        finally:
            pusher.cancel()
            await hub.detach_panel(ws)
        return ws

    async def brain_ws(request: web.Request) -> web.WebSocketResponse:
        # The brain sees the cameras and may drive, so it needs its own key (not just the page token).
        if cfg.brain_key is None or not secrets.compare_digest(request.query.get("key", ""), cfg.brain_key):
            raise web.HTTPForbidden(text="brain disabled or wrong key")
        ws = web.WebSocketResponse(heartbeat=5.0, max_msg_size=8 * 2**20)
        await ws.prepare(request)
        me = object()
        await hub.attach_brain(ws)
        log.info("brain connected from %s", request.remote)

        async def telemetry() -> None:
            # Heading, odometry and speeds for closed-loop moves ("turn 30 degrees", "drive 1 m").
            while not ws.closed:
                snap = ctrl.snapshot()
                snap["type"] = "telemetry"
                snap["you_are_driving"] = ctrl.drive.owner is me
                try:
                    await ws.send_json(snap)
                except ConnectionError:
                    return
                await asyncio.sleep(0.1)
        pusher = asyncio.create_task(telemetry())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                    if m.get("type") == "drive":
                        # Lower priority than any human: denied while someone is driving.
                        ok = ctrl.set_target(float(m.get("linear", 0)), float(m.get("angular", 0)), me,
                                             priority=PRIORITY_BRAIN)
                        if not ok:
                            await ws.send_json({"type": "denied", "reason": "the robot is asleep" if ctrl.sleep.asleep
                                                else "a person is driving"})
                    elif m.get("type") == "stop":
                        ctrl.release(me)
                    else:
                        await hub.from_brain(m)
                except (ValueError, TypeError, AttributeError):
                    pass
        finally:
            pusher.cancel()
            ctrl.release(me)
            await hub.detach_brain(ws)
        return ws

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        if request.query.get("role") == "robot":
            return await robot_ws(request)
        if request.query.get("role") == "brain":
            return await brain_ws(request)
        if request.query.get("role") == "panel":
            return await panel_ws(request)
        ws = web.WebSocketResponse(heartbeat=5.0)
        await ws.prepare(request)
        me = object()
        who = visitor_name(request)
        pid = await hub.attach_pilot(ws, who, visitor_login(request))
        await hub.greet_pilot(pid)
        peer = request.remote
        log.info("driver %sconnected from %s", f"{who} " if who else "", peer)

        async def push() -> None:
            while not ws.closed:
                snap = ctrl.snapshot()
                snap["media"] = hub.snapshot()
                snap["you_have_control"] = ctrl.drive.owner is me
                try:
                    await ws.send_json(snap)
                except ConnectionError:
                    return
                await asyncio.sleep(1.0 / cfg.telemetry_hz)

        pusher = asyncio.create_task(push())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                    kind = m.get("type")
                    if kind == "drive":
                        ok = ctrl.set_target(float(m.get("linear", 0)), float(m.get("angular", 0)), me)
                        if not ok:
                            await ws.send_json({"type": "denied", "reason": "the robot is asleep" if ctrl.sleep.asleep
                                                else "another driver has control"})
                    elif kind == "stop":
                        ctrl.set_target(0.0, 0.0, me)
                    elif kind == "sleep":
                        ctrl.go_to_sleep(who or "a driver")
                    elif kind == "wake":
                        ctrl.wake_up(who or "a driver")
                    elif kind == "estop":
                        ctrl.set_estop(True)
                    elif kind == "release_estop":
                        ctrl.set_estop(False)
                    elif kind == "rtc":
                        await hub.from_pilot(pid, m["data"])
                    elif kind == "rear_light":
                        await hub.rear_light_from_pilot(bool(m.get("on")))
                    elif kind == "brain":
                        if m["data"].get("kind") == "chat" and ctrl.sleep.asleep:
                            await ws.send_json({"type": "notice", "text": "Rocky is asleep. Wake the robot first."})
                        else:
                            await hub.brain_cmd_from_pilot(pid, m["data"])
                except (ValueError, TypeError, AttributeError, KeyError):
                    await ws.send_json({"type": "error", "reason": "bad message"})
        finally:
            ctrl.release(me)
            pusher.cancel()
            await hub.detach_pilot(pid)
            log.info("driver %s disconnected", peer)
        return ws

    app.router.add_get("/", page("index.html"))
    app.router.add_get("/robot", page("robot.html"))
    app.router.add_get("/panel", page("panel.html"))

    async def face_script(request: web.Request) -> web.StreamResponse:
        # Rocky's face drawing (no data in it), so no token: the robot page loads it with a plain <script src>.
        return web.FileResponse(WEB_DIR / "rocky-face.js", headers={"Cache-Control": "no-cache",
                                                                     "Content-Type": "text/javascript"})
    app.router.add_get("/rocky-face.js", face_script)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/cameras-in-use", cameras_busy)
    app.router.add_get("/ws", ws_handler)

    if allow_remote_robot:
        # Development only: a face photo the robot page's test mode can show to its "camera".
        test_face = Path(__file__).resolve().parent.parent / "tests" / "data" / "face_a.jpg"

        async def face(request: web.Request) -> web.StreamResponse:
            if not test_face.exists():
                raise web.HTTPNotFound()
            return web.FileResponse(test_face)
        app.router.add_get("/test/face.jpg", face)

        if world_url:
            # Same-origin proxy so the robot test page can draw the virtual camera into a canvas.
            import aiohttp as _aiohttp

            def proxy(path: str):
                async def handler(request: web.Request) -> web.Response:
                    try:
                        async with _aiohttp.ClientSession() as s, s.get(world_url.rstrip("/") + path) as r:
                            body = await r.read()
                            return web.Response(body=body, content_type=r.content_type,
                                                headers={"Cache-Control": "no-store"})
                    except _aiohttp.ClientError:
                        raise web.HTTPServiceUnavailable(text="virtual world not running")
                return handler
            app.router.add_get("/test/camera.jpg", proxy("/camera.jpg"))
            app.router.add_get("/test/nav.jpg", proxy("/nav.jpg"))
            app.router.add_get("/test/map.png", proxy("/map.png"))
            app.router.add_get("/test/pose", proxy("/pose"))

    async def sleep_schedule() -> None:
        while True:
            # "Busy" = someone drove in the last minute: the night waits until they're done.
            ctrl.sleep.tick(datetime.now(), busy=time.monotonic() - ctrl.drive.last_input < 60)
            await asyncio.sleep(10)

    tasks: list[asyncio.Task] = []

    async def on_startup(app: web.Application) -> None:
        ctrl.start()
        tasks.append(asyncio.create_task(sleep_schedule(), name="sleep-schedule"))

    async def on_cleanup(app: web.Application) -> None:
        for t in tasks:
            t.cancel()
        await ctrl.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Beam+ motor server")
    ap.add_argument("--device", default=Config.device, help="motor board serial port (default %(default)s)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--max-linear", type=float, default=Config.max_linear, help="m/s (default %(default)s)")
    ap.add_argument("--max-angular", type=float, default=Config.max_angular, help="rad/s (default %(default)s)")
    ap.add_argument("--deadman", type=float, default=Config.deadman_s, help="seconds (default %(default)s)")
    ap.add_argument("--tag-mode", choices=["limiter", "board_time", "zero", "counter"], default=Config.tag_mode)
    ap.add_argument("--token", help="require ?token=... on the page URL")
    ap.add_argument("--setup-file", default=Config.setup_file, help=argparse.SUPPRESS)
    ap.add_argument("--tls-cert", help="serve HTTPS (needed for the pilot's microphone); see scripts/make-cert.sh")
    ap.add_argument("--tls-key")
    ap.add_argument("--brain-key", help="enable the brain connection (face recognition etc.) with this shared key")
    ap.add_argument("--sleep-hours", help="sleep every night in this local-time window, e.g. 23:00-07:00")
    ap.add_argument("--backlight", default="auto",
                    help="screen backlight to switch off in sleep mode: a /sys/class/backlight/<name> path, "
                         "'auto' (default) or 'none'")
    ap.add_argument("--light-color", type=lambda s: int(s, 0), default=Config.light_color,
                    help="charging light colour, as the board understands it (default 0x0F00 = green; 0 = off)")
    # systemd's StateDirectory= sets $STATE_DIRECTORY: remember sleep there across restarts.
    ap.add_argument("--sleep-state", default=os.path.join(os.environ["STATE_DIRECTORY"], "sleep.json")
                    if os.environ.get("STATE_DIRECTORY") else None, help=argparse.SUPPRESS)
    ap.add_argument("--world-url", help=argparse.SUPPRESS)       # dev: simulator's virtual-world endpoint
    ap.add_argument("--allow-remote-robot", action="store_true",
                    help="development only: accept the robot media page from another machine")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config(device=args.device, max_linear=min(args.max_linear, 1.0), max_angular=args.max_angular,
                 deadman_s=args.deadman, tag_mode=args.tag_mode, token=args.token, setup_file=args.setup_file,
                 brain_key=args.brain_key, sleep_hours=args.sleep_hours,
                 backlight=None if args.backlight == "none" else args.backlight,
                 light_color=args.light_color, sleep_state=args.sleep_state)
    if cfg.token is None:
        log.warning("no --token set: anyone on this network can drive the robot")
    ssl_ctx = None
    if args.tls_cert:
        import ssl
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(args.tls_cert, args.tls_key)
    app = build_app(MotorController(cfg), allow_remote_robot=args.allow_remote_robot, world_url=args.world_url)
    # Access logs only with -v: the robot screen polls every few seconds and would flood the journal.
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx, print=None,
                access_log=logging.getLogger("aiohttp.access") if args.verbose else None)


if __name__ == "__main__":
    main()
