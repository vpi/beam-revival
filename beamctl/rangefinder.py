"""An ultrasonic distance sensor (DFRobot A02YYUW style) on a USB serial adapter; see docs/sensors.md.

The sensor only talks: about 10 times a second, at 9600 baud, 4 bytes: 0xFF, the distance in millimetres
(high byte, low byte) and a checksum (the low 8 bits of the first three). It's found by its adapter's name,
plugged in or unplugged at any time, and never written to. A port is used only once good frames come from it:
other things, like a robot arm, use the same USB serial chip.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from .serialport import PortPicker, SerialPort

log = logging.getLogger("beam.range")

# A plain CP2102. Not "CP2102N": the RPLIDAR C1 has one of those.
AUTO = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART*"
BAUD = 9600
# What the A02 measures (3-450 cm). 0 is its "no echo" and beyond MAX_MM is nothing in range; a small number
# under MIN_MM is something inside its blind zone, so it counts as touching (0 mm), never as a clear way.
MIN_MM, MAX_MM = 30, 4500
STALE_S = 0.5                  # no frame for this long: the reading is no longer trusted
RETRY_S = 2.0                  # look for the sensor this often while it isn't there
IDENTIFY_S = 1.5               # a port must send IDENTIFY_FRAMES good frames within this to count as the sensor
IDENTIFY_FRAMES = 2


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
    WHAT = "distance sensor"                   # for the log
    BAUD: int | None = BAUD                    # None: a USB port of its own (CDC), no baud rate to set

    def __init__(self, device: str = "auto", angle: float = 0.0, claimed: set[str] | None = None):
        """angle: which way it faces, degrees clockwise from straight ahead (for the driving page's view).
        claimed: ports other sensors in this server use, shared by them all, so two don't read one port (the
        port's lock only keeps other programs out)."""
        self.device = device                   # a serial port path, or "auto" (a plain CP2102 sending its frames)
        self.angle = angle
        self.claimed = claimed if claimed is not None else set()
        self.path: str | None = None           # the port it's on, once found
        self.picker = PortPicker(device or "auto", AUTO)
        self.port: SerialPort | None = None
        # A sensor named on the command line must be there; one found automatically, once it has been seen.
        # Missing after that, it's "lost" (unplugged, a USB reset): the obstacle stop can't see what it saw.
        self.expected = bool(device) and device != "auto"
        self.mm: int | None = None             # the latest distance; None = nothing in range
        self.at = 0.0                          # when it came
        self.frames = 0
        self._buf = b""
        self._last_error: str | None = None
        self._told: set[str] = set()           # ports already reported as not the sensor
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=self.WHAT.replace(" ", "-"))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.port is not None:
            self.port.close()
        if self.path is not None:
            self.claimed.discard(self.path)

    def _candidates(self) -> list[str]:
        return [c for c in self.picker.candidates() if c == self.path or c not in self.claimed]

    async def _identify(self, path: str) -> SerialPort | None:
        """The port open, if the sensor's frames come from it; else None, and the port is left alone a while."""
        port = SerialPort(path, self._on_data, lambda exc: None, baud=self.BAUD)
        self._buf = b""
        try:
            port.open()
        except OSError as e:
            if str(e) != self._last_error:               # once, not every retry
                log.warning("cannot open %s %s: %s", self.WHAT, path, e)
            self._last_error = str(e)
            return None
        start = self.frames
        deadline = time.monotonic() + IDENTIFY_S
        while time.monotonic() < deadline and port.is_open and self.frames - start < IDENTIFY_FRAMES:
            await asyncio.sleep(0.05)
        if self.frames - start < IDENTIFY_FRAMES or not port.is_open:
            port.close()
            self.mm = None
            self.picker.reject(path)
            if path not in self._told:
                log.warning("%s doesn't send %s frames; leaving it alone", path, self.WHAT)
                self._told.add(path)
            return None
        self._told.discard(path)
        port._on_lost = self._on_lost
        if self.path is None:
            # Found automatically: from now on this one is that port, so after a replug it comes back here
            # rather than as another sensor.
            self.path = path
            self.claimed.add(path)
            self.picker.device = path
        log.info("%s at %s", self.WHAT, path)
        return port

    async def _run(self) -> None:
        while True:
            if self.port is None or not self.port.is_open:
                for path in self._candidates():
                    port = await self._identify(path)
                    if port is not None:
                        self.port, self.expected = port, True
                        break
            await asyncio.sleep(RETRY_S)

    def _on_data(self, data: bytes) -> None:
        values, self._buf = parse_frames(self._buf + data)
        if values:
            self.mm = distance_mm(values[-1])
            self.at = time.monotonic()
            self.frames += len(values)

    def _on_lost(self, exc: Exception) -> None:
        log.warning("%s gone: %s", self.WHAT, exc)
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

    def readings(self) -> list[dict]:
        """For the obstacle stop: this sensor's snapshot, with which way it faces; [] while it isn't there."""
        s = self.snapshot()
        return [] if s is None else [{**s, "angle": self.angle}]


def parse_devices(spec: str) -> list[tuple[str, float]] | None:
    """RANGE_DEVICE / --range-device: "auto" (None: every plain CP2102 that sends the frames), or ports with
    which way each faces: "PORT", "PORT@-30,PORT@0,PORT@30" (degrees, clockwise from straight ahead)."""
    spec = (spec or "").strip()
    if spec.lower() in ("", "auto"):
        return None
    out = []
    for part in spec.split(","):
        path, _, angle = part.strip().partition("@")
        if not path:
            raise ValueError(f"an empty port in {spec!r}")
        a = float(angle) if angle.strip() else 0.0
        if not math.isfinite(a) or abs(a) > 180:
            raise ValueError(f"angle {angle!r} not between -180 and 180")
        out.append((path, a))
    return out


class RangeSensors:
    """All the distance sensors: the ones named, or (auto) every one that turns up, plugged in at any time."""

    def __init__(self, spec: str = "auto"):
        self.claimed: set[str] = set()
        named = parse_devices(spec)
        self.auto = named is None
        self.sensors = [] if named is None else [RangeSensor(p, a, self.claimed) for p, a in named]
        self._spare: RangeSensor | None = None     # auto: looking for the next one
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        for s in self.sensors:
            s.start()
        if self.auto:
            self._task = asyncio.create_task(self._watch(), name="range-sensors")

    async def _watch(self) -> None:
        while True:
            if self._spare is None:
                self._spare = RangeSensor("auto", 0.0, self.claimed)
                self._spare.start()
            elif self._spare.present:
                self.sensors.append(self._spare)
                log.info("distance sensors: %d", len(self.sensors))
                self._spare = None
                continue
            await asyncio.sleep(0.5)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for s in self.sensors + ([self._spare] if self._spare else []):
            await s.stop()

    @property
    def lost(self) -> bool:
        return any(s.lost for s in self.sensors)

    def readings(self) -> list[dict]:
        return [r for s in self.sensors for r in s.readings()]

    def snapshot(self) -> dict | None:
        """The nearest fresh reading ("m"; None = nothing in range), "fresh" only if every sensor is, and each
        sensor's own reading. None without any."""
        rs = self.readings()
        if not rs:
            return None
        near = [r["m"] for r in rs if r["fresh"] and r["m"] is not None]
        return {"m": min(near) if near else None, "fresh": all(r["fresh"] for r in rs), "sensors": rs}
