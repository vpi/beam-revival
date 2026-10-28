"""Text to speech as WAV bytes: Piper (natural, local) if available, else espeak-ng; optionally Fish Audio (cloud).

Fish Audio is for Rocky's voice: a community-made clone of the character, used for personal fun. It needs an API
key in ~/beam-brain/fish.key (and API credit at fish.audio); the reply text goes to Fish's servers.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import urllib.error
import urllib.request
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

    def wav(self, text: str) -> bytes:
        body = {"text": text, "reference_id": self.voice_id, "format": "wav", "sample_rate": 24000,
                "latency": "balanced", "temperature": 0.4, "top_p": 0.6}     # desk-robot's steady settings
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if self.model:
            headers["model"] = self.model           # e.g. "s2.1-pro"; without it Fish uses its default
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("message", "")
            except ValueError:
                why = ""
            raise FishError(f"Fish Audio said {e.code}{': ' + why if why else ''}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FishError(f"Fish Audio not reachable ({e})") from None
        if not data.startswith(b"RIFF"):
            raise FishError("Fish Audio sent something that isn't a WAV file")
        return fix_wav_sizes(data)


def level(data: bytes, target_rms: float = 0.18, max_gain: float = 4.0, knee: float = 0.7) -> bytes:
    """Bring a 16-bit WAV's speech to about the same loudness whichever voice made it (Fish's Rocky is ~9 dB
    quieter than Piper). Peaks above `knee` are rounded off softly instead of clipping."""
    import numpy as np
    try:
        with wave.open(io.BytesIO(data)) as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return data                                    # not a plain WAV: play it as it is
    if params.sampwidth != 2 or not frames:
        return data
    x = np.frombuffer(frames, np.int16).astype(np.float32) / 32768
    speech = x[np.abs(x) > 0.01]                       # loudness of the words, not the pauses
    if speech.size == 0:
        return data
    x = x * min(max_gain, target_rms / float(np.sqrt(np.mean(speech ** 2))))
    over = np.abs(x) > knee
    x[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((np.abs(x[over]) - knee) / (1 - knee)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams(params)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def fix_wav_sizes(data: bytes) -> bytes:
    """Fish streams its WAV, so the header's sizes are placeholders (0xffffff..: "25 hours long"). Write the real
    ones, so players know the clip's length and when it ends."""
    out = bytearray(data)
    out[4:8] = (len(out) - 8).to_bytes(4, "little")
    i = 12
    while i + 8 <= len(out):
        if out[i:i + 4] == b"data":
            out[i + 4:i + 8] = (len(out) - i - 8).to_bytes(4, "little")
            break
        i += 8 + int.from_bytes(out[i + 4:i + 8], "little")
    return bytes(out)
