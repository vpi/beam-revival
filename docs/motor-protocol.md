# Beam+ motor board protocol (tx011 / "st001" driver)

Reverse-engineered from Beam software 2.22.14 (live-disk build, Sep 2021):

- `libst001_lib.so`: the motor board driver (`bacon::st001::Communicator`)
- `librpd_shared.so`: `DrivingListener::st001Thread()` (the drive loop) and its settings defaults
- `st001_power_control`: the power/watchdog helper

Decompiled with Ghidra 12.1.4 through REA on the analysis VM (`~/beam/re/`).
Each item is marked **[confirmed]** (read straight from the code) or **[hypothesis]** (to verify on the robot).

## 1. Device

| | |
|---|---|
| Beam+ board | `tx011`, USB `25a1:000b`, USB CDC-ACM serial → `/dev/ttyACM*` **[confirmed]** |
| BeamPro board | `st001`, USB `25a1:0001` (same protocol, same driver) **[confirmed]** |
| udev symlink | Beam's rules link either board to `/dev/st001` (`75-st.rules`) **[confirmed]** |
| Firmware update mode | DFU, flashed with `dfu-util -d 25a1:000b -D tx011.stdfu`. **Don't use unless the board is broken.** |

How the port is opened (`openDevice`) **[confirmed]**:
- `open(dev, O_RDWR | O_NOCTTY | O_NONBLOCK)`, then an exclusive `fcntl(F_SETLK)` write lock
- raw termios: `c_cflag = CS8|CREAD|CLOCAL` (no baud rate, since it's USB CDC), `c_iflag = IGNPAR`, `c_oflag = c_lflag = 0`, `VMIN = 1`, `VTIME = 0`
- `tcflush(TCIFLUSH)` after configuring; any pending input is flushed before each request

## 2. Framing

All fields are **little-endian**. "Fixed" means a signed 32-bit integer = value × 65536.

### Host → board: 32-byte command **[confirmed]**

| Off | Size | Field |
|---|---|---|
| 0 | 4 | magic bytes `AA AA 55 55` (`0x5555AAAA` as a LE uint32) |
| 4 | u32 | `arg0`: driving mode for velocity commands, sub-selector for others |
| 8 | i32 | `a` (velocity: linear, fixed m/s) |
| 12 | i32 | `b` (velocity: angular, fixed rad/s) |
| 16 | u32 | command code (see §3) |
| 20 | u64 | velocity limiter tag (velocity command only, otherwise 0) |
| 28 | u32 | CRC over bytes 0..27 |

The rosbeam notes describe a 48-byte command with limits and accelerations. That's an older firmware; this build sends **32 bytes**.

### Board → host: replies **[confirmed]**

The reader scans the byte stream for a 4-byte magic, then reads the rest of the fixed-size packet. The CRC over the **whole** packet, including its trailing CRC word, must equal 0.

| Magic (LE u32) | Size | Reply to |
|---|---|---|
| `0xCCCC3333` | 128 | velocity command (code 0) or get-status (code 0x10, arg0 = 0) |
| `0xCCCC4444` | 128 | get controller status (code 0x10, arg0 = 0x10000) |
| `0xCCCC5555` | 64 | get dock id (code 0x14) |

### CRC **[confirmed]**

This is the STM32 hardware CRC: CRC-32 polynomial `0x04C11DB7`, init `0xFFFFFFFF`, MSB-first, no reflection, no final XOR. It's fed **one 32-bit LE word at a time**:

```python
def crc(buf: bytes) -> int:          # len(buf) % 4 == 0
    c = 0xFFFFFFFF
    for i in range(0, len(buf), 4):
        c ^= int.from_bytes(buf[i:i+4], "little")
        for _ in range(32):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
            c &= 0xFFFFFFFF
    return c
```

## 3. Commands

| Code | Name | arg0 | a | b | Notes |
|---|---|---|---|---|---|
| `0x00` | velocity | driving mode (default **1**) | linear × 65536 | angular × 65536 | + limiter tag at offset 20; reply is a status packet **[confirmed]** |
| `0x0D` | set gyro mode | 0 | 1/0 | 0 | default **on** **[confirmed]** |
| `0x0F` | power cycle / off | 0 | delay (s) | type: 0 = cycle, 1 = off until charging, 2 = off | a new one **replaces** any pending one **[confirmed]** |
| `0x10` | get status | 0 → `CCCC3333`, `0x10000` → `CCCC4444` | 0 | 0 | **[confirmed]** |
| `0x14` | get dock id | 0 | 0 | 0 | 64-byte reply **[confirmed]** |
| `0x19` | charging LED colour | 0 | colour & 0xFFFF | 0 | **[confirmed]** |

The library also sends battery calibration, RAM reads and serial-number commands. We don't need them, and their codes are in the decompiled sources on the VM.

## 4. Status packet (`0xCCCC3333`, 128 bytes) **[confirmed]**

Field names come from the library's own debug printer (`operator<<(_statStruct)`). Scaling comes from `readStatus()`.

| Off | Type | Field | Scaling to SI |
|---|---|---|---|
| 0 | u32 | magic | |
| 4 | u32 | flags | bit0 `m_en` motors enabled, bit1 `b_en` battery enabled, bit22 `cing` charging, bit23 `cdet` charger detected, bits 24–31 fault code |
| 8, 12 | i32 | speed L, R | raw encoder speed |
| 16, 20 | i32 | current L, R | × 0.0007947286 → A |
| 24 | i32 | temperature | raw |
| 28 | fixed | batteryVoltage | /65536 → V |
| 32 | fixed | batteryCurrent | /65536 → A |
| 36 | fixed | chargeVoltage | /65536 → V |
| 40 | fixed | chargeCurrent | /65536 → A |
| 44, 48, 52 | i32 | accel x, y, z | raw |
| 56, 60 | i32 | rotation | raw (gyro) |
| 64 | fixed | capacity | /65536 |
| 68 | u32 | timeStamp | board clock, µs (per rosbeam) |
| 72, 76 | i32 | encoder L, R (position) | lin = (L − R) × 0.0040578907, ang = −(L + R) × 0.019415744. The driver **aborts** if \|count\| > 0x7F000000 |
| 80 | fixed | capacityMax | /65536 |
| 84 | u64 | velocityLimiterTag | echo of the last command's tag |
| 92 | fixed | actualLinearVelocity | m/s |
| 96 | fixed | actualAngularVelocity | rad/s |
| 100 | fixed | limitedLinearVelocity | m/s, what the board actually allows |
| 104 | fixed | limitedAngularVelocity | rad/s |
| 108 | fixed | linearVelocityLimit | m/s |
| 112 | fixed | angularVelocityLimit | rad/s |
| 116 | u16 | drivingCommandLatency | ms |
| 118 | u16 | limiterLatency | ms |
| 120 | u32 | integratedYaw | low 24 bits × 2π / 2²⁴ → rad |
| 124 | u32 | CRC | |

(rosbeam put the latencies at 114/116. This build reads them at 116/118.)

## 5. How Beam drives it (`DrivingListener::st001Thread`) **[confirmed]**

Factory defaults from the settings initializer:

| Setting | Default |
|---|---|
| `st001/command_interval` | **10 ms** (100 Hz drive commands) |
| `st001/status_poll_interval` | 100 ms |
| `st001/driving_mode` | **1** |
| `st001/gyro_mode` | true |
| `st001/watchdog_refresh_interval` | **5 s** |
| `st001/watchdog_delay` | **300 s** |
| `st001/no_comm_reset_timeout` | 2 s (resets the board if it stops answering) |
| `st001/power_button_delay` | 2 s |

Beam+ limits from `getLimits(config = cleveland)`: linear 1.0, angular 1.0 (the BeamPro's are 2.0), plus 2.0 / 0.3 / 1.0 values that look like acceleration and deceleration limits. Before sending, the loop also clamps `|linear| ≤ rpd_max_speed` and `|angular| ≤ 1.5 × rpd_max_speed`.

Each cycle:
1. Take the latest pilot drive request: linear, angular, 64-bit tag.
2. If that request is **older than 5 s**, send linear = angular = 0 (the tag isn't updated).
3. Clamp, then send command `0x00` with mode = 1, and read back the status packet (100 ms timeout).

## 6. The "watchdog" is a delayed power cycle, not a heartbeat **[confirmed]**

Beam keeps sending **power-cycle in 300 s** (code `0x0F`, a = 300, b = 0) every 5 s. Both the app and `st001_power_control W /dev/st001 300 5` in the startup script do this. Each command replaces the previous one, so as long as Beam's software runs, the reboot never fires. If the PC hangs, the base power-cycles it 5 minutes later.

**What this means for us:** when we boot our own Linux on the robot, the old software's last refresh is still pending, so **the base will cut power about 5 minutes later** unless our software keeps refreshing it. Our motor server must send `0x0F, a = 300, b = 0` every 5 s, starting immediately. Sending `power-off` (b = 2) with a short delay is also the clean way to shut the robot down.

## 7. Open questions (to check on the robot)

1. **Limiter tag semantics [hypothesis].** The tag comes from the remote pilot's drive request, the board echoes it in the status, and it reports `drivingCommandLatency` / `limiterLatency`. The likely design: the pilot stamps each command with the board `timeStamp` it last saw, the board measures round-trip latency from it, and it scales the allowed speed down when latency is high. Test plan: send commands with tag = 0, a constant, and the latest status `timeStamp`, then compare `limitedLinearVelocity` with the commanded value, wheels off the ground.
2. Exact meaning of the fault byte and of `driving_mode` values other than 1.
3. Whether the board needs any initialisation (gyro mode, LED colour) before it accepts motion, or whether `m_en` turns on by itself.
4. Dock-id and controller-status packet layouts (only needed for auto-docking).
