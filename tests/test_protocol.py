import math
import struct

import pytest

from beamctl import protocol as p


def test_crc_residue_is_zero_for_valid_command():
    pkt = p.velocity(0.2, 0.0)
    assert len(pkt) == p.CMD_SIZE
    assert p.crc32_stm32(pkt) == 0


def test_crc_known_value():
    # Cross-checked against the algorithm decompiled from Communicator::continueCrc.
    body = struct.pack("<IIiiIQ", 0x5555AAAA, 1, int(0.2 * 65536), 0, 0, 0)
    assert p.crc32_stm32(body) == 0x1AF13C6F


def test_command_layout_matches_send_command():
    pkt = p.encode_command(p.CMD_POWER, 0, 300, p.POWER_CYCLE)
    assert pkt[:4] == bytes([0xAA, 0xAA, 0x55, 0x55])
    magic, arg0, a, b, code, tag, crc = struct.unpack("<IIiiIQI", pkt)
    assert (arg0, a, b, code, tag) == (0, 300, 0, 0x0F, 0)


def test_velocity_roundtrip():
    cmd = p.decode_command(p.velocity(-0.5, 0.25, tag=1234))
    assert cmd.code == p.CMD_VELOCITY
    assert cmd.arg0 == p.DRIVING_MODE_DEFAULT
    assert p.from_fixed(cmd.a) == pytest.approx(-0.5)
    assert p.from_fixed(cmd.b) == pytest.approx(0.25)
    assert cmd.tag == 1234


def test_decode_command_rejects_corruption():
    pkt = bytearray(p.velocity(0.1, 0.0))
    pkt[9] ^= 0x01
    with pytest.raises(ValueError):
        p.decode_command(bytes(pkt))


def test_status_roundtrip():
    s = p.Status(
        flags=p.FLAG_MOTORS_ENABLED | p.FLAG_CHARGING | (3 << 24),
        battery_voltage=25.2, capacity=50.0, capacity_max=100.0,
        timestamp_us=123456, encoder_l=1000, encoder_r=-1000, limiter_tag=99,
        actual_linear=0.4, actual_angular=-0.1, command_latency_ms=12, limiter_latency_ms=34,
        integrated_yaw=1 << 22,
    )
    pkt = p.encode_status(s)
    assert len(pkt) == 128
    assert p.crc32_stm32(pkt) == 0
    d = p.decode_status(pkt)
    assert d.charging and d.motors_enabled and not d.charger_detected
    assert d.fault == 3
    assert d.battery_voltage == pytest.approx(25.2, abs=1e-4)
    assert d.battery_percent == pytest.approx(50.0)
    assert d.limiter_tag == 99
    assert (d.command_latency_ms, d.limiter_latency_ms) == (12, 34)
    assert d.yaw_rad == pytest.approx(math.pi / 2)


def test_status_field_offsets():
    # Offsets from docs/motor-protocol.md section 4.
    pkt = p.encode_status(p.Status(timestamp_us=0xDEADBEEF, limiter_tag=0x1122334455667788,
                                   command_latency_ms=0xABCD, integrated_yaw=0x00123456))
    assert struct.unpack_from("<I", pkt, 68)[0] == 0xDEADBEEF
    assert struct.unpack_from("<Q", pkt, 84)[0] == 0x1122334455667788
    assert struct.unpack_from("<H", pkt, 116)[0] == 0xABCD
    assert struct.unpack_from("<I", pkt, 120)[0] == 0x00123456


def test_decode_status_rejects_encoder_overflow():
    with pytest.raises(ValueError):
        p.decode_status(p.encode_status(p.Status(encoder_l=0x7F000001)))


def test_packet_reader_resyncs_over_garbage_and_bad_crc():
    good = p.encode_status(p.Status(timestamp_us=1))
    bad = bytearray(p.encode_status(p.Status(timestamp_us=2)))
    bad[50] ^= 0xFF
    stream = b"\x00\x01garbage" + bytes(bad) + good[:60]
    r = p.PacketReader()
    assert r.feed(stream) == []
    out = r.feed(good[60:])
    assert len(out) == 1
    assert p.decode_status(out[0][1]).timestamp_us == 1
    assert r.crc_errors >= 1
