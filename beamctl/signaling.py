"""WebRTC signaling relay between the robot's media page and pilot pages.

Media never passes through the server. The robot page (Chromium kiosk on the
robot's own screen) owns the cameras, microphone and speaker. Each pilot page
gets a direct peer connection to it, and this hub only forwards the offer/answer
and ICE messages, tagged with the pilot's id.

  pilot -> server: {"type": "rtc", "data": {...}}
  server -> robot: {"type": "rtc", "from": <pilot id>, "data": {...}}
  robot -> server: {"type": "rtc", "to": <pilot id>, "data": {...}}
  server -> pilot: {"type": "rtc", "data": {...}}

When a pilot leaves, the robot gets data {"kind": "bye"} from that pilot.
When the robot (re)connects, every pilot gets {"type": "robot_online"} so it can say hello again.

The optional "brain" (face recognition and friends, running on another machine) also hangs off
this hub. While a brain is connected, the robot page sends camera frames:
  server -> robot: {"type": "frames", "fps": n}                 (0 = stop)
  robot -> server -> brain: {"type": "frame", "cam": "head", "w": .., "h": .., "jpeg": <base64>}
  brain -> server -> pilots: {"type": "faces", ...} / {"type": "people", ...} / {"type": "notice", ...}
  brain -> server -> robot:  {"type": "say", "text": .., "wav": <base64>} / {"type": "show", "text": ..}
  pilot -> server -> brain:  {"type": "brain_cmd", "from": <pilot id>, "data": {...}}  (enroll, forget, pause)
Everyone is told {"type": "brain", "online": bool} when it comes and goes, so the robot screen can
show that face recognition is running.
"""

from __future__ import annotations

import itertools
import logging

from aiohttp import web

log = logging.getLogger("beam.signaling")

REPLACED_CLOSE_CODE = 4000    # WebSocket close code sent to a robot page that another one replaced


class MediaHub:
    def __init__(self, brain_fps: float = 3.0) -> None:
        self.robot: web.WebSocketResponse | None = None
        self.pilots: dict[str, web.WebSocketResponse] = {}
        self.pilot_names: dict[str, str] = {}
        self.brain: web.WebSocketResponse | None = None
        self.brain_fps = brain_fps
        self.people: dict | None = None          # last "people" message, for pilots that join later
        self._brain_busy = False                 # drop frames rather than queue them behind a slow link
        self.frames_relayed = 0
        self.frames_dropped = 0
        self._ids = itertools.count(1)

    # --- membership ----------------------------------------------------------

    async def attach_robot(self, ws: web.WebSocketResponse) -> None:
        old, self.robot = self.robot, ws
        if old is not None and not old.closed:
            log.warning("a new robot media page replaced the previous one")
            # The page treats this code as "stand down" and does not reconnect; otherwise two
            # robot pages would keep knocking each other off.
            await old.close(code=REPLACED_CLOSE_CODE, message=b"replaced by another robot page")
        log.info("robot media page online")
        for pws in list(self.pilots.values()):
            await _send(pws, {"type": "robot_online"})
        await self._tell_robot_about_brain()
        await self._tell_robot_about_pilots()

    def detach_robot(self, ws: web.WebSocketResponse) -> None:
        if self.robot is ws:
            self.robot = None
            log.info("robot media page offline")

    async def attach_pilot(self, ws: web.WebSocketResponse, name: str | None = None) -> str:
        """name: who the driver is, if known (Tailscale tells us; see server.visitor_name)."""
        pid = f"p{next(self._ids)}"
        self.pilots[pid] = ws
        if name:
            self.pilot_names[pid] = name
        await self._tell_robot_about_pilots()
        return pid

    async def detach_pilot(self, pid: str) -> None:
        self.pilots.pop(pid, None)
        self.pilot_names.pop(pid, None)
        if self.robot is not None:
            await _send(self.robot, {"type": "rtc", "from": pid, "data": {"kind": "bye"}})
        await self._tell_robot_about_pilots()

    async def _tell_robot_about_pilots(self) -> None:
        # id -> name, so the robot screen can name the people actually connected to it on video.
        await _send(self.robot, {"type": "pilots", "names": dict(self.pilot_names)})

    # --- relay ---------------------------------------------------------------

    async def from_pilot(self, pid: str, data: dict) -> None:
        if self.robot is None or self.robot.closed:
            await _send(self.pilots.get(pid), {"type": "rtc_unavailable"})
            return
        await _send(self.robot, {"type": "rtc", "from": pid, "data": data})

    async def from_robot(self, to: str, data: dict) -> None:
        await _send(self.pilots.get(to), {"type": "rtc", "data": data})

    # --- brain ---------------------------------------------------------------

    @property
    def brain_online(self) -> bool:
        return self.brain is not None and not self.brain.closed

    async def attach_brain(self, ws: web.WebSocketResponse) -> None:
        old, self.brain = self.brain, ws
        if old is not None and not old.closed:
            log.warning("a new brain replaced the previous one")
            await old.close(code=REPLACED_CLOSE_CODE, message=b"replaced by another brain")
        self._brain_busy = False
        log.info("brain online")
        await self._tell_robot_about_brain()
        await self._broadcast({"type": "brain", "online": True})

    async def detach_brain(self, ws: web.WebSocketResponse) -> None:
        if self.brain is not ws:
            return
        self.brain = None
        self.people = None
        log.info("brain offline")
        await self._tell_robot_about_brain()
        await self._broadcast({"type": "brain", "online": False})
        await self._broadcast({"type": "faces", "faces": []})
        await _send(self.robot, {"type": "seen", "names": []})

    async def _tell_robot_about_brain(self) -> None:
        if self.robot is None:
            return
        await _send(self.robot, {"type": "brain", "online": self.brain_online})
        await _send(self.robot, {"type": "frames", "fps": self.brain_fps if self.brain_online else 0})

    async def frame_from_robot(self, raw: str) -> None:
        """Forward a frame message to the brain untouched (no re-encoding of the JPEG)."""
        if not self.brain_online:
            return
        if self._brain_busy:
            self.frames_dropped += 1
            return
        self._brain_busy = True
        try:
            await self.brain.send_str(raw)
            self.frames_relayed += 1
        except ConnectionError:
            pass
        finally:
            self._brain_busy = False

    async def from_brain(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind in ("faces", "people", "notice"):
            if kind == "people":
                self.people = msg
            await self._broadcast(msg)
        elif kind in ("say", "show", "seen"):
            await _send(self.robot, msg)
        elif kind == "ai":
            # The AI is (or stops being) in charge: everyone must be able to see that.
            await _send(self.robot, msg)
            await self._broadcast(msg)
        elif kind == "status":
            state = {"type": "brain", "online": True, "paused": bool(msg.get("paused"))}
            await _send(self.robot, state)
            await self._broadcast(state)

    async def brain_cmd_from_pilot(self, pid: str, data: dict) -> None:
        if not self.brain_online:
            await _send(self.pilots.get(pid), {"type": "brain", "online": False})
            return
        await _send(self.brain, {"type": "brain_cmd", "from": pid, "data": data})

    async def greet_pilot(self, pid: str) -> None:
        """Bring a newly connected pilot up to date on the brain."""
        ws = self.pilots.get(pid)
        await _send(ws, {"type": "brain", "online": self.brain_online})
        if self.people is not None:
            await _send(ws, self.people)

    async def _broadcast(self, msg: dict) -> None:
        for pws in list(self.pilots.values()):
            await _send(pws, msg)

    def snapshot(self) -> dict:
        return {"robot_online": self.robot is not None and not self.robot.closed,
                "viewers": len(self.pilots),
                "brain_online": self.brain_online,
                "frames_relayed": self.frames_relayed,
                "frames_dropped": self.frames_dropped}


async def _send(ws: web.WebSocketResponse | None, msg: dict) -> None:
    if ws is None or ws.closed:
        return
    try:
        await ws.send_json(msg)
    except ConnectionError:
        pass
