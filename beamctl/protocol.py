"""Beam+ (tx011) / BeamPro (st001) motor board wire protocol.

See docs/motor-protocol.md for where every constant comes from.
All fields are little-endian; "fixed" values are int32 = value * 65536.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

CMD_MAGIC = 0x5555AAAA          # bytes AA AA 55 55
CMD_SIZE = 32

STATUS_MAGIC = 0xCCCC3333
CONTROLLER_STATUS_MAGIC = 0xCCCC4444
DOCK_ID_MAGIC = 0xCCCC5555
REPLY_SIZES = {STATUS_MAGIC: 128, CONTROLLER_STATUS_MAGIC: 128, DOCK_ID_MAGIC: 64}

# Command codes
CMD_VELOCITY = 0x00
CMD_GYRO_MODE = 0x0D
CMD_POWER = 0x0F
CMD_GET_STATUS = 0x10
CMD_GET_DOCK_ID = 0x14
CMD_CHARGING_COLOR = 0x19

# CMD_POWER "b" argument
POWER_CYCLE = 0
POWER_OFF_UNTIL_CHARGE = 1
POWER_OFF = 2

DRIVING_MODE_DEFAULT = 1
FIXED = 65536.0

# Status flag bits
FLAG_MOTORS_ENABLED = 1 << 0
FLAG_BATTERY_ENABLED = 1 << 1
FLAG_CHARGING = 1 << 22
FLAG_CHARGER_DETECTED = 1 << 23

# Scale factors taken from Communicator::readStatus
CURRENT_SCALE = 0.0007947286
ENCODER_LINEAR_SCALE = 0.0040578907
ENCODER_ANGULAR_SCALE = 0.019415744
ENCODER_ABORT_LIMIT = 0x7F000000


def crc32_stm32(buf: bytes) -> int:
    """STM32 hardware CRC: poly 0x04C11DB7, init 0xFFFFFFFF, MSB-first, fed 32-bit LE words."""
    if len(buf) % 4:
        raise ValueError("CRC input must be a multiple of 4 bytes")
    c = 0xFFFFFFFF
    for (word,) in struct.iter_unpack("<I", buf):
        c ^= word
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
    return c


def to_fixed(value: float) -> int:
    return int(value * FIXED)


def from_fixed(raw: int) -> float:
    return raw / FIXED


# --- commands (host -> board) ------------------------------------------------

_CMD_BODY = struct.Struct("<IIiiIQ")   # magic, arg0, a, b, code, tag  (28 bytes)


def encode_command(code: int, arg0: int = 0, a: int = 0, b: int = 0, tag: int = 0) -> bytes:
    body = _CMD_BODY.pack(CMD_MAGIC, arg0 & 0xFFFFFFFF, a, b, code, tag & 0xFFFFFFFFFFFFFFFF)
    return body + struct.pack("<I", crc32_stm32(body))


@dataclass(frozen=True)
class Command:
    code: int
    arg0: int
    a: int
    b: int
    tag: int


def decode_command(pkt: bytes) -> Command:
    if len(pkt) != CMD_SIZE:
        raise ValueError(f"command must be {CMD_SIZE} bytes, got {len(pkt)}")
    if crc32_stm32(pkt) != 0:
        raise ValueError("bad command CRC")
    magic, arg0, a, b, code, tag = _CMD_BODY.unpack_from(pkt)
    if magic != CMD_MAGIC:
        raise ValueError(f"bad command magic {magic:#x}")
    return Command(code, arg0, a, b, tag)


def velocity(linear: float, angular: float, tag: int = 0, mode: int = DRIVING_MODE_DEFAULT) -> bytes:
    return encode_command(CMD_VELOCITY, mode, to_fixed(linear), to_fixed(angular), tag)


def power(delay_s: int, kind: int = POWER_CYCLE) -> bytes:
    return encode_command(CMD_POWER, 0, delay_s, kind)


def get_status() -> bytes:
    return encode_command(CMD_GET_STATUS)


def gyro_mode(on: bool) -> bytes:
    return encode_command(CMD_GYRO_MODE, 0, int(on))


def charging_color(color: int) -> bytes:
    return encode_command(CMD_CHARGING_COLOR, 0, color & 0xFFFF)


# --- status (board -> host) --------------------------------------------------

# Offsets 0..127 of the 0xCCCC3333 packet.
_STATUS = struct.Struct(
    "<I I ii ii i iiii iii ii i I ii i Q iiiiii HH I I"
)
assert _STATUS.size == 128


@dataclass
class Status:
    flags: int = FLAG_MOTORS_ENABLED | FLAG_BATTERY_ENABLED
    speed_l: int = 0
    speed_r: int = 0
    current_l: int = 0                 # raw; amps = raw * CURRENT_SCALE
    current_r: int = 0
    temperature: int = 25
    battery_voltage: float = 0.0       # volts
    battery_current: float = 0.0       # amps
    charge_voltage: float = 0.0
    charge_current: float = 0.0
    accel: tuple[int, int, int] = (0, 0, 0)
    rotation: tuple[int, int] = (0, 0)
    capacity: float = 0.0
    timestamp_us: int = 0
    encoder_l: int = 0
    encoder_r: int = 0
    capacity_max: float = 0.0
    limiter_tag: int = 0
    actual_linear: float = 0.0         # m/s
    actual_angular: float = 0.0        # rad/s
    limited_linear: float = 0.0
    limited_angular: float = 0.0
    linear_limit: float = 0.0
    angular_limit: float = 0.0
    command_latency_ms: int = 0
    limiter_latency_ms: int = 0
    integrated_yaw: int = 0            # low 24 bits = full turn

    # Derived values, matching what Beam's readStatus() computes.
    @property
    def motors_enabled(self) -> bool:
        return bool(self.flags & FLAG_MOTORS_ENABLED)

    @property
    def charging(self) -> bool:
        return bool(self.flags & FLAG_CHARGING)

    @property
    def charger_detected(self) -> bool:
        return bool(self.flags & FLAG_CHARGER_DETECTED)

    @property
    def fault(self) -> int:
        return (self.flags >> 24) & 0xFF

    @property
    def yaw_rad(self) -> float:
        return (self.integrated_yaw & 0xFFFFFF) * 2 * math.pi / (1 << 24)

    @property
    def battery_percent(self) -> float | None:
        if self.capacity_max <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * self.capacity / self.capacity_max))

    def to_dict(self) -> dict:
        return {
            "motors_enabled": self.motors_enabled,
            "charging": self.charging,
            "charger_detected": self.charger_detected,
            "fault": self.fault,
            "battery_voltage": round(self.battery_voltage, 2),
            "battery_current": round(self.battery_current, 2),
            "battery_percent": None if self.battery_percent is None else round(self.battery_percent, 1),
            "actual_linear": round(self.actual_linear, 3),
            "actual_angular": round(self.actual_angular, 3),
            "limited_linear": round(self.limited_linear, 3),
            "limited_angular": round(self.limited_angular, 3),
            "command_latency_ms": self.command_latency_ms,
            "limiter_latency_ms": self.limiter_latency_ms,
            "yaw_deg": round(math.degrees(self.yaw_rad), 1),
            # Distance driven since the board started, from the wheel encoders (Beam's own formula).
            "odometry_m": round((self.encoder_l - self.encoder_r) * ENCODER_LINEAR_SCALE, 3),
            "temperature": self.temperature,
            "timestamp_us": self.timestamp_us,
        }


def encode_status(s: Status) -> bytes:
    body = _STATUS.pack(
        STATUS_MAGIC, s.flags & 0xFFFFFFFF, s.speed_l, s.speed_r, s.current_l, s.current_r, s.temperature,
        to_fixed(s.battery_voltage), to_fixed(s.battery_current),
        to_fixed(s.charge_voltage), to_fixed(s.charge_current),
        *s.accel, *s.rotation, to_fixed(s.capacity), s.timestamp_us & 0xFFFFFFFF,
        s.encoder_l, s.encoder_r, to_fixed(s.capacity_max), s.limiter_tag & 0xFFFFFFFFFFFFFFFF,
        to_fixed(s.actual_linear), to_fixed(s.actual_angular),
        to_fixed(s.limited_linear), to_fixed(s.limited_angular),
        to_fixed(s.linear_limit), to_fixed(s.angular_limit),
        s.command_latency_ms & 0xFFFF, s.limiter_latency_ms & 0xFFFF,
        s.integrated_yaw & 0xFFFFFFFF, 0,
    )
    return body[:124] + struct.pack("<I", crc32_stm32(body[:124]))


def decode_status(pkt: bytes) -> Status:
    if len(pkt) != 128:
        raise ValueError(f"status must be 128 bytes, got {len(pkt)}")
    if crc32_stm32(pkt) != 0:
        raise ValueError("bad status CRC")
    f = _STATUS.unpack(pkt)
    if f[0] != STATUS_MAGIC:
        raise ValueError(f"bad status magic {f[0]:#x}")
    # Beam's driver abort()s here; we raise so the server can stop and reset instead.
    if max(abs(f[18]), abs(f[19])) > ENCODER_ABORT_LIMIT:
        raise ValueError("encoder counts too close to overflow")
    return Status(
        flags=f[1], speed_l=f[2], speed_r=f[3], current_l=f[4], current_r=f[5], temperature=f[6],
        battery_voltage=from_fixed(f[7]), battery_current=from_fixed(f[8]),
        charge_voltage=from_fixed(f[9]), charge_current=from_fixed(f[10]),
        accel=(f[11], f[12], f[13]), rotation=(f[14], f[15]),
        capacity=from_fixed(f[16]), timestamp_us=f[17],
        encoder_l=f[18], encoder_r=f[19], capacity_max=from_fixed(f[20]), limiter_tag=f[21],
        actual_linear=from_fixed(f[22]), actual_angular=from_fixed(f[23]),
        limited_linear=from_fixed(f[24]), limited_angular=from_fixed(f[25]),
        linear_limit=from_fixed(f[26]), angular_limit=from_fixed(f[27]),
        command_latency_ms=f[28], limiter_latency_ms=f[29], integrated_yaw=f[30],
    )


class PacketReader:
    """Reassembles board replies from a byte stream: scan for a known magic, then a fixed-size packet."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out: list[tuple[int, bytes]] = []
        while len(self._buf) >= 4:
            magic = int.from_bytes(self._buf[:4], "little")
            size = REPLY_SIZES.get(magic)
            if size is None:
                del self._buf[0]
                continue
            if len(self._buf) < size:
                break
            pkt = bytes(self._buf[:size])
            if crc32_stm32(pkt) != 0:
                self.crc_errors += 1
                del self._buf[0]          # resync on the next magic
                continue
            del self._buf[:size]
            out.append((magic, pkt))
        return out
