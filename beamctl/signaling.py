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
  brain -> server -> robot:  {"type": "say", "text": .., "wav": <base64>, "by"?, "emotion"?} / {"type": "show", "text": ..}
                             {"type": "face", "emotion": ..}  (Rocky's face, e.g. thinking while he waits for the AI)
  brain -> server -> pilots: {"type": "rocky", "sender", "heard", "emotion", "text", "voice", "tts_s"}  (a typed chat
                             and its reply) / {"type": "rocky_voice", "voice", "available"}  (Piper or Fish Audio)
  listening ("hey Rocky"): the brain asks with {"type": "ears", "on": true}; while it's allowed (robot page up, brain
                             asked, NO driver connected) robot and brain get {"type": "listen", "on": true}, and the
                             robot page sends its microphone as binary 16 kHz PCM, relayed to the brain as-is.
                             brain -> robot: {"type": "rocky_awake", "seconds"} (he heard his name: show it)
                             The phone has a switch for it, {"type": "ears_mute", "muted"} (remembered); robot and phone
                             get {"type": "ears_status", "muted", "available"}.
  pilot -> server -> brain:  {"type": "brain_cmd", "from": <pilot id>, "data": {...}}  (enroll, forget, pause, chat;
                             a chat gets "sender": the pilot's Tailscale name, added here)
Everyone is told {"type": "brain", "online": bool} when it comes and goes, so the robot screen can
show that face recognition is running.

The optional "panel" is a phone on the robot (USB, see beam-setup phone): a touch screen for people in
the room, and its back camera as a rear view. The phone is only on the home Wi-Fi, so pilots can't
reach it directly: the robot page receives the rear camera (over the LAN) and passes it on to every
pilot as a third video in the connection it already has with them.
  robot -> server: {"type": "rtc", "to": "panel", "data": {...}}
  server -> panel: {"type": "rtc", "from": "robot", "data": {...}}
  panel -> server: {"type": "rtc", "to": "robot", "data": {...}}
  server -> robot: {"type": "rtc", "from": "panel", "data": {...}}
The robot page is told {"type": "panel", "online": bool} when it comes and goes, and asks for the
rear camera only while someone is connected. A pilot's {"type": "rear_light", "on": bool} goes to the
panel as {"type": "light", "on": bool} (the phone's camera light); the panel answers {"type": "light",
"on": bool, "available": bool}, which every pilot gets as {"type": "rear_light", ...}. From the panel: {"type": "call"} (someone in
the room calls the owner) goes to every pilot as {"type": "call"}; {"type": "volume", "target": "speaker" |
"rocky", "delta": d} goes to the robot page, which plays the sound (drivers' voices / Rocky's voice) and
answers with its levels, {"type": "volumes", "speaker": .., "rocky": ..}, which the panel shows.
"""

from __future__ import annotations

import itertools
import json
import logging
from pathlib import Path

from aiohttp import web

log = logging.getLogger("beam.signaling")

REPLACED_CLOSE_CODE = 4000    # WebSocket close code sent to a robot page that another one replaced


class MediaHub:
    def __init__(self, brain_fps: float = 3.0, ears_state: str | None = None) -> None:
        self.robot: web.WebSocketResponse | None = None
        self.panel: web.WebSocketResponse | None = None
        self.rear_light: dict | None = None      # last {"type": "rear_light", ...} from the panel
        self.pilots: dict[str, web.WebSocketResponse] = {}
        self.pilot_names: dict[str, str] = {}
        self.brain: web.WebSocketResponse | None = None
        self.brain_fps = brain_fps
        self.people: dict | None = None          # last "people" message, for pilots that join later
        self.rocky_voice: dict | None = None     # last "rocky_voice" message (Piper or Fish), likewise
        self.volumes: dict | None = None         # the robot page's speaker and Rocky levels, for the phone
        self.brain_ears = False                  # the brain asked for the microphone (Rocky listens)
        self.listening = False                   # the robot page is sending it (see _update_listen)
        self.audio_relayed = 0
        # The phone's "Rocky listening" switch: people in the room can turn his microphone off. Remembered.
        self.ears_state = Path(ears_state) if ears_state else None
        self.ears_muted = False
        try:
            self.ears_muted = bool(json.loads(self.ears_state.read_text()).get("muted"))
        except (AttributeError, OSError, ValueError):
            pass
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
        await self._tell_robot_about_panel()
        await self._update_listen(force=True)
        await self._tell_about_ears()

    async def detach_robot(self, ws: web.WebSocketResponse) -> None:
        if self.robot is ws:
            self.robot = None
            log.info("robot media page offline")
            await self._update_listen()

    async def attach_pilot(self, ws: web.WebSocketResponse, name: str | None = None) -> str:
        """name: who the driver is, if known (Tailscale tells us; see server.visitor_name)."""
        pid = f"p{next(self._ids)}"
        self.pilots[pid] = ws
        if name:
            self.pilot_names[pid] = name
        await self._tell_robot_about_pilots()
        await self._update_listen()
        return pid

    async def detach_pilot(self, pid: str) -> None:
        self.pilots.pop(pid, None)
        self.pilot_names.pop(pid, None)
        for media in (self.robot, self.panel):
            await _send(media, {"type": "rtc", "from": pid, "data": {"kind": "bye"}})
        await self._tell_robot_about_pilots()
        await self._update_listen()

    # --- listening: the robot's microphone to the brain (Rocky, "hey Rocky") -------------

    @property
    def listen_allowed(self) -> bool:
        """Only while nobody drives (a driver's conversation is theirs, and Rocky stays quiet), and only when
        the brain asked for it. Asleep, the robot page is closed, so nothing is sent then either."""
        return (self.robot is not None and not self.robot.closed and self.brain_online and self.brain_ears
                and not self.pilots and not self.ears_muted)

    def ears_status(self) -> dict:
        """For the phone's switch and the robot screen: is the switch off, and is there a Rocky to listen at all."""
        return {"type": "ears_status", "muted": self.ears_muted, "available": self.brain_online and self.brain_ears}

    async def _tell_about_ears(self) -> None:
        for ws in (self.robot, self.panel):
            await _send(ws, self.ears_status())

    async def set_ears_muted(self, muted: bool) -> None:
        if muted != self.ears_muted:
            self.ears_muted = muted
            log.info("Rocky's microphone switched %s on the phone", "off" if muted else "on")
            if self.ears_state is not None:
                try:
                    self.ears_state.write_text(json.dumps({"muted": muted}) + "\n")
                except OSError as e:
                    log.warning("can't save the listening switch to %s: %s", self.ears_state, e)
        await self._update_listen()
        await self._tell_about_ears()

    async def _update_listen(self, force: bool = False) -> None:
        on = self.listen_allowed
        if on == self.listening and not force:
            return
        changed, self.listening = on != self.listening, on
        await _send(self.robot, {"type": "listen", "on": on})      # a new robot page always hears it
        if changed:
            log.info("microphone to the brain %s", "on" if on else "off")
            await _send(self.brain, {"type": "listen", "on": on})

    async def audio_from_robot(self, data: bytes) -> None:
        """16 kHz PCM from the robot page. Checked again here: nothing goes to the brain unless allowed."""
        if not self.listening or not self.brain_online:
            return
        try:
            await self.brain.send_bytes(data)
            self.audio_relayed += 1
        except ConnectionError:
            pass

    async def _tell_robot_about_pilots(self) -> None:
        # id -> name, so the robot screen (and the panel) can name the people connected on video.
        for media in (self.robot, self.panel):
            await _send(media, {"type": "pilots", "names": dict(self.pilot_names)})

    # --- panel (phone on the robot) ------------------------------------------

    @property
    def panel_online(self) -> bool:
        return self.panel is not None and not self.panel.closed

    async def attach_panel(self, ws: web.WebSocketResponse) -> None:
        old, self.panel = self.panel, ws
        if old is not None and not old.closed:
            await old.close(code=REPLACED_CLOSE_CODE, message=b"replaced by another panel")
        log.info("panel (phone) online")
        await self._tell_robot_about_pilots()
        await self._tell_robot_about_panel()
        if self.volumes is not None:
            await _send(ws, self.volumes)
        await _send(ws, self.ears_status())

    async def detach_panel(self, ws: web.WebSocketResponse) -> None:
        if self.panel is ws:
            self.panel = None
            log.info("panel (phone) offline")
            self.rear_light = None
            await self._broadcast({"type": "rear_light", "on": False, "available": False})
            await self._tell_robot_about_panel()

    async def _tell_robot_about_panel(self) -> None:
        await _send(self.robot, {"type": "panel", "online": self.panel_online})

    async def from_panel(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "rtc":
            # The rear camera goes to the robot page only; it passes the video on to the pilots.
            if msg.get("to") == "robot":
                await _send(self.robot, {"type": "rtc", "from": "panel", "data": msg.get("data")})
        elif kind == "call":
            log.info("someone at the robot is calling the owner")
            await self._broadcast({"type": "call"})
        elif kind == "volume":
            target = "rocky" if msg.get("target") == "rocky" else "speaker"
            await _send(self.robot, {"type": "volume", "target": target, "delta": float(msg.get("delta", 0))})
        elif kind == "ears_mute":
            await self.set_ears_muted(bool(msg.get("muted")))
        elif kind == "light":
            self.rear_light = {"type": "rear_light", "on": bool(msg.get("on")), "available": bool(msg.get("available"))}
            await self._broadcast(self.rear_light)

    async def volumes_from_robot(self, msg: dict) -> None:
        """The robot page's current levels, for the phone's volume buttons to show."""
        self.volumes = {"type": "volumes", "speaker": float(msg.get("speaker", 1)), "rocky": float(msg.get("rocky", 1))}
        await _send(self.panel, self.volumes)

    async def rear_light_from_pilot(self, on: bool) -> None:
        """The phone's camera light, to see behind the robot in the dark (e.g. backing onto the charger)."""
        await _send(self.panel, {"type": "light", "on": bool(on)})

    # --- relay ---------------------------------------------------------------

    async def from_pilot(self, pid: str, data: dict) -> None:
        if self.robot is None or self.robot.closed:
            await _send(self.pilots.get(pid), {"type": "rtc_unavailable"})
            return
        await _send(self.robot, {"type": "rtc", "from": pid, "data": data})

    async def from_robot(self, to: str, data: dict) -> None:
        if to == "panel":
            await _send(self.panel, {"type": "rtc", "from": "robot", "data": data})
            return
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
        self.brain_ears = False
        await self._update_listen()
        await self._tell_about_ears()
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
        elif kind in ("say", "show", "seen", "face", "rocky_awake"):
            await _send(self.robot, msg)
        elif kind == "ears":
            self.brain_ears = bool(msg.get("on"))
            await self._update_listen()
            await self._tell_about_ears()
        elif kind == "rocky":
            await self._broadcast(msg)            # the exchange, so every driver can read what Rocky said
        elif kind == "rocky_voice":
            self.rocky_voice = msg                # for drivers who join later
            await self._broadcast(msg)
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
        if data.get("kind") == "chat":
            # Who's typing comes from the server (the Tailscale name), never from the page.
            data = {**data, "sender": self.pilot_names.get(pid)}
        await _send(self.brain, {"type": "brain_cmd", "from": pid, "data": data})

    async def greet_pilot(self, pid: str) -> None:
        """Bring a newly connected pilot up to date on the brain."""
        ws = self.pilots.get(pid)
        await _send(ws, {"type": "brain", "online": self.brain_online})
        if self.panel_online and self.rear_light is not None:
            await _send(ws, self.rear_light)
        if self.people is not None:
            await _send(ws, self.people)
        if self.brain_online and self.rocky_voice is not None:
            await _send(ws, self.rocky_voice)

    async def _broadcast(self, msg: dict) -> None:
        for pws in list(self.pilots.values()):
            await _send(pws, msg)

    def snapshot(self) -> dict:
        return {"robot_online": self.robot is not None and not self.robot.closed,
                "panel_online": self.panel_online,
                "viewers": len(self.pilots),
                "viewer_names": sorted(set(self.pilot_names.values())),
                "brain_online": self.brain_online,
                "frames_relayed": self.frames_relayed,
                "frames_dropped": self.frames_dropped,
                "listening": self.listening, "audio_relayed": self.audio_relayed}


async def _send(ws: web.WebSocketResponse | None, msg: dict) -> None:
    if ws is None or ws.closed:
        return
    try:
        await ws.send_json(msg)
    except ConnectionError:
        pass
