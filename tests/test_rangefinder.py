"""The ultrasonic distance sensor: its frames, and the reader that follows it being plugged in and out."""

import asyncio
import os

from beamctl import rangefinder
from beamctl.rangefinder import RangeSensor, distance_mm, parse_frames


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


def test_closer_than_it_measures_is_touching_not_clear():
    assert distance_mm(1341) == 1341 and distance_mm(30) == 30
    assert distance_mm(29) == 0 and distance_mm(1) == 0          # inside the blind zone: blocked
    assert distance_mm(0) is None                                # the sensor's "no echo"
    assert distance_mm(4501) is None and distance_mm(0xFFFF) is None


async def test_reading_a_sensor(monkeypatch):
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    master, slave = os.openpty()
    sensor = RangeSensor(os.ttyname(slave))
    assert sensor.snapshot() is None                       # not found yet
    sensor.start()
    try:
        for _ in range(40):
            os.write(master, frame(900))                   # it talks all the time: that's how it's recognised
            await asyncio.sleep(0.05)
            if sensor.snapshot() is not None:
                break
        os.write(master, frame(1341))
        await asyncio.sleep(0.1)
        assert sensor.snapshot() == {"m": 1.341, "fresh": True}
        os.write(master, frame(0))                         # the sensor's "no echo"
        await asyncio.sleep(0.1)
        assert sensor.snapshot() == {"m": None, "fresh": True}
        os.write(master, frame(12))                        # something right against it
        await asyncio.sleep(0.1)
        assert sensor.snapshot() == {"m": 0.0, "fresh": True}
        monkeypatch.setattr(rangefinder, "STALE_S", 0.0)  # it stops talking
        assert sensor.snapshot() == {"m": None, "fresh": False}
    finally:
        await sensor.stop()
        os.close(master)
        os.close(slave)


async def test_unplugged_after_it_was_seen_is_lost(monkeypatch):
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    master, slave = os.openpty()
    path = os.ttyname(slave)
    sensor = RangeSensor()
    monkeypatch.setattr(sensor, "_candidates", lambda: [path])  # found "automatically"
    assert not sensor.lost                                     # not seen yet: nothing to miss
    sensor.start()
    try:
        for _ in range(40):
            os.write(master, frame(900))
            await asyncio.sleep(0.05)
            if sensor.present:
                break
        assert sensor.present and not sensor.lost
        monkeypatch.setattr(sensor, "_candidates", lambda: [])  # gone from /dev
        os.close(slave)
        os.close(master)                                       # unplugged: its port reports an error
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not sensor.present:
                break
        assert sensor.snapshot() is None and sensor.lost
    finally:
        await sensor.stop()


async def test_no_sensor_no_reading(monkeypatch):
    monkeypatch.setattr(rangefinder, "AUTO", "/nonexistent/serial-*")
    sensor = RangeSensor()
    sensor.start()
    await asyncio.sleep(0.05)
    assert sensor.snapshot() is None
    assert not sensor.lost
    await sensor.stop()


async def test_a_port_without_its_frames_is_left_alone(monkeypatch):
    """A robot arm on the same chip (talking, but not in distance frames) isn't taken for the sensor."""
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    monkeypatch.setattr(rangefinder, "IDENTIFY_S", 0.3)
    arm_m, arm_s = os.openpty()
    rng_m, rng_s = os.openpty()
    arm, real = os.ttyname(arm_s), os.ttyname(rng_s)
    sensor = RangeSensor()
    monkeypatch.setattr(sensor, "_candidates",
                        lambda: [p for p in (arm, real) if p not in sensor.picker.rejected])
    sensor.start()
    try:
        for _ in range(60):
            os.write(arm_m, b'{"T":1051,"x":235.1,"y":0,"z":234}\n')
            os.write(rng_m, frame(1500))
            await asyncio.sleep(0.05)
            if sensor.present:
                break
        assert sensor.present and sensor.port.path == real and arm in sensor.picker.rejected
        assert sensor.snapshot()["m"] == 1.5
    finally:
        await sensor.stop()
        for fd in (arm_m, arm_s, rng_m, rng_s):
            os.close(fd)


def test_a_turned_down_port_is_tried_again_later(monkeypatch, tmp_path):
    """In case it was the sensor still starting up; and at once if it was unplugged and plugged back."""
    from beamctl import serialport
    a = tmp_path / "usb-Silicon_Labs_CP2102_USB_to_UART_a"
    a.touch()
    picker = serialport.PortPicker("auto", str(tmp_path / "usb-Silicon_Labs_CP2102_USB_to_UART*"))
    assert picker.candidates() == [str(a)]
    picker.reject(str(a))
    assert picker.candidates() == []
    picker.rejected[str(a)] -= picker.REJECT_S
    assert picker.candidates() == [str(a)]
    picker.reject(str(a))
    a.unlink()                                                 # unplugged: forgotten
    assert picker.candidates() == [] and not picker.rejected
    a.touch()                                                  # plugged back: tried at once
    assert picker.candidates() == [str(a)]


async def feed_until(masters, cond, frames, tries=80):
    for _ in range(tries):
        for fd, mm in zip(masters, frames):
            os.write(fd, mm if isinstance(mm, bytes) else frame(mm))
        await asyncio.sleep(0.05)
        if cond():
            return True
    return cond()


async def test_several_sensors_found_automatically_each_on_its_own_port(monkeypatch, tmp_path):
    """Corner sensors on more adapters of the same kind: each port read by one sensor only, the arm left alone."""
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    monkeypatch.setattr(rangefinder, "IDENTIFY_S", 0.3)
    ptys = [os.openpty() for _ in range(3)]
    for (m, s), name in zip(ptys, "abc"):
        (tmp_path / f"usb-Silicon_Labs_CP2102_USB_to_UART_{name}").symlink_to(os.ttyname(s))
    monkeypatch.setattr(rangefinder, "AUTO", str(tmp_path / "usb-Silicon_Labs_CP2102_USB_to_UART*"))
    group = rangefinder.RangeSensors("auto")
    group.start()
    try:
        masters = [m for m, _ in ptys]
        arm = b'{"T":1051,"x":235.1}\n'
        assert await feed_until(masters, lambda: len(group.sensors) == 2, [800, arm, 450])
        await feed_until(masters, lambda: False, [800, arm, 450], tries=10)
        assert len(group.sensors) == 2                               # not the arm, and no port twice
        assert sorted(s.path[-1] for s in group.sensors) == ["a", "c"]
        snap = group.snapshot()
        assert snap["m"] == 0.45 and snap["fresh"] and len(snap["sensors"]) == 2
    finally:
        await group.stop()
        for m, s in ptys:
            os.close(m)
            os.close(s)


async def test_named_sensors_with_the_way_they_face(monkeypatch):
    monkeypatch.setattr(rangefinder, "RETRY_S", 0.05)
    (ma, sa), (mb, sb) = os.openpty(), os.openpty()
    group = rangefinder.RangeSensors(f"{os.ttyname(sa)}@-30,{os.ttyname(sb)}@30")
    assert group.lost                                              # named: expected from the start
    group.start()
    try:
        assert await feed_until([ma, mb], lambda: len(group.readings()) == 2, [1200, 600])
        assert sorted((r["angle"], r["m"]) for r in group.readings()) == [(-30.0, 1.2), (30.0, 0.6)]
        assert not group.lost
    finally:
        await group.stop()
        for fd in (ma, sa, mb, sb):
            os.close(fd)


def test_the_sensor_list():
    assert rangefinder.parse_devices("auto") is None and rangefinder.parse_devices("") is None
    assert rangefinder.parse_devices("/dev/a@-30, /dev/b ,/dev/c@30") == [("/dev/a", -30.0), ("/dev/b", 0.0),
                                                                          ("/dev/c", 30.0)]
    for bad in ("/dev/a@left", "/dev/a@400", "/dev/a,,/dev/b", "@30"):
        try:
            rangefinder.parse_devices(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
