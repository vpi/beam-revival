"""Ears: the robot's microphone -> whole spoken turns -> text, all on the brain host (nothing goes to the cloud).

  robot page (16 kHz mono PCM, only while the server allows listening) -> Ears.feed()
    -> Silero VAD finds speech, 32 ms at a time
    -> Smart Turn judges, at each pause, whether the person sounds finished or is only pausing
    -> faster-whisper turns the finished turn into text -> on_text(text) on the event loop

The pipeline (models, thresholds, the wake phrases) is ported from desk-robot by Casey
(https://github.com/cgro00/desk-robot, MIT License, Copyright (c) 2026 Casey). Silero VAD (MIT) and Smart Turn
(BSD-2-Clause) download once into the models folder, pinned to fixed revisions and checked against their SHA-256.
Audio is never written to disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger("beambrain.ears")

SAMPLE_RATE = 16_000
VAD_CHUNK = 512                    # samples per VAD step (32 ms), fixed by the Silero model
CHUNK_S = VAD_CHUNK / SAMPLE_RATE
_VAD_CONTEXT = 64

# To upgrade a model: change the revision in the URL, download it, check it yourself, update the hash.
MODELS = {
    "silero_vad.onnx": (
        "https://raw.githubusercontent.com/snakers4/silero-vad/867c2aa69264/src/silero_vad/data/silero_vad.onnx",
        "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    "smart-turn-v3.2-cpu.onnx": (
        "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfd/smart-turn-v3.2-cpu.onnx",
        "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"),
}
STT_MODEL = "base.en"                                       # ~0.3 s a sentence on the VM; small.en hears better, slower
STT_REVISION = "3d3d5dee26484f91867d81cb899cfcf72b96be6c"   # Systran/faster-whisper-base.en, pinned
STT_PROMPT = "Hey Rocky. Rocky is a robot."                 # without it "Rocky" often comes out as "right"

VAD_THRESHOLD = 0.4        # how sure the VAD must be that a chunk is speech
TURN_PAUSE_S = 0.2         # a pause this long makes Smart Turn judge the turn...
TURN_RECHECK_S = 0.6       # ...and again this often while the pause goes on
TURN_THRESHOLD = 0.5       # how sure Smart Turn must be that you've finished
TURN_MAX_SILENCE_S = 2.5   # finished anyway after this much quiet
MIN_TURN_S = 0.4           # shorter: a click or a cough
MAX_TURN_S = 15.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def fetch_model(models_dir: Path, name: str) -> Path:
    """The model file, downloaded once and checked; a file that doesn't match its hash is fetched again."""
    url, sha = MODELS[name]
    path = Path(models_dir) / name
    if path.is_file() and _sha256(path) == sha:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s (one time)", name)
    tmp = path.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    if _sha256(tmp) != sha:
        tmp.unlink()
        raise RuntimeError(f"{name} from {url} doesn't match its expected SHA-256: refusing to use it")
    tmp.rename(path)
    return path


def _session(path: Path, threads: int = 1):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.inter_op_num_threads, so.intra_op_num_threads, so.log_severity_level = 1, threads, 3
    return ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])


class SileroVAD:
    """32 ms chunks in, in order; speech probability (0..1) out."""

    def __init__(self, models_dir: Path):
        self.session = _session(fetch_model(models_dir, "silero_vad.onnx"))
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _VAD_CONTEXT), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self._context, chunk.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self.session.run(None, {"input": x, "state": self._state,
                                                    "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
        self._context = x[:, -_VAD_CONTEXT:]
        return float(out[0, 0])


class SmartTurn:
    """The current turn's audio in; how likely it is that the speaker has finished (looks at the last 8 s)."""

    WINDOW = 8 * SAMPLE_RATE

    def __init__(self, models_dir: Path):
        self.session = _session(fetch_model(models_dir, "smart-turn-v3.2-cpu.onnx"), threads=2)

    def __call__(self, audio: np.ndarray) -> float:
        from .whisper_features import compute_whisper_log_mel_features
        audio = audio[-self.WINDOW:] if len(audio) > self.WINDOW else np.pad(audio, (self.WINDOW - len(audio), 0))
        feats = compute_whisper_log_mel_features(audio.astype(np.float32), do_normalize=True)
        return float(self.session.run(None, {"input_features": feats[np.newaxis]})[0][0, 0])


class Segmenter:
    """Float32 audio in (any block size); whole turns out. A turn starts at the first speech chunk (with a short
    pre-roll, so the first syllable isn't clipped) and ends when the speaker sounds finished."""

    def __init__(self, vad, judge=None):
        self.vad, self.judge = vad, judge
        self.pre_roll: deque[np.ndarray] = deque(maxlen=int(0.3 / CHUNK_S))
        self.pause_chunks = int(TURN_PAUSE_S / CHUNK_S)
        self.recheck_chunks = int(TURN_RECHECK_S / CHUNK_S)
        self.max_quiet = int(TURN_MAX_SILENCE_S / CHUNK_S)
        self.reset()

    def reset(self) -> None:
        self.vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self.pre_roll.clear()
        self.current: list[np.ndarray] = []
        self.speaking = False
        self.quiet = 0
        self.next_check = 0
        self.why = ""

    def push(self, block: np.ndarray) -> np.ndarray | None:
        self._pending = np.concatenate([self._pending, block.astype(np.float32)])
        turn = None
        while len(self._pending) >= VAD_CHUNK:
            chunk, self._pending = self._pending[:VAD_CHUNK], self._pending[VAD_CHUNK:]
            got = self._step(chunk)
            turn = turn if turn is not None else got
        return turn

    def _step(self, chunk: np.ndarray) -> np.ndarray | None:
        p = self.vad(chunk)
        # Once a voice has started, keep its softer syllables (Silero's separate start/stop cutoffs).
        loud = p > (VAD_THRESHOLD - 0.15 if self.speaking else VAD_THRESHOLD)
        if not self.speaking:
            self.pre_roll.append(chunk)
            if loud:
                self.speaking, self.current, self.quiet = True, list(self.pre_roll), 0
                self.next_check = self.pause_chunks
            return None
        self.current.append(chunk)
        if loud:
            self.quiet, self.next_check = 0, self.pause_chunks
            if len(self.current) * CHUNK_S >= MAX_TURN_S:
                return self._finish("longest turn")
            return None
        self.quiet += 1
        if self.quiet >= self.max_quiet:
            return self._finish(f"{self.quiet * CHUNK_S:.1f} s of quiet")
        if self.quiet >= self.next_check:
            self.next_check = self.quiet + self.recheck_chunks
            if self.judge is None:
                return self._finish("pause")
            done = self.judge(np.concatenate(self.current))
            if done > TURN_THRESHOLD:
                return self._finish(f"sounds finished (p={done:.2f})")
        return None

    def _finish(self, why: str) -> np.ndarray | None:
        turn, quiet = self.current, self.quiet
        self.speaking, self.current, self.quiet, self.why = False, [], 0, why
        self.pre_roll.clear()
        if (len(turn) - quiet) * CHUNK_S < MIN_TURN_S:
            return None
        return np.concatenate(turn)


class Transcriber:
    def __init__(self, models_dir: Path):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(STT_MODEL, device="cpu", compute_type="int8", revision=STT_REVISION,
                                  download_root=str(Path(models_dir) / "whisper"))

    def __call__(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                            initial_prompt=STT_PROMPT)
        return " ".join(s.text.strip() for s in segments).strip()


# --- the wake word -----------------------------------------------------------------------

WAKE_PHRASES = ["hey rocky", "hey rocket", "hey rocking", "hi rocky", "a rocky", "hey ricky", "okay rocky"]
_WAKE_RE = re.compile(r"\b(?:" + "|".join(r"\W+".join(map(re.escape, p.split())) for p in WAKE_PHRASES)
                      + r")\b[\s,.!?:;-]*", re.IGNORECASE)
AWAKE_S = 60.0       # after "hey Rocky", no need to say his name again for this long


class Wake:
    """Whether something heard is meant for Rocky. Returns ("ignore", ""), ("wake", "") for his name alone,
    or ("chat", words to answer). His name alone, without "hey", doesn't wake him (too easy to say by chance)."""

    def __init__(self, awake_s: float = AWAKE_S):
        self.awake_s, self.until = awake_s, 0.0

    @property
    def awake(self) -> bool:
        return time.monotonic() < self.until

    def heard(self, text: str, now: float | None = None) -> tuple[str, str]:
        now = time.monotonic() if now is None else now
        m = _WAKE_RE.search(text)
        if m:
            self.until = now + self.awake_s
            rest = text[m.end():].strip()
            return ("chat", rest) if re.search(r"\w", rest) else ("wake", "")
        if now < self.until and re.search(r"\w", text):
            self.until = now + self.awake_s
            return "chat", text.strip()
        return "ignore", ""

    def sleep(self) -> None:
        self.until = 0.0


# --- the whole thing, on its own thread -------------------------------------------------

class Ears:
    """Feed it PCM from the event loop; it calls on_text(text) back on that loop for every finished turn."""

    def __init__(self, models_dir: Path, on_text, loop: asyncio.AbstractEventLoop | None = None,
                 segmenter: Segmenter | None = None, transcriber=None):
        self.models_dir, self.on_text = Path(models_dir), on_text
        self.loop = loop
        self.segmenter, self.transcriber = segmenter, transcriber
        self.q: queue.Queue = queue.Queue(maxsize=200)        # ~20 s of audio; dropped beyond that
        self.thread = threading.Thread(target=self._run, name="ears", daemon=True)

    def load(self) -> None:
        """Load (the first time: download) the models. Slow: run it off the event loop."""
        if self.segmenter is None:
            self.segmenter = Segmenter(SileroVAD(self.models_dir), SmartTurn(self.models_dir))
        if self.transcriber is None:
            self.transcriber = Transcriber(self.models_dir)

    def start(self) -> None:
        self.loop = self.loop or asyncio.get_running_loop()
        self.thread.start()

    def feed(self, pcm: bytes) -> None:
        """16-bit little-endian mono PCM at 16 kHz."""
        try:
            self.q.put_nowait(pcm)
        except queue.Full:
            pass

    def reset(self) -> None:
        """Listening stopped (a driver connected, the robot went away): drop any half-heard turn."""
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        last = 0.0
        while True:
            pcm = self.q.get()
            if pcm is None:
                self.segmenter.reset()
                continue
            now = time.monotonic()
            if now - last > 1.0:
                # A gap: the robot paused its mic while Rocky talked. Don't glue old sound onto new.
                self.segmenter.reset()
            last = now
            turn = self.segmenter.push(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)
            if turn is None:
                continue
            t0 = time.monotonic()
            try:
                text = self.transcriber(turn)
            except Exception:
                log.exception("transcription failed")
                continue
            log.info("heard %.1f s of speech (%s), transcribed in %.2f s", len(turn) / SAMPLE_RATE,
                     self.segmenter.why, time.monotonic() - t0)
            if text:
                asyncio.run_coroutine_threadsafe(self.on_text(text), self.loop)
