"""Text to speech as WAV bytes: Piper (natural, local) if available, else espeak-ng; optionally Fish Audio (cloud).

Fish Audio is for Rocky's voice: a community-made clone of the character, used for personal fun. It needs an API
key in ~/beam-brain/fish.key (and API credit at fish.audio); the reply text goes to Fish's servers. Fish's sound
is streamed (raw PCM, played on the robot as it arrives), so he starts talking before the whole reply is made.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess
import wave
from pathlib import Path

log = logging.getLogger("beambrain.speech")
VOICE = "en_US-lessac-medium.onnx"
FISH_URL = "https://api.fish.audio/v1/tts"
# desk-robot's Rocky voice on fish.audio (a fan clone; it may disappear one day). --fish-voice changes it.
FISH_ROCKY = "6dd07916890445e59c5f019ad0fc7879"


class Speaker:
    def __init__(self, models_dir: str | Path):
        self.voice = None
        path = Path(models_dir) / VOICE
        try:
            from piper import PiperVoice
            if path.exists():
                self.voice = PiperVoice.load(str(path))
                log.info("speech: Piper voice %s", VOICE)
        except ImportError:
            pass
        if self.voice is None:
            if shutil.which("espeak-ng") is None:
                log.warning("speech: neither Piper nor espeak-ng available; the robot will only show text")
            else:
                log.info("speech: espeak-ng (install piper-tts and %s for a nicer voice)", VOICE)

    def wav(self, text: str) -> bytes | None:
        if self.voice is not None:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                self.voice.synthesize_wav(text, w)
            return buf.getvalue()
        if shutil.which("espeak-ng"):
            return subprocess.run(["espeak-ng", "--stdout", text], capture_output=True, check=True).stdout
        return None


class FishError(Exception):
    """Fish Audio refused or failed; the message is safe to show (never contains the key)."""


class FishVoice:
    def __init__(self, key: str, voice_id: str = FISH_ROCKY, model: str = "", url: str = FISH_URL):
        self.key, self.voice_id, self.model, self.url = key, voice_id, model, url

    async def stream(self, session, text: str):
        """Rocky's voice as raw 16-bit mono PCM at STREAM_RATE, yielded as Fish makes it (its first sound comes
        after ~0.2 s, the whole reply after ~1-1.5 s). Raises FishError before the first piece if Fish refuses."""
        import aiohttp
        body = {"text": text, "reference_id": self.voice_id, "format": "pcm", "sample_rate": STREAM_RATE,
                "latency": "balanced", "temperature": 0.4, "top_p": 0.6}     # desk-robot's steady settings
        headers = {"Authorization": f"Bearer {self.key}"}
        if self.model:
            headers["model"] = self.model           # e.g. "s2.1-pro"; without it Fish uses its default
        try:
            async with session.post(self.url, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30, sock_read=10)) as r:
                if r.status != 200:
                    try:
                        why = (await r.json(content_type=None)).get("message", "")
                    except ValueError:
                        why = ""
                    raise FishError(f"Fish Audio said {r.status}{': ' + why if why else ''}")
                odd = b""
                async for piece in r.content.iter_any():
                    piece = odd + piece
                    cut = len(piece) // 2 * 2                    # whole 16-bit samples only
                    odd = piece[cut:]
                    if cut:
                        yield piece[:cut]
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise FishError(f"Fish Audio not reachable ({type(e).__name__})") from None


STREAM_RATE = 24000


class Leveller:
    """level() for a stream: holds back the first `lead_s` of sound to judge its loudness (Fish makes sound much
    faster than it plays, so that costs ~0.1 s), then passes everything on at that gain, `piece_s` at a time."""

    def __init__(self, rate: int = STREAM_RATE, lead_s: float = 0.5, piece_s: float = 0.25):
        self.lead, self.piece = int(rate * lead_s) * 2, int(rate * piece_s) * 2
        self.buf, self.gain = b"", None

    def feed(self, pcm: bytes) -> list[bytes]:
        self.buf += pcm
        if self.gain is None:
            if len(self.buf) < self.lead:
                return []
            self.gain = loudness_gain(self.buf)
        out = []
        while len(self.buf) >= self.piece:
            out.append(apply_gain(self.buf[:self.piece], self.gain))
            self.buf = self.buf[self.piece:]
        return out

    def flush(self) -> list[bytes]:
        if not self.buf:
            return []
        if self.gain is None:
            self.gain = loudness_gain(self.buf)
        out, self.buf = [apply_gain(self.buf, self.gain)], b""
        return out


def loudness_gain(pcm: bytes, target_rms: float = 0.18, max_gain: float = 4.0) -> float:
    """How much to amplify 16-bit PCM so its speech is about as loud as Piper's (Fish's Rocky is ~9 dB quieter)."""
    import numpy as np
    x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    speech = x[np.abs(x) > 0.01]                       # loudness of the words, not the pauses
    if speech.size == 0:
        return 1.0
    return min(max_gain, target_rms / float(np.sqrt(np.mean(speech ** 2))))


def apply_gain(pcm: bytes, gain: float, knee: float = 0.7) -> bytes:
    """Amplify 16-bit PCM; peaks above `knee` are rounded off softly instead of clipping."""
    import numpy as np
    x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768 * gain
    over = np.abs(x) > knee
    x[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((np.abs(x[over]) - knee) / (1 - knee)))
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def level(data: bytes) -> bytes:
    """Bring a 16-bit WAV's speech to about the same loudness whichever voice made it."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return data                                    # not a plain WAV: play it as it is
    if params.sampwidth != 2 or not frames:
        return data
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams(params)
        w.writeframes(apply_gain(frames, loudness_gain(frames)))
    return buf.getvalue()
