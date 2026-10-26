# Beam+ motor board protocol (tx011 / "st001" driver)

Reverse-engineered from Beam software 2.22.14 (live-disk build, Sep 2021):

- `libst001_lib.so`: the motor board driver (`bacon::st001::Communicator`)
- `librpd_shared.so`: `DrivingListener::st001Thread()` (the drive loop) and its settings defaults
- `st001_power_control`: the power/watchdog helper

Decompiled with Ghidra 12.1.4 through REA on the analysis VM (`~/beam/re/`).
Each item is marked **[confirmed]** (read straight from the code), **[tested]** (seen on a real Beam+, Oct 2026)
or **[hypothesis]** (still to verify). Section 8 has what the real robot taught us.

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
| `0x00` | velocity | driving mode: **always 1** (see §8) | linear × 65536 | angular × 65536 | tag at offset 20 = the board's latest limiter clock (status offset 84), or the board ignores the speeds; reply is a status packet **[tested]** |
| `0x0D` | set gyro mode | 0 | 1/0 | 0 | default **on** **[confirmed]** |
| `0x0F` | power cycle / off | 0 | delay (s) | type: 0 = cycle, 1 = off until charging, 2 = off | a new one **replaces** any pending one **[confirmed]** |
| `0x10` | get status | 0 → `CCCC3333`, `0x10000` → `CCCC4444` | 0 | 0 | **[confirmed]** |
| `0x14` | get dock id | 0 | 0 | 0 | 64-byte reply **[confirmed]** |
| `0x19` | charging LED colour | 0 | colour & 0xFFFF | 0 | the light at the bottom of the Beam+. 0 = off, `0x0F00` = the green it shows while charging, `0xFFFF` = light blue, 1–4 = blue. **Not** RGB565 (`0x07E0` is light blue) **[tested]** |

The library also sends battery calibration, RAM reads and serial-number commands. We don't need them, and their codes are in the decompiled sources on the VM.

## 4. Status packet (`0xCCCC3333`, 128 bytes) **[confirmed]**

Field names come from the library's own debug printer (`operator<<(_statStruct)`). Scaling comes from `readStatus()`.

| Off | Type | Field | Scaling to SI |
|---|---|---|---|
| 0 | u32 | magic | |
| 4 | u32 | flags | names from `inFlagsToString`: bit0 ENABLED, bit1 BRAKE_DISABLE, bit16 TIMEOUT, bit17 BUTTON, bit20 RRQ, bit22 CHARGING, bit23 CHARGE (charger detected), bit25 TEMP, bit26 GYRO, bit27 SPI, bit28 DRIVE, bit29 OV, bit30 UV, bit31 OC (bits 24–31 are also the fault code). Our Beam+ always shows ENABLED + TIMEOUT, also while driving fine **[tested]** |
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
| 72, 76 | i32 | encoder L, R (position). L = left wheel, R = right (seen from behind); the motors are mirrored, so forward is + on L and − on R **[tested]** | lin = (L − R) × 0.0040578907, ang = −(L + R) × 0.019415744. The driver **aborts** if \|count\| > 0x7F000000 |
| 80 | fixed | capacityMax | /65536 |
| 84 | u64 | velocityLimiterTag | **not an echo**: the board's limiter clock, ~2000 counts per second in the upper 32 bits (about 730 million on ours). Send it back as the velocity tag **[tested]** |
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

## 7. Open questions

1. **How the limiter scales speed.** The board drives only with a fresh limiter-clock tag (§8), but it reports
   0 in the limit, limited-velocity and latency fields (offsets 100–118), even while driving. How stale a tag
   may be, and whether a stale-ish one slows the robot rather than stopping it, is unknown.
2. **The "actual velocity" fields (92, 96)** don't match the encoders: on blocks, turns showed up as linear
   speed and angular read 0. Use the encoders until this is understood.
3. Meaning of the fault byte, and of the TIMEOUT flag (always set on our Beam+).
4. Dock-id and controller-status layouts (only needed for auto-docking). The controller status
   (`0xCCCC4444`) has, per the library's printer: sensors, slip, `l_cmd`/`a_cmd`, linear and angular PID terms
   (p, i, d, ff, out), linear/angular limits (odo, lim, sd, cd) and a `ttl`.
5. Whether the board can be told to release the motors when idle (see the jitter in §8).

## 8. Tested on a real Beam+ (Oct 2026)

**Driving needs the limiter-clock tag [tested].** With any other tag (0, a counter, the `timeStamp` µs clock,
or variants of it) the board ignores velocity commands completely: no encoder movement at up to 0.5 m/s.
Echoing the latest status's `velocityLimiterTag` (offset 84) as the command tag makes both wheels drive.
That's how Beam's remote pilot did it: it echoed the value, so the board could tell how old a command was.

**Only use driving mode 1 [tested].** Mode 0 does nothing. Modes 2 and 3 do something else: a short
command made **one wheel spin on its own for ~3 s at a fixed speed**, a different wheel on different runs,
then stop by itself. It looks like a gyro-steered "turn by angle" that never completes on blocks. Even
`DrivingListener` only ever sends mode 1 (it sends code `0x03` to switch the motors off in even modes).

**Wheels and directions [tested].** Status/encoder "L" is the left wheel and "R" the right, seen from behind
(screen = front). The motors are mirrored: forward is + counts on L, − on R. This matches the library's
`lin = (L − R) × k`.

**The motor controller sleeps until the first fresh command [tested].** After a full power-off (battery out)
the board reports exactly 0 currents and speeds and the wheels are still. After the first accepted
command, it actively holds the wheels at the commanded speed. With the wheels in the air that hold loop
"hunts": the wheels twitch back and forth and currents swing up to ~±9000 raw. Expected to settle under
the robot's weight on the floor (not yet tested).

**Don't experiment with odd commands while it drives [tested].** After a gyro-off command plus mode-1
commands with tag 0, the board's motor data froze (encoders and currents stuck) while the rest of it kept
answering. Only a full power-off (battery out ~30 s) recovered it. A PC reboot doesn't reset the board:
it's the power controller.

**The power watchdog works as described [tested].** When the board's USB link dropped (a loose mini-USB
cable on J8 of the TX011), refreshes stopped getting through and the board cut power 5 min 19 s later.
While the link is down it keeps doing that after every boot. Secure that cable: lifting the robot's top
can pull it out.

**On the floor it behaves [tested].** With the limiter-clock tag, mode 1 and gyro mode on, through the motor
server: forward and backward 0.1 m/s for 1 s moved 8 cm straight, with no heading change; turning left
0.3 rad/s for 2.5 s turned 34°, and right 26°. Every pulse stopped within ~0.25 s of the last command,
with no twitching and no run-on. Everything odd on blocks (twitching, a wheel running on for seconds,
straight commands turning) came from the board steering by its gyro while the body couldn't move.
Bench tests on blocks can only show direction; judge behaviour on the floor.

**Turning accelerates slowly [tested].** A 1 s turn command at 0.3 rad/s turned only ~4°: the board limits
angular acceleration (`getLimits` has a 0.3 that fits rad/s²). Hold a turn for 2+ s to reach full rate.

**Odometry needs calibrating [tested].** For the 34° turn, the gyro (`integratedYaw`) said 34° while the
encoders with Beam's `−(L + R) × 0.019415744` gave ~23°. Prefer the gyro for heading until this is sorted.
