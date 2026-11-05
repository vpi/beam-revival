"""The 2D lidar (Slamtec RPLIDAR C1) on top of the base; see docs/sensors.md.

It talks at 460800 baud through its own CP2102N adapter. "A5 20" starts a scan, "A5 25" stops it. A scan
answers with a 7-byte descriptor (A5 5A 05 00 00 40 81), then 5 bytes per measurement:
  byte 0: bit 0 = first point of a new turn, bit 1 = its inverse, bits 2-7 = signal quality
  byte 1: bit 0 = always 1, bits 1-7 = angle (1/64 degree, low 7 bits); byte 2 = the angle's next 8 bits
  bytes 3-4: distance in 1/4 mm (0 = no echo)
Before a port is used, "A5 50" (device info) must get the lidar's answer: other things, like a robot arm, use
the same USB serial chip.
Angles go clockwise seen from above, 0 = the arrow on the lidar's top. It's mounted with that arrow to the
robot's front, so 0 is straight ahead; the mast right behind it hides a slice, which is left out.

The lidar spins only while the robot is awake, and nothing is ever stored: the latest turn is all there is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .serialport import PortPicker, SerialPort

log = logging.getLogger("beam.lidar")

AUTO = "/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART*"
BAUD = 460800
START, STOP = bytes([0xA5, 0x20]), bytes([0xA5, 0x25])
DESCRIPTOR = bytes([0xA5, 0x5A, 0x05, 0x00, 0x00, 0x40, 0x81])
GET_INFO = bytes([0xA5, 0x50])
INFO = bytes([0xA5, 0x5A, 0x14, 0x00, 0x00, 0x00, 0x04])   # then model, firmware minor, major, hardware, serial (16)
IDENTIFY_S = 1.0               # how long a port has to answer like a lidar
MIN_M, MAX_M = 0.05, 12.0      # what the C1 measures
BIN_DEG = 2                    # the driving page gets the nearest hit per 2 degrees
STALE_S = 1.0                  # no complete turn for this long: restart the scan
RETRY_S = 2.0


def parse_mask(text: str | None) -> tuple[float, float] | None:
    """"135-210" -> (135, 210): angles (degrees) to leave out, e.g. what the mast hides. None/"" = none."""
    if not text:
        return None
    lo, hi = (float(v) for v in text.split("-"))
    return lo % 360, hi % 360


def masked(angle: float, mask: tuple[float, float] | None) -> bool:
    if mask is None:
        return False
    lo, hi = mask
    return lo <= angle < hi if lo <= hi else angle >= lo or angle < hi      # a slice across 0 too


class ScanParser:
    """Bytes from the lidar in, complete turns out: lists of (angle in degrees, distance in metres)."""

    def __init__(self) -> None:
        self.buf = b""
        self.synced = False          # past the scan descriptor
        self.turn: list[tuple[float, float]] = []
        self.started = False         # seen the first point of a turn (the partial one before it is dropped)

    def reset(self) -> None:
        self.__init__()

    def feed(self, data: bytes) -> list[list[tuple[float, float]]]:
        self.buf += data
        if not self.synced:
            i = self.buf.find(DESCRIPTOR)
            if i < 0:
                self.buf = self.buf[-(len(DESCRIPTOR) - 1):]
                return []
            self.buf, self.synced = self.buf[i + len(DESCRIPTOR):], True
        turns, buf, i = [], self.buf, 0
        while len(buf) - i >= 5:
            b0, b1, b2, b3, b4 = buf[i:i + 5]
            if (b0 & 1) == (b0 >> 1) & 1 or not b1 & 1:
                i += 1                                          # out of step: find the next good point
                continue
            if b0 & 1:                                          # a new turn starts here
                if self.started and self.turn:
                    turns.append(self.turn)
                self.turn, self.started = [], True
            d = (b3 | b4 << 8) / 4000
            if self.started and MIN_M <= d <= MAX_M:
                self.turn.append(((b1 >> 1 | b2 << 7) / 64 % 360, d))
            i += 5
        self.buf = buf[i:]
        return turns


class Lidar:
    def __init__(self, device: str = "auto", mask: str | None = None, rotation: float = 0.0,
                 should_spin: Callable[[], bool] = lambda: True):
        self.device = device                   # a serial port path, or "auto" (a CP2102N that answers as a lidar)
        self.picker = PortPicker(device or "auto", AUTO)
        try:
            self.mask = parse_mask(mask)
        except ValueError:
            # Never let a typo stop the motor server: without it the board powers the robot off.
            log.error("lidar mask %r not understood (e.g. 135-210); running without one", mask)
            self.mask = None
        self.rotation = rotation               # degrees to add if the arrow isn't to the front
        self.should_spin = should_spin         # False while the robot sleeps
        self.expected = bool(device) and device != "auto"   # as RangeSensor.expected: see `lost`
        self.port: SerialPort | None = None
        self.parser = ScanParser()
        self.spinning = False
        self.points: list[tuple[float, float]] = []   # the latest complete turn, robot angles
        self.at = 0.0
        self.turns = 0
        self._turn_times: list[float] = []
        self._started_at = 0.0
        self._last_error: str | None = None
        self._probing: bytearray | None = None  # what came back while checking a port is a lidar
        self._told: set[str] = set()           # ports already reported as not a lidar
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="lidar")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.port is not None and self.port.is_open:
            self._send(STOP)
            self.port.close()

    def _candidates(self) -> list[str]:
        return self.picker.candidates()

    async def _identify(self, path: str) -> SerialPort | None:
        """The port open, if a lidar answers on it; else None, and the port is left alone for a while."""
        port = SerialPort(path, self._on_data, self._on_lost, baud=BAUD)
        try:
            port.open()
        except OSError as e:
            if str(e) != self._last_error:              # once, not every retry
                log.warning("cannot open lidar %s: %s", path, e)
            self._last_error = str(e)
            return None
        self._probing = bytearray()
        answer = None
        try:
            port.write(STOP)                            # in case it's still scanning
            await asyncio.sleep(0.1)
            port.flush_input()
            self._probing.clear()
            port.write(GET_INFO)
            deadline = time.monotonic() + IDENTIFY_S
            while time.monotonic() < deadline and port.is_open:
                i = self._probing.find(INFO)
                if i >= 0 and len(self._probing) >= i + len(INFO) + 4:
                    answer = bytes(self._probing[i + len(INFO):i + len(INFO) + 4])
                    break
                await asyncio.sleep(0.05)
        except OSError:
            pass
        finally:
            self._probing = None
        if answer is None:
            port.close()
            self.picker.reject(path)
            if path not in self._told:
                log.warning("%s doesn't answer like a lidar; leaving it alone", path)
                self._told.add(path)
            return None
        self._told.discard(path)
        model, minor, major, hw = answer
        log.info("lidar at %s (model %02X, firmware %d.%02d, hardware %d)", path, model, major, minor, hw)
        return port

    def _send(self, data: bytes) -> None:
        try:
            self.port.write(data)
        except OSError as e:
            log.warning("lidar write failed: %s", e)

    async def _start_scan(self) -> None:
        self._send(STOP)
        await asyncio.sleep(0.05)              # it ignores a start that follows a stop too closely
        if self.port is None or not self.port.is_open:
            return
        self.port.flush_input()
        self.parser.reset()
        self._send(START)
        self.spinning, self._started_at = True, time.monotonic()

    def _stop_scan(self) -> None:
        self._send(STOP)
        self.spinning, self.points = False, []

    async def _run(self) -> None:
        while True:
            if self.port is None or not self.port.is_open:
                self.spinning = False
                for path in self._candidates():
                    port = await self._identify(path)
                    if port is not None:
                        self.port, self.expected = port, True
                        break
            if self.port is not None and self.port.is_open:
                now = time.monotonic()
                if self.should_spin() and not self.spinning:
                    await self._start_scan()
                    log.info("lidar spinning")
                elif not self.should_spin() and self.spinning:
                    self._stop_scan()
                    log.info("lidar stopped (robot asleep)")
                elif self.spinning and now - max(self.at, self._started_at) > STALE_S + 2:
                    log.warning("no scan from the lidar; restarting it")   # 2 s more for spinning up
                    await self._start_scan()
            await asyncio.sleep(0.5 if self.port is not None else RETRY_S)

    def _on_data(self, data: bytes) -> None:
        if self._probing is not None:
            self._probing += data
            return
        if not self.spinning:
            return
        for turn in self.parser.feed(data):
            pts = []
            for a, d in turn:
                a = (a + self.rotation) % 360
                if not masked(a, self.mask):
                    pts.append((a, d))
            now = time.monotonic()
            self.points, self.at = pts, now
            self.turns += 1
            self._turn_times = [t for t in self._turn_times if now - t < 2.0] + [now]

    def _on_lost(self, exc: Exception) -> None:
        if self._probing is not None:
            return                                      # it was only being checked
        log.warning("lidar gone: %s", exc)
        self.spinning, self.points = False, []

    @property
    def present(self) -> bool:
        return self.port is not None and self.port.is_open

    @property
    def lost(self) -> bool:
        """It was there (or was named) and isn't now. Stays so until it's back."""
        return self.expected and not self.present

    @property
    def fresh(self) -> bool:
        """A complete turn came lately (cheap: the obstacle stop asks 100 times a second)."""
        return self.spinning and time.monotonic() - self.at < STALE_S

    def bins(self) -> list[int]:
        """The nearest hit per BIN_DEG degrees, in cm, from straight ahead clockwise; 0 = nothing (or hidden)."""
        out = [0] * (360 // BIN_DEG)
        for a, d in self.points:
            i = int(a // BIN_DEG) % len(out)
            cm = max(1, round(d * 100))
            if out[i] == 0 or cm < out[i]:
                out[i] = cm
        return out

    def scan(self) -> dict | None:
        """The latest turn in finer steps, for the brain's map: the mean distance per degree in cm, from straight
        ahead clockwise (0 = nothing, or hidden), and which turn it is. None while it isn't fresh."""
        if not self.fresh:
            return None
        total, count = [0.0] * 360, [0] * 360
        for a, d in self.points:
            i = int(a) % 360
            total[i] += d
            count[i] += 1
        return {"turn": self.turns, "cm": [round(t * 100 / c) if c else 0 for t, c in zip(total, count)]}

    def snapshot(self) -> dict | None:
        """None without a lidar. Else whether it spins, turns per second, the hidden slice and the latest turn."""
        if not self.present:
            return None
        fresh = self.fresh
        return {"spinning": self.spinning, "fresh": fresh,
                "hz": round(len(self._turn_times) / 2.0, 1) if fresh else 0.0,      # turns in the last 2 s
                "mask": list(self.mask) if self.mask else None, "bin_deg": BIN_DEG,
                "cm": self.bins() if fresh else None}
