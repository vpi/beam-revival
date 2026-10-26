"""Sleep mode: cameras, microphone, screen and lights off, but the robot stays reachable.

The Beam+ can't be woken remotely from a real power-off (no wake-on-LAN, and the motor board only
powers back up on its button or the charger), so "sleep" keeps the PC running and switches off
everything people notice:

- the robot page closes the cameras and microphone and goes black (it watches /api/status)
- the screen backlight goes to 0 (restored on wake)
- driving is refused until someone wakes it from the driving page

A nightly schedule (`--sleep-hours 23:00-07:00`, in the robot's local time) puts it to sleep at the
start and wakes it at the end, however it fell asleep. Waking it by hand during the night keeps it
awake until the next night; it never falls asleep while someone is driving.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("beam.sleep")


def parse_hours(text: str | None) -> tuple[int, int] | None:
    """'23:00-07:00' -> (start, end) in minutes after midnight. None or '' -> no schedule."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", text)
    if not m:
        raise ValueError(f"sleep hours must look like 23:00-07:00, not {text!r}")
    h1, m1, h2, m2 = map(int, m.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60) or (h1, m1) == (h2, m2):
        raise ValueError(f"bad sleep hours {text!r}")
    return h1 * 60 + m1, h2 * 60 + m2


def in_window(hours: tuple[int, int], now: datetime) -> bool:
    start, end = hours
    t = now.hour * 60 + now.minute
    return start <= t < end if start < end else (t >= start or t < end)      # may wrap past midnight


class Backlight:
    """The screen backlight in /sys/class/backlight. Silently does nothing where there is none."""

    def __init__(self, path: str | None = "auto"):
        self.dir: Path | None = None
        if path == "auto":
            found = sorted(Path("/sys/class/backlight").glob("*"))
            self.dir = found[0] if found else None
        elif path:
            self.dir = Path(path)
        self._saved: int | None = None
        self._warned = False

    def _write(self, value: int) -> None:
        try:
            (self.dir / "brightness").write_text(str(value))
        except OSError as e:
            if not self._warned:
                log.warning("can't set the screen backlight (%s): %s", self.dir, e)
                self._warned = True

    def off(self) -> None:
        if self.dir is None:
            return
        try:
            current = int((self.dir / "brightness").read_text())
            if current > 0:
                self._saved = current
        except (OSError, ValueError):
            pass
        self._write(0)

    def ensure_on(self) -> None:
        """Awake but dark (e.g. the server was killed while asleep): light the screen again."""
        if self.dir is None:
            return
        try:
            dark = int((self.dir / "brightness").read_text()) == 0
        except (OSError, ValueError):
            return
        if dark:
            self.on()

    def on(self) -> None:
        if self.dir is None:
            return
        value = self._saved
        if not value:
            try:
                value = int(int((self.dir / "max_brightness").read_text()) * 0.75)
            except (OSError, ValueError):
                return
        self._write(value)


class SleepManager:
    """state_file: where to remember sleep across server restarts (an update or crash at night must not
    wake the robot). on_change(asleep), if given, is called on every change."""

    def __init__(self, hours: str | None = None, backlight: Backlight | None = None,
                 state_file: str | None = None, on_change=None):
        try:
            self.hours = parse_hours(hours)
        except ValueError as e:
            # Never let a typo stop the motor server: without it the board powers the robot off.
            log.error("%s; running without a sleep schedule", e)
            self.hours, hours = None, None
        self.hours_text = hours or None
        self.backlight = backlight or Backlight(None)
        self.asleep = False
        self.reason: str | None = None          # "schedule", or who put it to sleep
        self.since: float | None = None
        self._was_in_window: bool | None = None
        self._pending = False                   # the night started while someone was driving
        self.state_file = Path(state_file) if state_file else None
        self.on_change = on_change
        self._load()
        if not self.asleep:
            self.backlight.ensure_on()

    # --- remembered state ------------------------------------------------------

    def _load(self) -> None:
        if not self.state_file:
            return
        try:
            s = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self.asleep = bool(s.get("asleep"))
        self.reason, self.since = s.get("reason"), s.get("since")
        self._was_in_window = s.get("was_in_window")
        self.backlight._saved = s.get("backlight") or None
        if self.asleep:
            log.info("still asleep after a restart (%s)", self.reason)
            self.backlight.off()

    def _save(self) -> None:
        if not self.state_file:
            return
        data = {"asleep": self.asleep, "reason": self.reason, "since": self.since,
                "was_in_window": self._was_in_window, "backlight": self.backlight._saved}
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.state_file)
        except OSError as e:
            log.warning("can't save the sleep state to %s: %s", self.state_file, e)

    # --- sleep and wake ----------------------------------------------------------

    def sleep(self, reason: str) -> None:
        if self.asleep:
            return
        self.asleep, self.reason, self.since = True, reason, time.time()
        self.backlight.off()
        log.info("going to sleep (%s)", reason)
        self._save()
        if self.on_change:
            self.on_change(True)

    def wake(self, reason: str) -> None:
        self._pending = False
        if not self.asleep:
            return
        self.asleep, self.reason, self.since = False, None, time.time()
        self.backlight.on()
        log.info("waking up (%s)", reason)
        self._save()
        if self.on_change:
            self.on_change(False)

    def tick(self, now: datetime, busy: bool) -> None:
        """Call every few seconds with the local time. busy: someone is driving right now."""
        if self.hours is None:
            return
        inside = in_window(self.hours, now)
        if inside and self._was_in_window is not True:        # the night begins (or we started at night)
            self._pending = True
        if not inside and self._was_in_window and self.asleep:
            self.wake("schedule")                             # morning: awake, however it fell asleep
        if not inside:
            self._pending = False
        if self._pending and not busy:                        # never send a driven robot to sleep
            self._pending = False
            self.sleep("schedule")
        if inside != self._was_in_window:
            self._was_in_window = inside
            self._save()

    def snapshot(self) -> dict:
        return {"asleep": self.asleep, "reason": self.reason, "since": self.since, "schedule": self.hours_text}
