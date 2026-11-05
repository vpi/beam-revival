"""Minimal asyncio serial port, configured the way Beam's Communicator::openDevice() does it."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import glob
import os
import termios
import time
from typing import Callable


def configure_raw(fd: int, baud: int | None = None) -> None:
    """Raw 8N1, no echo, no flow control. Baud rate is irrelevant for USB CDC-ACM (None: leave it)."""
    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNPAR                                  # iflag
    attrs[1] = 0                                               # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL    # cflag
    attrs[3] = 0                                               # lflag
    if baud is not None:                                       # a real UART, e.g. behind a USB serial adapter
        attrs[4] = attrs[5] = getattr(termios, f"B{baud}")
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcflush(fd, termios.TCIFLUSH)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


class SerialPort:
    """Non-blocking serial port that hands received bytes to a callback on the event loop."""

    def __init__(self, path: str, on_data: Callable[[bytes], None], on_lost: Callable[[Exception], None],
                 baud: int | None = None):
        self.path = path
        self.baud = baud
        self._on_data = on_data
        self._on_lost = on_lost
        self._fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            # Exclusive lock, like Beam's fcntl(F_SETLK): refuse to share the board with another process.
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            configure_raw(fd, self.baud)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(fd, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            self._lost(e)
            return
        if not data:                       # EOF: device unplugged / pty closed
            self._lost(OSError(errno.EIO, "device closed"))
            return
        self._on_data(data)

    def write(self, data: bytes) -> None:
        if self._fd is None:
            raise OSError(errno.EBADF, "port not open")
        view = memoryview(data)
        while view:
            try:
                n = os.write(self._fd, view)
            except BlockingIOError:
                # Board isn't draining its input. Never spin the event loop: report a failed send.
                raise OSError(errno.EAGAIN, "serial output buffer full") from None
            except OSError as e:
                self._lost(e)
                raise
            view = view[n:]

    def flush_input(self) -> None:
        if self._fd is not None:
            termios.tcflush(self._fd, termios.TCIFLUSH)

    def _lost(self, exc: Exception) -> None:
        self.close()
        self._on_lost(exc)

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        if self._loop is not None:
            self._loop.remove_reader(fd)
        os.close(fd)


class PortPicker:
    """Which serial ports a sensor should try: the one named, or every match of its adapter's name.

    A port is only used once what answers on it is the sensor (each sensor checks that itself): other gadgets,
    like a robot arm, use the same USB serial chips. One that didn't answer is left alone for REJECT_S, so it
    isn't opened every few seconds (opening a port can reset what's behind it), then tried again, in case it
    was the sensor still starting up.
    """

    REJECT_S = 60.0

    def __init__(self, device: str, pattern: str):
        self.device = device                   # a port path, or "auto"
        self.pattern = pattern
        self.rejected: dict[str, float] = {}

    def candidates(self) -> list[str]:
        now = time.monotonic()
        self.rejected = {p: t for p, t in self.rejected.items() if now - t < self.REJECT_S and os.path.exists(p)}
        found = [self.device] if self.device != "auto" else sorted(glob.glob(self.pattern))
        return [p for p in found if p not in self.rejected]

    def reject(self, path: str) -> None:
        self.rejected[path] = time.monotonic()
