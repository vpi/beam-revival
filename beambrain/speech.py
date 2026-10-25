"""Text to speech as WAV bytes: Piper (natural, local) if available, else espeak-ng."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import wave
from pathlib import Path

log = logging.getLogger("beambrain.speech")
VOICE = "en_US-lessac-medium.onnx"


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
