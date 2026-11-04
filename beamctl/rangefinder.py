"""An ultrasonic distance sensor (DFRobot A02YYUW style) on a USB serial adapter; see docs/sensors.md.

The sensor only talks: about 10 times a second, at 9600 baud, 4 bytes: 0xFF, the distance in millimetres
(high byte, low byte) and a checksum (the low 8 bits of the first three). It's found by its adapter's name,
plugged in or unplugged at any time, and never written to.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import time

from .serialport import SerialPort

log = logging.getLogger("beam.range")

# A plain CP2102. Not "CP2102N": the RPLIDAR C1 has one of those.
AUTO = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART*"
BAUD = 9600
# What the A02 measures (3-450 cm). 0 is its "no echo" and beyond MAX_MM is nothing in range; a small number
# under MIN_MM is something inside its blind zone, so it counts as touching (0 mm), never as a clear way.
MIN_MM, MAX_MM = 30, 4500
STALE_S = 0.5                  # no frame for this long: the reading is no longer trusted
RETRY_S = 2.0                  # look for the sensor this often while it isn't there


def parse_frames(buf: bytes) -> tuple[list[int], bytes]:
    """Distances (mm) in buf, and the bytes left over for next time. Bad frames are skipped a byte at a time."""
    out, i = [], 0
    while len(buf) - i >= 4:
        if buf[i] == 0xFF and (0xFF + buf[i + 1] + buf[i + 2]) & 0xFF == buf[i + 3]:
            out.append(buf[i + 1] << 8 | buf[i + 2])
            i += 4
        else:
            i += 1
    return out, buf[i:]


def distance_mm(v: int) -> int | None:
    """A frame's value as a distance: None = nothing in range, 0 = closer than the sensor can measure."""
    if v == 0 or v > MAX_MM:
        return None
    return v if v >= MIN_MM else 0


class RangeSensor:
    def __init__(self, device: str = "auto"):
        self.device = device                   # a serial port path, or "auto" (the first plain CP2102)
        self.port: SerialPort | None = None
        # A sensor named on the command line must be there; one found automatically, once it has been seen.
        # Missing after that, it's "lost" (unplugged, a USB reset): the obstacle stop can't see what it saw.
        self.expected = bool(device) and device != "auto"
        self.mm: int | None = None             # the latest distance; None = nothing in range
        self.at = 0.0                          # when it came
        self.frames = 0
        self._buf = b""
        self._last_error: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="range-sensor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.port is not None:
            self.port.close()

    def _find(self) -> str | None:
        if self.device != "auto":
            return self.device
        found = sorted(glob.glob(AUTO))
        return found[0] if found else None

    async def _run(self) -> None:
        while True:
            if self.port is None or not self.port.is_open:
                path = self._find()
                if path is not None:
                    self.port = SerialPort(path, self._on_data, self._on_lost, baud=BAUD)
                    self._buf = b""
                    try:
                        self.port.open()
                        log.info("distance sensor at %s", path)
                        self.expected = True
                    except OSError as e:
                        if str(e) != self._last_error:       # once, not every retry
                            log.warning("cannot open distance sensor %s: %s", path, e)
                        self._last_error = str(e)
                        self.port = None
            await asyncio.sleep(RETRY_S)

    def _on_data(self, data: bytes) -> None:
        values, self._buf = parse_frames(self._buf + data)
        if values:
            self.mm = distance_mm(values[-1])
            self.at = time.monotonic()
            self.frames += len(values)

    def _on_lost(self, exc: Exception) -> None:
        log.warning("distance sensor gone: %s", exc)
        self.mm = None

    @property
    def present(self) -> bool:
        return self.port is not None and self.port.is_open

    @property
    def lost(self) -> bool:
        """It was there (or was named) and isn't now. Stays so until it's back."""
        return self.expected and not self.present

    def snapshot(self) -> dict | None:
        """None without a sensor; else the distance in metres (None = nothing in range) and whether it's fresh."""
        if not self.present:
            return None
        fresh = time.monotonic() - self.at < STALE_S
        return {"m": round(self.mm / 1000, 3) if fresh and self.mm is not None else None, "fresh": fresh}
