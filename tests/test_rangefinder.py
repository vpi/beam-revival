"""The ultrasonic distance sensor: its frames, and the reader that follows it being plugged in and out."""

import asyncio
import os

from beamctl import rangefinder
from beamctl.rangefinder import RangeSensor, parse_frames


def frame(mm: int) -> bytes:
    hi, lo = mm >> 8, mm & 0xFF
    return bytes([0xFF, hi, lo, (0xFF + hi + lo) & 0xFF])


def test_frames():
    assert parse_frames(frame(1341) + frame(492)) == ([1341, 492], b"")


def test_noise_and_bad_checksums_are_skipped():
    bad = bytes([0xFF, 0x05, 0x3D, 0x00])
    assert parse_frames(b"\x12\xff" + bad + frame(500) + b"\x34") == ([500], b"\x34")


def test_a_frame_split_across_reads():
    values, rest = parse_frames(frame(1000)[:3])
    assert values == [] and rest == frame(1000)[:3]
    assert parse_frames(rest + frame(1000)[3:]) == ([1000], b"")


async def test_reading_a_sensor(monkeypatch):
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    master, slave = os.openpty()
    sensor = RangeSensor(os.ttyname(slave))
    assert sensor.snapshot() is None                       # not found yet
    sensor.start()
    try:
        for _ in range(40):
            await asyncio.sleep(0.05)
            if sensor.snapshot() is not None:
                break
        os.write(master, frame(1341))
        await asyncio.sleep(0.1)
        assert sensor.snapshot() == {"m": 1.341, "fresh": True}
        os.write(master, frame(0))                         # the sensor's "no echo"
        await asyncio.sleep(0.1)
        assert sensor.snapshot() == {"m": None, "fresh": True}
        monkeypatch.setattr(rangefinder, "STALE_S", 0.0)  # it stops talking
        assert sensor.snapshot() == {"m": None, "fresh": False}
    finally:
        await sensor.stop()
        os.close(master)
        os.close(slave)


async def test_no_sensor_no_reading(monkeypatch):
    monkeypatch.setattr(rangefinder, "AUTO", "/nonexistent/serial-*")
    sensor = RangeSensor()
    sensor.start()
    await asyncio.sleep(0.05)
    assert sensor.snapshot() is None
    await sensor.stop()
