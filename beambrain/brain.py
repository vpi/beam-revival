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
import time
from pathlib import Path

import aiohttp
import cv2
import numpy as np

from .ears import Wake
from .faces import FaceEngine, thumbnail_jpeg
from .greeter import Greeter
from .people import PeopleStore
from .rocky import MAX_PERSONALITY, PERSONALITY
from .speech import FISH_ROCKY, Speaker
from .tracker import Tracker
from .wave import WaveDetector

log = logging.getLogger("beambrain")


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
        self.home_drivers: set[str] = set()  # Tailscale logins whose typed chats may control the home

    # --- connection ------------------------------------------------------------

    async def run(self, url: str, stop: asyncio.Event | None = None) -> None:
        """Connect and serve until stopped, reconnecting with backoff."""
        stop = stop or asyncio.Event()
        if self.ears is not None and not self.ears.thread.is_alive():
            try:
                await asyncio.to_thread(self.ears.load)
                self.ears.start()
                log.info("ears ready (speech recognition on this computer)")
            except Exception:
                log.exception("speech recognition unavailable; Rocky won't listen")
                self.ears = None
        delay = 1.0
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                try:
                    async with session.ws_connect(url, heartbeat=10, max_msg_size=8 * 2**20) as ws:
                        log.info("connected to robot")
                        delay = 1.0
                        await self.serve(ws, stop)
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
                m = json.loads(msg.data)
                kind = m.get("type")
                if kind == "listen":
                    await self.on_listen(bool(m.get("on")))
                elif kind == "frame":
                    self.on_frame(m)
                elif kind == "telemetry":
                    self.telemetry = m
                    for listener in list(self.telemetry_listeners):
                        listener(m)
                elif kind == "denied":
                    self.human_took_over.set()      # a person is driving: the AI must stand down
                elif kind == "brain_cmd":
                    await self.on_command(m.get("data") or {})
        finally:
            stopper.cancel()
            self.ws = None
            await self.on_listen(False)
            if self.driver is not None:
                self.driver.abort("lost connection to the robot")

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
        asyncio.create_task(self._process(m))

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
                        asyncio.create_task(self.greet(text))   # Fish takes ~1 s; keep watching meanwhile
        except Exception:
            log.exception("frame processing failed")
        finally:
            self._busy = False

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
        log.info("greeting: %s", text)
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
            log.warning("Rocky's reply failed: %s", e)
            await self.send({"type": "face", "emotion": "neutral"})
            await self.send({"type": "notice", "text": "Rocky couldn't think of a reply (the AI service failed)."})
            return
        self.thinking_about = None                       # from here on he's answering: too late to add words
        log.info("Rocky [%s]: %s", emotion, words)
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
            if self.ears is not None:
                self.ears.reset()
            self.wake.sleep()
            if self.waves is not None:
                self.waves.reset()

    async def on_heard(self, text: str) -> None:
        """A finished spoken turn from the room (beambrain/ears.py)."""
        if not self.listening or self.rocky is None:
            return
        action, words = self.wake.heard(text)
        if action == "ignore":
            log.info("heard speech not meant for Rocky (%d words%s)", len(text.split()),   # never its words
                     ", his name without 'hey'" if self.wake.has_name(text) else "")
            return
        await self.send({"type": "rocky_awake", "seconds": self.wake.awake_s})
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
        self.wake.wake_up()
        await self.send({"type": "rocky_awake", "seconds": self.wake.awake_s, "by": "wave"})
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
        llm = LLM(model or self.ai["model"], self.ai["key"])
        self.driver = AIDriver(self, llm, task)
        self.driver_task = asyncio.create_task(self.driver.run())


def main() -> None:
    ap = argparse.ArgumentParser(description="Beam+ brain: face recognition and greetings")
    ap.add_argument("--url", required=True, help="robot WebSocket, e.g. wss://beam.<tailnet>.ts.net/ws")
    ap.add_argument("--key", required=True, help="BRAIN_KEY from the robot's beam.conf")
    ap.add_argument("--token", help="ACCESS_TOKEN, if the robot uses one")
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
    if ai:
        from .llm import LLM
        from .rocky import Rocky
        home = None
        if args.home_url:
            from .home import HomeAssistant, load_token
            token = load_token(Path(args.data).parent / "ha.key")
            if token:
                home = HomeAssistant(args.home_url, token)
                brain.home_drivers = {s.strip().lower() for s in args.home_drivers.split(",") if s.strip()}
                log.info("Rocky can control Home Assistant (room speech, and %d allowed driver(s))",
                         len(brain.home_drivers))
            else:
                log.warning("--home-url given but no ~/beam-brain/ha.key; no home control")
        brain.rocky = Rocky(LLM(ai["model"], ai["key"]), home=home, eyes=None if args.no_eyes else brain.look)
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
