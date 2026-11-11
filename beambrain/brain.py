"""Brain main loop: connect to the robot, recognise faces in its camera frames, greet people.

  python3 -m beambrain --url wss://beam.<tailnet>.ts.net/ws --key <BRAIN_KEY> [--token <ACCESS_TOKEN>]

Messages (see beamctl/signaling.py for the server side):
  in:  frame (from the robot camera), brain_cmd (from a driving page: enroll / forget / pause / chat / voice /
       personality)
  out: faces, people, notice, rocky (to driving pages); say, show, face (to the robot); status (to everyone)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import signal
import time
from pathlib import Path

import aiohttp
import cv2
import numpy as np

from .ears import WAVE_AWAKE_S, Wake
from .faces import FaceEngine, thumbnail_jpeg
from .greeter import Greeter
from .people import PeopleStore
from .rocky import MAX_PERSONALITY, PERSONALITY
from .speech import FISH_ROCKY, Speaker
from .tracker import Tracker
from .wave import WaveDetector

log = logging.getLogger("beambrain")

REPLACED_CLOSE_CODE = 4000   # the robot server closes with this when another brain takes our place (signaling.py)


class Brain:
    def __init__(self, engine: FaceEngine | None, store: PeopleStore, speaker: Speaker | None, greeter: Greeter,
                 ai: dict | None = None):
        """engine None = no face recognition (e.g. the driving bake-off).
        ai: {"key": ..., "model": ...} enables AI driving tasks from the driving page."""
        self.engine, self.store, self.speaker, self.greeter = engine, store, speaker, greeter
        self.ai = ai
        self.tracker = Tracker()
        self.paused = False
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._busy = False
        self._send_lock = asyncio.Lock()
        self.frames_seen = 0
        self.frames_analyzed = 0
        self.in_view: list[str] = []         # confirmed names currently in front of the camera
        # For the AI driver: latest robot telemetry and camera frame, and "a person took over".
        self.telemetry: dict | None = None
        self.last_jpeg: str | None = None    # base64, head camera
        self.last_frame_at = 0.0
        self.frames: dict[str, tuple[str, float]] = {}   # cam -> (base64 jpeg, received at)
        self.snapshot_dir = os.path.expanduser("~/beam-brain/calibration")   # save_snapshot()
        self.maps_dir: Path | None = None                                    # the lidar map (lidarmap.py); main() sets it
        self.mapping = None                                                   # a Recorder while mapping
        self._map_status_at = 0.0
        self.localizer = None                                                 # where it is on the map (localize.py)
        self._localizing: asyncio.Task | None = None
        self._pose_sent: tuple[dict, float] = ({}, 0.0)
        self._places = None                                                   # named places on it (places.py)
        self.home = None                     # Home Assistant (home.py), for room presence; main() sets it
        self.presence: dict[str, bool | None] = {}   # place id -> someone there now (its HA sensors), None: unknown
        self._presence_poke = asyncio.Event()        # read the sensors now (a place's sensors changed)
        self.camera = None             # the robot's measured cameras (geometry.CameraModel); None = simulator's
        self.human_took_over = asyncio.Event()
        self.telemetry_listeners: list = []      # e.g. the AI driver's breadcrumb tracker
        self.driver_task: asyncio.Task | None = None
        self.driver = None
        self.rocky = None                    # the personality (beambrain/rocky.py), when there's an AI key
        self.chat_task: asyncio.Task | None = None
        self.thinking_about: str | None = None   # what was said to him, while the AI is still answering it
        self.fish = None                     # Fish Audio voice (speech.FishVoice), when there's a key
        self.rocky_voice = "piper"           # "piper" or "fish", switched from the driving page
        self.say_id = 0                      # numbers Fish's streamed replies, so the robot can tell them apart
        self.personality_by: dict | None = None  # who last changed his personality, and when: {"name", "at"}
        self.settings_path: Path | None = None   # where those choices are remembered across restarts
        self.ears = None                     # beambrain/ears.Ears: the robot's microphone -> text
        self.wake = Wake()
        self.wake_reply: asyncio.Task | None = None   # "Question?" after his name alone, unless words follow
        self.waves: WaveDetector | None = None   # beambrain/wave.py: a wave wakes him too (needs the face engine)
        self.listening = False               # the server sends the microphone (awake, nobody driving)
        self.pending_trip: tuple[str, float] | None = None   # (place id, until): asked in the room, waiting for "yes"
        self.home_drivers: set[str] = set()  # Tailscale logins whose typed chats may control the home
        self._tasks: set[asyncio.Task] = set()   # fire-and-forget tasks, held so they aren't garbage-collected
        self.replaced = False                # another brain took our place on the robot: stand down

    def spawn(self, coro) -> asyncio.Task:
        """Run something in the background, keeping a reference until it ends."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- connection ------------------------------------------------------------

    async def run(self, url: str, stop: asyncio.Event | None = None) -> None:
        """Connect and serve until stopped, reconnecting with backoff."""
        stop = stop or asyncio.Event()
        with contextlib.suppress(NotImplementedError, RuntimeError):     # not on Windows, nor off the main thread
            asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, self.save_snapshot)
            asyncio.get_running_loop().add_signal_handler(signal.SIGUSR2, lambda: self.spawn(self.move_test()))
        if self.ears is not None and not self.ears.thread.is_alive():
            try:
                await asyncio.to_thread(self.ears.load)
                self.ears.start()
                log.info("ears ready (speech recognition on this computer)")
            except Exception:
                log.exception("speech recognition unavailable; Rocky won't listen")
                self.ears = None
        async with aiohttp.ClientSession() as session:
            presence = asyncio.create_task(self.watch_presence(session))
            try:
                await self._connect_loop(session, url, stop)
            finally:
                presence.cancel()

    async def _connect_loop(self, session: aiohttp.ClientSession, url: str, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                async with session.ws_connect(url, heartbeat=10, max_msg_size=8 * 2**20) as ws:
                    log.info("connected to robot")
                    delay = 1.0
                    await self.serve(ws, stop)
                if ws.close_code == REPLACED_CLOSE_CODE:
                    # Another brain connected with the same key and the server dropped us for it. Coming
                    # back would only knock that one off in turn, over and over, so stay off (like the
                    # robot page does) until this one is restarted. Exiting wouldn't do: systemd would
                    # restart it at once.
                    log.warning("another brain replaced this one on the robot; standing down until restarted")
                    self.replaced = True
                    await stop.wait()
                    break
            except (aiohttp.ClientError, OSError) as e:
                # Not the error's text: aiohttp's includes the URL, which carries the brain key.
                status = getattr(e, "status", None)
                log.warning("robot not reachable (%s%s); retrying in %.0f s", type(e).__name__,
                            f" {status}" if status else "", delay)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30)

    async def serve(self, ws: aiohttp.ClientWebSocketResponse, stop: asyncio.Event) -> None:
        self.ws = ws
        self.in_view = []
        await self.send_people()
        await self.send({"type": "status", "paused": self.paused})
        await self.send({"type": "seen", "names": []})
        if self.rocky is not None:
            await self.send_voice()
            await self.send_personality()
        # Ask for the robot's microphone; the server decides when it may come (awake, nobody driving).
        await self.send({"type": "ears", "on": self.rocky is not None and self.ears is not None})
        await self.send_map()
        await self.send_places()
        if self.localizer is None and self._localizing is None:
            self._localizing = asyncio.create_task(self.load_localizer())   # may rebuild an older map: ~15 s
        stopper = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                recv = asyncio.create_task(ws.receive())
                done, _ = await asyncio.wait({recv, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    recv.cancel()
                    break
                msg = recv.result()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if self.ears is not None:
                        self.ears.feed(msg.data)          # the robot's microphone, 16 kHz PCM
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                m: dict = {}
                try:
                    m = json.loads(msg.data)
                    await self.on_message(m)
                except Exception as e:
                    # One bad message (e.g. a driving page sending a name where a number belongs) mustn't
                    # take the whole brain down. Only the error's type: the message may hold words or names.
                    log.warning("skipped a message the brain couldn't handle (%s)", type(e).__name__)
                    if isinstance(m, dict) and m.get("type") == "telemetry" and self.driver is not None:
                        self.driver.abort("couldn't read the robot's telemetry")   # driving blind: stop
        finally:
            stopper.cancel()
            self.ws = None
            await self.on_listen(False)
            if self.driver is not None:
                self.driver.abort("lost connection to the robot")

    async def on_message(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "listen":
            await self.on_listen(bool(m.get("on")))
        elif kind == "frame":
            self.on_frame(m)
        elif kind == "telemetry":
            self.telemetry = m
            if self.mapping is not None:
                self.mapping.feed(m)
                if time.monotonic() - self._map_status_at > 1.0:
                    await self.send_map_status()
            if self.localizer is not None:
                await self.localize(m)
            for listener in list(self.telemetry_listeners):
                listener(m)
        elif kind == "denied":
            self.human_took_over.set()      # a person is driving: the AI must stand down
        elif kind == "brain_cmd":
            await self.on_command(m.get("data") or {})

    async def send(self, msg: dict) -> None:
        async with self._send_lock:
            if self.ws is not None and not self.ws.closed:
                await self.ws.send_json(msg)

    # --- frames ----------------------------------------------------------------

    def on_frame(self, m: dict) -> None:
        cam = m.get("cam", "head")
        self.frames[cam] = (m.get("jpeg"), time.monotonic())
        if cam != "head":
            return                          # faces: head camera only; the floor camera is for driving
        self.frames_seen += 1
        self.last_jpeg, self.last_frame_at = m.get("jpeg"), time.monotonic()
        if self.engine is None or self.paused or self._busy:
            return                          # only ever work on the latest frame
        self._busy = True
        self.spawn(self._process(m))

    async def _process(self, m: dict) -> None:
        try:
            img = cv2.imdecode(np.frombuffer(base64.b64decode(m["jpeg"]), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return
            faces = await asyncio.to_thread(self.engine.analyze, img)
            self.frames_analyzed += 1
            tracks = self.tracker.update(faces)
            for t in tracks:
                name, score = self.store.match(t.face.embedding)
                t.votes.append(name)
                t.score = score
            await self.send({"type": "faces", "w": img.shape[1], "h": img.shape[0], "faces": [
                {"id": t.id, "x": t.face.box[0], "y": t.face.box[1], "w": t.face.box[2], "h": t.face.box[3],
                 "name": t.name, "score": round(t.score, 3)} for t in tracks]})
            # All live tracks, not just this frame's detections: one missed frame mustn't make names flicker.
            await self.update_in_view(sorted({t.name for t in self.tracker.tracks.values() if t.name}))
            if self.waves is not None and self.listening and self.rocky is not None and not self.wake.awake:
                waved = self.waves.update(img, [f.box for f in faces], time.monotonic())
                w = self.waves
                log.debug("wave check: faces %d, hand zone %.2f, person moved %.2f, scene %.2f, hits %d/%d",
                          len(faces), w.last_score, w.last_moved, w.last_scene, sum(h > 0 for h in w.hits), len(w.hits))
                if waved:
                    await self.on_wave()
            for t in tracks:
                if t.name:
                    text = self.greeter.maybe_greet(t.name)
                    if text:
                        self.spawn(self.greet(text))   # Fish takes ~1 s; keep watching meanwhile
        except Exception:
            log.exception("frame processing failed")
        finally:
            self._busy = False

    def save_snapshot(self) -> None:
        """Camera calibration (docs/ai-driving.md): `kill -USR1` the brain and it saves the latest frame of each
        camera to ~/beam-brain/calibration/ (next to the data folder), on this computer only. Delete them when done."""
        out = Path(self.snapshot_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        saved = 0
        for cam, (jpeg, at) in self.frames.items():
            if jpeg and time.monotonic() - at < 2.0:
                (out / f"{stamp}-{cam}.jpg").write_bytes(base64.b64decode(jpeg))
                saved += 1
        log.info("calibration snapshot: saved %d camera frame(s)", saved)

    async def move_test(self) -> None:
        """Checking the AI's moves on the real robot (docs/ai-driving.md): `kill -USR2` the brain and it does the
        one move in ~/beam-brain/calibration/move.txt ("turn 90", "drive 0.5", "dock", "dock-teach" on the charger),
        labelled as the AI like any AI driving, stopped by any human input, and logs what the gyro and wheels measured."""
        try:
            kind, *rest = (Path(self.snapshot_dir) / "move.txt").read_text().split()
            amount = float(rest[0]) if kind in ("turn", "drive") else 0.0
            if kind not in ("turn", "drive", "dock", "dock-teach"):
                raise ValueError(kind)
        except (OSError, ValueError, IndexError) as e:
            log.warning("move test: no valid move in move.txt (%s)", e)
            return
        await self.run_move(kind, amount)

    def ai_driving(self) -> bool:
        return self.driver_task is not None and not self.driver_task.done() and self.driver_task is not asyncio.current_task()

    async def run_move(self, kind: str, amount: float = 0.0, place: dict | None = None) -> str | None:
        """One move ("turn", "drive"), docking ("dock", "dock-teach") or going to a named place ("goto", place) by
        the AI driver's own code: labelled as the AI on every screen, stopped by any human input or the AI card's
        Stop, and logged with what the gyro and wheels measured. None if the AI driver is already busy."""
        if self.ai_driving():
            await self.send({"type": "notice", "text": "The AI is already driving."})
            return None
        from .dock import Docking
        from .driver import AIDriver, Aborted
        from .navigate import Planner, go_to
        what = ("docking" if kind.startswith("dock") else f"going to “{place['names'][0]}”" if kind == "goto"
                else f"move test: {kind} {amount:g}")
        model = type("NoModel", (), {"model": "docking" if kind.startswith("dock") else
                                     "map driving" if kind == "goto" else "move test"})()
        d = AIDriver(self, model, what, camera=self.camera)
        self.driver, self.driver_task = d, asyncio.current_task()
        docking = Docking(self, d, (self.settings_path.parent if self.settings_path else Path(self.snapshot_dir)) / "dock.json")
        self.human_took_over.clear()
        before = dict(d._board())
        outcome = "gave_up"
        await d._label(True, action="docking: looking for the dock" if kind == "dock" else what)
        try:
            if kind == "dock":
                result = await docking.run()
            elif kind == "dock-teach":
                result = await docking.teach()
            elif kind == "goto":
                loc = self.localizer
                if loc is None or loc.state == "searching":
                    result = "doesn't know where it is on the map"
                else:
                    planner = await asyncio.to_thread(Planner, loc.grid)
                    why = await go_to(self, d, planner, (place["x"], place["y"], place["th"]), place["names"][0])
                    result = f"couldn't get there: {why}" if why else "arrived"
            else:
                result = await (d.turn(amount) if kind == "turn" else d.drive(amount))
            if isinstance(result, tuple):
                result = result[0]
            if kind not in ("dock", "dock-teach", "goto") or "docked: on the charger" in result or result == "arrived":
                outcome = "done"
        except Aborted as e:
            result, outcome = f"stopped: {e}", "aborted"
        finally:
            await d._cmd(0.0, 0.0)
            await self.send({"type": "stop"})
            await self.send({"type": "rear_feed", "on": False})
        await d._label(False, outcome=outcome, summary=result)
        if kind.startswith("dock"):
            await self.send({"type": "notice", "text": f"🤖 Docking: {result}"})
        elif kind == "goto":
            await self.send({"type": "notice", "text": f"🤖 Going to “{place['names'][0]}”: {result}"})
        after = d._board()
        dyaw = (after.get("yaw_deg", 0.0) - before.get("yaw_deg", 0.0) + 180) % 360 - 180
        log.info("move test %s %g: %s | gyro %+.1f deg, wheels %+.3f m", kind, amount, result,
                 dyaw, after.get("odometry_m", 0.0) - before.get("odometry_m", 0.0))
        return result

    # --- the lidar map (lidarmap.py): a person drives the robot round once ------------------------------------

    async def start_mapping(self) -> None:
        from .lidarmap import Recorder
        if self.mapping is not None or self.maps_dir is None:
            return
        if not (self.telemetry or {}).get("scan"):
            await self.send({"type": "notice", "text": "The lidar isn't sending scans: is the robot awake?"})
            return
        b = (self.telemetry or {}).get("board") or {}
        if not (b.get("charging") or b.get("charger_detected")):
            # The map is anchored at the dock: places, and docking by the map, rely on it.
            await self.send({"type": "notice", "text": "Start mapping with the robot on its charger: the map is "
                                                       "anchored at the dock (your places depend on that)."})
            return
        self.mapping = Recorder()
        log.info("mapping: recording")
        await self.send_map_status()

    async def finish_mapping(self) -> None:
        from .lidarmap import build, render, save_grid
        rec, self.mapping = self.mapping, None
        if rec is None:
            return
        await self.send_map_status(building=len(rec.rows) >= 10)
        if len(rec.rows) < 10:
            await self.send({"type": "notice", "text": "Too short to make a map: drive it round a little first."})
            return
        path = await asyncio.to_thread(rec.save, self.maps_dir)
        log.info("mapping: %d scans over %.1f m saved; building the map", len(rec.rows), rec.metres)
        t0 = time.monotonic()
        built = await asyncio.to_thread(build, rec.rows)
        if built.dock is None or built.dock != built.poses[0]:
            log.info("mapping: didn't start on the charger; the map in use is kept")
            await self.send_map_status()
            await self.send({"type": "notice", "text": "Not used: that drive didn't start on the charger, so the map "
                                                       "in use is kept (the drive is saved on the brain)."})
            return
        png, info = await asyncio.to_thread(render, built)
        await asyncio.to_thread(save_grid, built, self.maps_dir / "map.npz")
        info.update(recording=path.name, built=time.strftime("%Y-%m-%d %H:%M"), metres=round(rec.metres, 1))
        (self.maps_dir / "map.png").write_bytes(png)
        (self.maps_dir / "map.json").write_text(json.dumps(info) + "\n")
        log.info("mapping: map built in %.0f s (%d scans used, median fit %.2f)", time.monotonic() - t0,
                 info["scans"], info["fit"])
        await self.send_map_status()
        await self.send_map()
        await self.load_localizer()

    async def load_localizer(self) -> None:
        """Track the robot on the saved map. A map made before the grid was kept is rebuilt from its recording."""
        from .lidarmap import build, load, load_grid, save_grid
        from .localize import Localizer
        if self.maps_dir is None:
            return
        grid_path = self.maps_dir / "map.npz"
        try:
            if not grid_path.exists():
                rec = json.loads((self.maps_dir / "map.json").read_text())["recording"]
                built = await asyncio.to_thread(lambda: build(load(self.maps_dir / rec)))
                await asyncio.to_thread(save_grid, built, grid_path)
            grid, dock = await asyncio.to_thread(load_grid, grid_path)
            self.localizer = await asyncio.to_thread(Localizer, grid, dock)
            log.info("localize: map loaded")
        except (OSError, ValueError, KeyError):
            self.localizer = None                          # no map yet

    async def localize(self, t: dict) -> None:
        """Telemetry in: move the pose by the wheels and gyro, match a scan when one is due (in a worker thread,
        one at a time), and tell the driving pages where the robot is."""
        loc = self.localizer
        loc.feed(t, time.monotonic())
        if self._localizing is None or self._localizing.done():
            job = loc.take()
            if job is not None:
                async def run():
                    try:
                        loc.apply(job, await asyncio.to_thread(loc.match, job))
                    except Exception:
                        log.exception("localize: matching failed")
                self._localizing = asyncio.create_task(run())
        snap, at = loc.snapshot(), time.monotonic()
        last, last_at = self._pose_sent
        if snap != last and at - last_at > 0.25 or at - last_at > 5.0:
            self._pose_sent = (snap, at)
            await self.send({"type": "map_pose", **snap})

    # --- named places on the map (places.py): "go to the kitchen" -------------------------------------------

    @property
    def places(self):
        from .places import Places
        if self._places is None:
            self._places = Places(self.maps_dir / "places.json" if self.maps_dir else None)
        return self._places

    def trips(self):
        """What Rocky's go_to tool uses (rocky.py)."""
        import types
        return types.SimpleNamespace(names=lambda: self.places.all_names(), go=self.go_to_place,
                                     ask=self.ask_trip, now=self.current_trip, people=self.people_at)

    def current_trip(self) -> str | None:
        """What the AI driver is doing right now ("going to “kitchen”", "docking", ...), or None."""
        return getattr(self.driver, "task", None) if self.ai_driving() else None

    async def send_places(self) -> None:
        hint = getattr(self.ears, "hint", None)
        if hint is not None:
            hint(self.places.all_names())                   # so speech recognition expects these words
        places = self.places.summary()
        for p in places:
            if p.get("sensors"):
                p["here"] = self.presence.get(p["id"])        # someone there now: True / False / None (unknown)
        await self.send({"type": "places", "places": places})

    # --- room presence: Home Assistant sensors named on places ---------------------------------------------

    PRESENCE_S = 5.0          # how often the sensors are read
    PRESENCE_RETRY_S = 60.0   # after HA didn't answer

    def read_presence(self, states: dict[str, bool | None]) -> dict[str, bool | None]:
        """Sensor states -> place id: someone there (any sensor on), no one (all known and off), or None."""
        out = {}
        for pid, ids in self.places.sensors().items():
            known = [states.get(e) for e in ids if states.get(e) is not None]
            out[pid] = True if any(known) else (False if known else None)
        return out

    async def watch_presence(self, session: aiohttp.ClientSession) -> None:
        """Read the places' presence sensors every few seconds; tell the driving page when that changes. Never
        logs which rooms have someone in them: only how many sensors, and when HA stops or starts answering."""
        answering = None
        while True:
            wait = self.PRESENCE_S
            sensors = self.places.sensors() if self.home is not None else {}
            if sensors:
                try:
                    states = await self.home.states(session, [e for ids in sensors.values() for e in ids])
                    new = self.read_presence(states)
                    if answering is not True:
                        log.info("room presence: reading %d sensor(s) for %d place(s)",
                                 sum(map(len, sensors.values())), len(sensors))
                    answering = True
                except Exception as e:
                    if answering is not False:
                        log.warning("room presence: Home Assistant didn't answer (%s); trying again every %.0f s",
                                    type(e).__name__, self.PRESENCE_RETRY_S)
                    answering, wait = False, self.PRESENCE_RETRY_S
                    new = {pid: None for pid in sensors}
            else:
                new = {}
            if new != self.presence:
                self.presence = new
                if self.ws is not None:
                    await self.send_places()
            self._presence_poke.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._presence_poke.wait(), wait)

    def people_at(self) -> dict | None:
        """For Rocky: {"seen": places someone is at now, "watched": places with sensors}, or None if none."""
        watched = [p for p in self.places.items if p["id"] in self.places.sensors()]
        if not watched or self.home is None:
            return None
        return {"seen": [self.places.label(p) for p in watched if self.presence.get(p["id"])],
                "watched": [self.places.label(p) for p in watched],
                "unknown": [self.places.label(p) for p in watched if self.presence.get(p["id"]) is None]}

    async def add_place(self, names) -> None:
        """A place where the robot stands now, facing the way it faces."""
        loc = self.localizer
        if loc is None or loc.pose is None or loc.state != "here":
            await self.send({"type": "notice", "text": "The robot doesn't know where it is on the map right now: "
                                                       "drive a little until the red dot shows, then save the spot."})
            return
        p = self.places.add(names, loc.pose)
        if isinstance(p, str):
            await self.send({"type": "notice", "text": p})
        else:
            log.info("places: saved a place (%d now)", len(self.places.items))
            await self.send({"type": "notice", "text": f"Saved “{p['names'][0]}” where the robot stands."})
        await self.send_places()

    def _trip_check(self, name: str, spoken: bool) -> tuple[dict | None, str | None]:
        """The place, and why a trip there can't start now (None: it can)."""
        from .places import DOCK
        p = self.places.find(name)
        why = (f"No place called “{name}”. Places: {', '.join(self.places.all_names())}." if p is None else
               f"Already busy: {self.current_trip()}. Finish or stop that first." if self.ai_driving() else
               "Doesn't know where it is on the map right now." if p["id"] != DOCK and (
                   self.localizer is None or self.localizer.state == "searching") else None)
        if why:
            log.info("places: a trip%s not started: %s", " (asked by voice)" if spoken else "",
                     "no such place" if p is None else "already driving" if self.ai_driving() else why)   # a task can be words
        return p, why

    async def ask_trip(self, name: str) -> tuple[bool, str]:
        """A trip asked for in the room: not started yet. Rocky asks, and on_heard starts it on a clear "yes".
        Returns (asking, the place's name) or (False, why not)."""
        from .rocky import CONFIRM_S
        p, why = self._trip_check(name, spoken=True)
        if why:
            return False, why
        self.pending_trip = (p["id"], time.monotonic() + CONFIRM_S)
        log.info("places: a trip asked by voice: waiting for a yes")
        return True, p["names"][0]

    async def _answer_trip(self, text: str) -> bool:
        """What was said right after "Going to the kitchen. OK?". True if it was the answer (handled here)."""
        from .rocky import yes_or_no
        pid, until = self.pending_trip
        if self.chat_task is not None and not self.chat_task.done():
            return False                                # he hasn't asked yet: not an answer
        self.pending_trip = None
        if time.monotonic() > until:
            log.info("places: no yes in time: trip not started")
            return False
        answer = yes_or_no(text)
        if answer is None:
            log.info("places: not a yes or no: trip not started")
            return False
        self.wake.wake_up()                             # still talking with him
        if answer == "no":
            log.info("places: heard no: trip not started")
            self.spawn(self.rocky_say("Okay. Stay here.", "neutral"))
            return True
        log.info("places: heard yes: trip confirmed")
        ok, _ = await self.go_to_place(pid, spoken=True)
        self.spawn(self.rocky_say("Yes yes. Going." if ok else "Bad. Cannot go now.", "happy" if ok else "sad"))
        return True

    async def go_to_place(self, name: str, spoken: bool = False) -> tuple[bool, str]:
        """Start going to a place by id or name (the dock: docking). Returns at once: (started, what to say)."""
        from .places import DOCK
        p, why = self._trip_check(name, spoken)
        if why:
            return False, why
        label = p["names"][0]

        async def trip():
            result = await (self.run_move("dock") if p["id"] == DOCK else self.run_move("goto", place=p))
            if spoken and self.rocky is not None and result is not None:     # asked in the room: say how it went
                ok = result == "arrived" or "docked: on the charger" in result
                await self.rocky_say(f"{label.capitalize()}. Here." if ok else f"Bad. Not get to {label}.",
                                     "happy" if ok else "sad")
        self.spawn(trip())
        return True, f"Started going to “{label}”."

    async def send_map_status(self, building: bool = False) -> None:
        self._map_status_at = time.monotonic()
        r = self.mapping
        await self.send({"type": "map_status", "recording": r is not None, "building": building,
                         "scans": len(r.rows) if r else 0, "metres": round(r.metres, 1) if r else 0,
                         "seconds": round(time.time() - r.started) if r else 0})

    async def send_map(self) -> None:
        """The latest map, for the driving pages (only theirs: it's a floor plan of someone's home)."""
        if self.maps_dir is None:
            return
        try:
            png = (self.maps_dir / "map.png").read_bytes()
            info = json.loads((self.maps_dir / "map.json").read_text())
        except (OSError, ValueError):
            return
        await self.send({"type": "map", "png": base64.b64encode(png).decode(), **info})

    async def look(self, camera: str) -> str | None:
        """Rocky's eyes: a fresh frame (base64 JPEG) from the head or floor camera, or None if it isn't sending.
        Shown on the robot screen, so the room knows a picture is going to the AI."""
        cam = "nav" if camera == "floor" else "head"
        for _ in range(15):
            f = self.frames.get(cam)
            if f and f[0] and time.monotonic() - f[1] < 1.5:
                await self.send({"type": "rocky_look", "camera": camera})
                return f[0]
            await asyncio.sleep(0.1)
        return None

    async def update_in_view(self, names: list[str]) -> None:
        """Tell the robot screen who it's looking at, only when that changes."""
        if names != self.in_view:
            self.in_view = names
            await self.send({"type": "seen", "names": names})

    async def greet(self, text: str) -> None:
        log.info("greeting someone")                  # never the words: they hold the person's name
        if self.rocky is not None:
            await self.rocky_say(text, "happy")      # Rocky's on: he says hello himself
            return
        wav = None
        if self.speaker is not None:
            try:
                wav = await asyncio.to_thread(self.speaker.wav, text)
            except Exception:
                log.exception("speech failed")
        await self.send({"type": "say", "text": text, "wav": base64.b64encode(wav).decode() if wav else None})

    # --- commands from driving pages ------------------------------------------------

    async def on_command(self, d: dict) -> None:
        kind = d.get("kind")
        if kind == "enroll":
            track = self.tracker.tracks.get(int(d.get("face_id", -1)))
            if track is None or not track.samples:
                await self.send({"type": "notice", "text": "That face is no longer in view. Try again."})
                return
            try:
                name = self.store.add(d.get("name", ""), list(track.samples), thumbnail_jpeg(track.face.crop))
            except ValueError as e:
                await self.send({"type": "notice", "text": str(e)})
                return
            track.votes.clear()
            track.votes.extend([name] * 3)      # recognise immediately, without waiting for votes
            self.greeter.last[name] = time.time()   # just introduced: don't greet in the same breath
            await self.send({"type": "notice", "text": f"Nice to meet you, {name}! I'll remember you."})
            await self.send_people()
        elif kind == "forget":
            if self.store.forget(d.get("name", "")):
                for t in self.tracker.tracks.values():
                    t.votes.clear()
                await self.update_in_view([n for n in self.in_view if n != d.get("name")])
                await self.send({"type": "notice", "text": f"Forgot {d.get('name')}."})
                await self.send_people()
        elif kind == "ai_task":
            await self.start_ai_task(str(d.get("task", "")).strip()[:300], d.get("model"))
        elif kind == "chat":
            await self.start_chat(str(d.get("text", "")).strip()[:500], d.get("sender"), login=d.get("login"))
        elif kind == "voice":
            if d.get("voice") == "fish" and self.fish is None:
                await self.send({"type": "notice", "text": "The Fish Audio voice isn't set up (no ~/beam-brain/fish.key)."})
            elif d.get("voice") in ("piper", "fish"):
                self.rocky_voice = d["voice"]
                self.save_settings()
            await self.send_voice()
        elif kind == "personality":
            await self.set_personality(d.get("text"), d.get("sender"), d.get("login"))
        elif kind == "map_start":
            await self.start_mapping()
        elif kind == "map_stop":
            await self.finish_mapping()
        elif kind == "dock":
            self.spawn(self.run_move("dock"))
        elif kind == "place_add":
            await self.add_place(d.get("names"))
        elif kind == "place_rename":
            why = self.places.rename(str(d.get("id")), d.get("names"))
            if why:
                await self.send({"type": "notice", "text": why})
            await self.send_places()
        elif kind == "place_sensors":
            why = self.places.set_sensors(str(d.get("id")), d.get("sensors"))
            if why:
                await self.send({"type": "notice", "text": why})
            elif self.home is None:
                await self.send({"type": "notice", "text": "Saved, but the brain has no Home Assistant connection "
                                                           "(--home-url), so it can't read them."})
            self._presence_poke.set()
            await self.send_places()
        elif kind == "place_remove":
            self.places.remove(str(d.get("id")))
            await self.send_places()
        elif kind == "goto":
            ok, text = await self.go_to_place(str(d.get("id", "")))
            if not ok:
                await self.send({"type": "notice", "text": text})
        elif kind == "ai_stop":
            if self.driver is not None:
                self.driver.abort("stopped from the driving page")
        elif kind == "pause":
            self.paused = bool(d.get("on"))
            self.tracker = Tracker()
            await self.send({"type": "status", "paused": self.paused})
            await self.send({"type": "faces", "faces": []})
            await self.update_in_view([])

    async def send_people(self) -> None:
        await self.send({"type": "people", "people": self.store.summary()})

    # --- Rocky: typed chat and speech in the room, spoken replies -----------------------

    def may_control_home(self, spoken: bool, login: str | None) -> bool:
        """Anyone in the room may ask Rocky to switch things (they can reach the switches anyway); a remote
        driver only if their Tailscale login (from the robot server, not the page) is on --home-drivers."""
        return spoken or (login is not None and login.lower() in self.home_drivers)

    async def start_chat(self, text: str, sender: str | None, spoken: bool = False, login: str | None = None) -> None:
        if self.rocky is None:
            await self.send({"type": "notice", "text": "Rocky isn't set up on the brain (no AI key)."})
            return
        if not text:
            return
        if self.chat_task is not None and not self.chat_task.done():
            if not spoken:
                await self.send({"type": "notice", "text": "Rocky is still answering. Wait a moment."})
            return
        self.chat_task = asyncio.create_task(self._chat(text, sender, spoken, self.may_control_home(spoken, login)))

    async def _chat(self, text: str, sender: str | None, spoken: bool = False, may_control: bool = False) -> None:
        await self.send({"type": "face", "emotion": "thinking"})
        self.thinking_about = text if spoken else None
        try:
            async with aiohttp.ClientSession() as session:
                emotion, words, actions = await self.rocky.reply(session, text, sender, list(self.in_view), spoken,
                                                                 may_control)
        except Exception as e:
            self.thinking_about = None
            # The error's type and HTTP status only: an AI service's error text can quote what was said.
            status = re.match(r"HTTP \d+", str(e))
            log.warning("Rocky's reply failed (%s%s)", type(e).__name__, f" {status[0]}" if status else "")
            await self.send({"type": "face", "emotion": "neutral"})
            await self.send({"type": "notice", "text": "Rocky couldn't think of a reply (the AI service failed)."})
            return
        self.thinking_about = None                       # from here on he's answering: too late to add words
        # Never the words themselves. Only whether a saved place's name was in what was asked, so a trip that
        # didn't start can be told apart: misheard (no name) or the AI not using its trip tool (a name, no go_to).
        named = ""
        if self.maps_dir is not None and self.places.items:
            heard = any(re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE) for n in self.places.all_names())
            named = "; a place name in what was asked" if heard else "; no place name in what was asked"
        log.info("Rocky answered [%s], %d words%s", emotion, len(words.split()), named)
        voice, tts_s = await self.rocky_say(words, emotion)
        log.info("voice: %s, sound after %s s", voice, tts_s)
        await self.send({"type": "rocky", "sender": sender, "heard": text, "emotion": emotion, "text": words,
                         "voice": voice, "tts_s": tts_s, "spoken": spoken, "actions": actions})

    async def rocky_say(self, words: str, emotion: str) -> tuple[str | None, float | None]:
        """Say it on the robot in Rocky's voice, with his face. Returns (which voice, seconds until it could
        start: Fish streams, so that's its first sound; Piper's whole clip is ready at once)."""
        if self.rocky_voice == "fish" and self.fish is not None:
            first_s = await self.fish_say(words, emotion)
            if first_s is not None:
                return "fish", first_s
        wav, voice, tts_s = await self.rocky_speech(words)
        if wav:
            from .speech import level
            wav = await asyncio.to_thread(level, wav)     # same loudness from either voice
        await self.send({"type": "say", "by": "rocky", "emotion": emotion, "text": words,
                         "wav": base64.b64encode(wav).decode() if wav else None})
        return voice, tts_s

    async def on_listen(self, on: bool) -> None:
        """The server starts or stops sending the microphone (it stops while anyone drives, or asleep)."""
        if on == self.listening:
            return
        self.listening = on
        log.info("listening %s", "on" if on else "off")
        if not on:
            self.pending_trip = None                    # someone drives, or asleep: the question is off
            if self.ears is not None:
                self.ears.reset()
            self.wake.sleep()
            if self.waves is not None:
                self.waves.reset()

    async def on_heard(self, text: str) -> None:
        """A finished spoken turn from the room (beambrain/ears.py)."""
        if not self.listening or self.rocky is None:
            return
        if self.ai_driving() and self.driver is not None and re.search(r"\bstop", text, re.IGNORECASE):
            # Nobody is at the controls while he listens: "stop" (no name needed) ends any AI driving at once.
            self.driver.abort("someone in the room said stop")
            log.info("heard 'stop' while the AI drove: stopped")
            return
        if self.pending_trip is not None and await self._answer_trip(text):
            return
        action, words = self.wake.heard(text)
        if action == "ignore":
            log.info("heard speech not meant for Rocky (%d words%s)", len(text.split()),   # never its words
                     ", his name not as 'hey Rocky' first" if self.wake.has_name(text) else "")
            return
        await self.send({"type": "rocky_awake", "seconds": self.wake.span})
        if self.wake_reply is not None:
            self.wake_reply.cancel()                   # the question came right after his name: just answer it
            self.wake_reply = None
        # Who's talking: the one person the face recognition knows in front of him, if exactly one.
        sender = self.in_view[0] if len(self.in_view) == 1 else None
        if action == "wake":
            # His name alone ("Hey Rocky," then a breath): people usually go on, and his own voice would pause
            # the microphone over their question. So wait a moment; only if nothing follows, "Question?".
            await self.send({"type": "face", "emotion": "curious"})
            self.wake_reply = asyncio.create_task(self._question_after(self.WAKE_WAIT_S))
        else:
            if self.thinking_about is not None and self.chat_task is not None and not self.chat_task.done():
                # More words while he's still thinking: the speaker only paused mid-sentence (it happens to about
                # one turn in six). Drop the half-done answer and answer the whole thing.
                words = f"{self.thinking_about} {words}"
                self.chat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.chat_task
                self.thinking_about = None
                log.info("more words before Rocky answered: answering them together")
            elif self.chat_task is not None and not self.chat_task.done():
                log.info("heard more words while Rocky was answering; dropped")
            await self.start_chat(words[:500], sender, spoken=True)

    WAKE_WAIT_S = 1.2

    async def on_wave(self) -> None:
        """Someone waved at him (beambrain/wave.py): the same as "hey Rocky" alone."""
        if self.chat_task is not None and not self.chat_task.done():
            return
        log.info("Rocky saw a wave")
        self.wake.wake_up(span=WAVE_AWAKE_S)
        await self.send({"type": "rocky_awake", "seconds": self.wake.span, "by": "wave"})
        await self.send({"type": "face", "emotion": "curious"})
        self.wake_reply = asyncio.create_task(self._question_after(0.3))

    async def _question_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.wake_reply = None
        await self.rocky_say("Question?", "curious")

    async def fish_say(self, words: str, emotion: str) -> float | None:
        """Rocky's Fish voice, sent to the robot as Fish makes it ("say" with a stream id, then "say_audio"
        pieces, then "say_audio" end), so he starts talking ~1 s sooner than with the whole clip. Returns the
        seconds until the first sound went out, or None if Fish failed before any (then Piper speaks instead)."""
        from .speech import STREAM_RATE, FishError, Leveller
        self.say_id += 1
        sid, t0, first_s = self.say_id, time.monotonic(), None
        leveller = Leveller()

        async def send(pieces: list[bytes]) -> None:
            nonlocal first_s
            for pcm in pieces:
                if first_s is None:
                    first_s = round(time.monotonic() - t0, 2)
                    await self.send({"type": "say", "by": "rocky", "emotion": emotion, "text": words,
                                     "stream": sid, "rate": STREAM_RATE})
                await self.send({"type": "say_audio", "stream": sid, "pcm": base64.b64encode(pcm).decode()})
        try:
            async with aiohttp.ClientSession() as session:
                async for pcm in self.fish.stream(session, words):
                    await send(leveller.feed(pcm))
            await send(leveller.flush())
        except FishError as e:
            if first_s is None:
                log.warning("Fish voice failed, using Piper: %s", e)
                await self.send({"type": "notice", "text": f"Fish voice failed, used Piper instead. {e}"})
                return None
            log.warning("Fish voice broke off: %s", e)          # he stops mid-sentence; rare
        if first_s is not None:
            await self.send({"type": "say_audio", "stream": sid, "end": True})
        return first_s

    async def rocky_speech(self, words: str) -> tuple[bytes | None, str | None, float | None]:
        """Rocky's Piper voice (local), as a whole WAV: also what he falls back to when Fish fails (no credit,
        network), so he never goes silent. Returns (wav, "piper", seconds it took)."""
        if self.speaker is None:
            return None, None, None
        t0 = time.monotonic()
        try:
            wav = await asyncio.to_thread(self.speaker.wav, words)
            return wav, "piper", round(time.monotonic() - t0, 2)
        except Exception:
            log.exception("speech failed")
            return None, None, None

    def load_settings(self) -> None:
        try:
            saved = json.loads(self.settings_path.read_text())
        except (AttributeError, OSError, ValueError):
            return
        voice = saved.get("voice")
        if voice == "piper" or (voice == "fish" and self.fish is not None):
            self.rocky_voice = voice
        mine = saved.get("personality")
        if self.rocky is not None and isinstance(mine, dict) and isinstance(mine.get("text"), str):
            self.rocky.personality = mine["text"][:MAX_PERSONALITY]
            self.personality_by = {"name": mine.get("by"), "at": mine.get("at")}

    def save_settings(self) -> None:
        if self.settings_path is None:
            return
        saved = {"voice": self.rocky_voice}
        if self.rocky is not None and self.rocky.personality != PERSONALITY:
            by = self.personality_by or {}
            saved["personality"] = {"text": self.rocky.personality, "by": by.get("name"), "at": by.get("at")}
        try:
            self.settings_path.write_text(json.dumps(saved, indent=1) + "\n")
        except OSError as e:
            log.warning("couldn't save Rocky's settings: %s", e)

    def may_edit_personality(self, login: str | None) -> bool:
        """Changing who Rocky is affects everyone, so with a list of household drivers (--home-drivers) only
        they may; without one, any driver may (like the voice switch)."""
        return not self.home_drivers or (login is not None and login.lower() in self.home_drivers)

    async def set_personality(self, text: str | None, sender: str | None, login: str | None) -> None:
        """text: the new personality, or empty/None for the default."""
        if self.rocky is None:
            return
        if not self.may_edit_personality(login):
            await self.send({"type": "notice", "text": "Only the household's drivers can change Rocky's personality."})
            await self.send_personality()
            return
        text = str(text or "").replace("\r\n", "\n").strip()[:MAX_PERSONALITY]
        self.rocky.personality = text or PERSONALITY
        self.rocky.history = []        # a fresh start: his old replies would pull him back to the old style
        changed = self.rocky.personality != PERSONALITY
        self.personality_by = {"name": sender or "a driver", "at": int(time.time())} if changed else None
        self.save_settings()
        log.info("Rocky's personality %s by a driver (%d characters)", "changed" if changed else "reset",
                 len(self.rocky.personality))
        who = sender or "A driver"
        await self.send({"type": "notice", "text": f"{who} changed Rocky's personality." if changed else
                         f"{who} put Rocky's personality back to the default."})
        await self.send_personality()

    async def send_personality(self) -> None:
        if self.rocky is None:
            return
        await self.send({"type": "rocky_personality", "text": self.rocky.personality,
                         "default": PERSONALITY, "max": MAX_PERSONALITY, **(self.personality_by or {})})

    async def send_voice(self) -> None:
        await self.send({"type": "rocky_voice", "voice": self.rocky_voice,
                         "available": ["piper"] + (["fish"] if self.fish is not None else [])})

    # --- AI driving --------------------------------------------------------------

    async def start_ai_task(self, task: str, model: str | None = None) -> None:
        from .driver import AIDriver
        from .llm import LLM
        if not self.ai:
            await self.send({"type": "notice", "text": "AI driving isn't set up on the brain (no API key)."})
            return
        if not task:
            await self.send({"type": "notice", "text": "Give the AI a task first."})
            return
        if self.driver_task is not None and not self.driver_task.done():
            await self.send({"type": "notice", "text": "The AI is already working on a task."})
            return
        # As in the bake-off that chose the models (bakeoff.py): the slower ones take over a minute on hard steps.
        llm = LLM(model or self.ai["model"], self.ai["key"], timeout_s=180)
        self.driver = AIDriver(self, llm, task, camera=self.camera)
        self.driver_task = asyncio.create_task(self.driver.run())


def main() -> None:
    ap = argparse.ArgumentParser(description="Beam+ brain: face recognition and greetings")
    ap.add_argument("--url", required=True, help="robot WebSocket, e.g. wss://beam.<tailnet>.ts.net/ws")
    # Better in the environment ($BEAM_BRAIN_KEY, $BEAM_TOKEN; the service reads them from /etc/beam-brain.env):
    # any program on this machine can read a command line.
    ap.add_argument("--key", default=os.environ.get("BEAM_BRAIN_KEY") or None,
                    help="BRAIN_KEY from the robot's beam.conf (better: set $BEAM_BRAIN_KEY)")
    ap.add_argument("--token", default=os.environ.get("BEAM_TOKEN") or None,
                    help="ACCESS_TOKEN, if the robot uses one (better: set $BEAM_TOKEN)")
    ap.add_argument("--models", default=str(Path.home() / "beam-brain" / "models"))
    ap.add_argument("--data", default=str(Path.home() / "beam-brain" / "data"))
    ap.add_argument("--cooldown", type=float, default=30 * 60, help="seconds between greetings per person")
    ap.add_argument("--ai-model", default="hf:deepseek-ai/DeepSeek-V4.1-Flash",
                    help="default model for AI driving tasks (needs ~/beam-brain/ai.key)")
    ap.add_argument("--fish-voice", default=FISH_ROCKY,
                    help="Fish Audio voice id for Rocky (needs ~/beam-brain/fish.key)")
    ap.add_argument("--fish-model", default="", help='Fish Audio model, e.g. "s2.1-pro" (default: Fish\'s default)')
    ap.add_argument("--no-ears", action="store_true", help="never listen to the robot's microphone")
    ap.add_argument("--no-wave", action="store_true", help="a wave doesn't wake Rocky (only \"hey Rocky\")")
    ap.add_argument("--no-eyes",action="store_true", help="Rocky never sends camera pictures to the AI")
    ap.add_argument("--home-url", default="",
                    help="Home Assistant's MCP endpoint, e.g. http://<ha>:8123/api/mcp (needs ~/beam-brain/ha.key)")
    ap.add_argument("--home-drivers", default="",
                    help="comma-separated Tailscale logins whose typed chats may control the home "
                         "(speech in the room always may)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if not args.key:
        ap.error("the brain key is needed: set $BEAM_BRAIN_KEY (or --key)")
    for name in ("BEAM_BRAIN_KEY", "BEAM_TOKEN"):     # read: don't hand them on to child programs
        os.environ.pop(name, None)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    url = f"{args.url}?role=brain&key={args.key}" + (f"&token={args.token}" if args.token else "")
    ai = None
    key_file = Path(args.data).parent / "ai.key"
    if os.environ.get("SYNTHETIC_API_KEY") or key_file.exists():
        from .llm import load_key
        ai = {"key": load_key(key_file), "model": args.ai_model}
        log.info("AI driving and Rocky enabled (model %s)", args.ai_model)
    greeter = Greeter(args.cooldown)
    if ai:
        from .rocky import GREETINGS
        greeter.phrases = GREETINGS                 # Rocky says hello in his own words
    brain = Brain(FaceEngine(args.models), PeopleStore(Path(args.data) / "people.json"),
                  Speaker(args.models), greeter, ai)
    brain.settings_path = Path(args.data) / "rocky.json"
    brain.snapshot_dir = str(Path(args.data).parent / "calibration")
    brain.maps_dir = Path(args.data).parent / "maps"
    if (Path(args.data) / "cameras.json").exists():          # written by `python3 -m beambrain.calibrate`
        from .geometry import CameraModel
        brain.camera = CameraModel.load(Path(args.data) / "cameras.json")
        log.info("using the robot's measured cameras (cameras.json)")
    # The household drivers also decide who may change Rocky's personality, so this holds with or without
    # Home Assistant.
    brain.home_drivers = {s.strip().lower() for s in args.home_drivers.split(",") if s.strip()}
    if ai:
        from .llm import LLM
        from .rocky import Rocky
        home = None
        if args.home_url:
            from .home import HomeAssistant, load_token
            token = load_token(Path(args.data).parent / "ha.key")
            if token:
                home = HomeAssistant(args.home_url, token)
                brain.home = home                    # also for room presence (places with sensors)
                log.info("Rocky can control Home Assistant (room speech, and %d allowed driver(s))",
                         len(brain.home_drivers))
            else:
                log.warning("--home-url given but no ~/beam-brain/ha.key; no home control")
        brain.rocky = Rocky(LLM(ai["model"], ai["key"]), home=home, eyes=None if args.no_eyes else brain.look,
                            trips=brain.trips())
        fish_key = Path(args.data).parent / "fish.key"
        if fish_key.exists() and fish_key.read_text().strip():
            from .speech import FishVoice
            brain.fish = FishVoice(fish_key.read_text().strip(), args.fish_voice, args.fish_model)
            log.info("Fish Audio voice available for Rocky (voice %s)", args.fish_voice)
        brain.load_settings()
        log.info("Rocky's voice: %s", brain.rocky_voice)
        if not args.no_ears:
            from .ears import Ears
            brain.ears = Ears(args.models, brain.on_heard)
            if not args.no_wave:
                brain.waves = WaveDetector()
    log.info("knows %d people", len(brain.store.people))
    try:
        asyncio.run(brain.run(url))
    except KeyboardInterrupt:
        pass
