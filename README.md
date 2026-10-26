# beam-revival

A local, cloud-free control stack for the Suitable Technologies **Beam+** telepresence robot,
whose cloud service is gone.

> **Unofficial hobby project.** Not affiliated with, endorsed by or supported by Suitable Technologies,
> Blue Ocean Robotics, GoBe Robots or Awabot. "Beam" and "Beam+" are their trademarks. This repository
> contains no Beam software, firmware or disk images. The motor-board protocol notes in
> `docs/motor-protocol.md` describe how to talk to the hardware, for interoperability with robots people
> already own. Use at your own risk: a robot that moves can hurt people, pets and itself. Test with the
> wheels off the ground, and never leave the AI driving unsupervised.

## What works

Tested on a real Beam+ (October 2026), running from a USB stick, with the robot's own disk untouched:

- **Driving** from a browser on your laptop or phone: keyboard, on-screen joystick or gamepad, with
  a dead-man stop, E-STOP, and speed limits
- **Live video** from the head and floor cameras (two-way audio is set up, but not yet confirmed on the real robot)
- **Charging** on the original dock, and **remote access** from anywhere through Tailscale, with
  nothing exposed to the internet
- Experimental, so far only tried against the simulator: face recognition with spoken greetings, and AI
  driving ([docs/brain.md](docs/brain.md))

Not tested: the BeamPro (it uses the same motor board protocol, so it may work).

## Quick start

### You need

- A **Beam+** with a working battery. Ours was dead after years in storage; a standard 12 V 18 Ah
  sealed lead-acid battery (ML18-12) replaced it.
- A **USB stick**, 16 GB or more, and a **USB keyboard** (only to pick the boot device).
- A **Linux machine** to build the image: Ubuntu 24.04+ or Debian 12+, real or a virtual machine,
  with 15 GB free. Building takes about 20 minutes.
- Your **Wi-Fi** name and password.
- A free **[Tailscale](https://tailscale.com)** account, to drive from your devices over HTTPS
  (recommended; plain home-network access is possible too).
- Something to put the robot **on blocks**, so its wheels are off the ground for the first tests.

### Steps

All in [docs/robot-usb.md](docs/robot-usb.md), step by step:

1. **Build the image** on the Linux machine: install a few packages, clone this repo, run
   `sudo image/build-image.sh`.
2. **Write it to the USB stick**, with Rufus on Windows or `dd` on Linux.
3. **Fill in `beam.conf`** on the stick (it shows up as a drive called BEAMCONFIG): your Wi-Fi,
   and optionally a Tailscale auth key and a lower top speed.
4. **Boot the robot from the stick**, wheels off the ground. Use the boot-menu key, usually F11 or F12.
   The robot's screen shows a Tailscale login link (unless you gave an auth key), then the address
   to drive it from.
5. **Drive.** Open that address, check the wheels turn the right way, then put it on the floor and
   drive slowly.

The robot's own software is untouched: take the stick out and it boots Beam's original system again.

### Why there's no ready-made image to download

You build the stick yourself from this repository. That takes about 20 minutes, and it means you can
see exactly what ends up on your robot: one readable script (`image/build-image.sh`), this project's
code, and packages fetched from Debian's and Tailscale's official servers. The image then contains only
your own SSH key and settings. A ready-made image would ask you to trust a large file from a stranger,
which nobody can practically inspect, on a device with cameras and a microphone in your home.

**No robot?** You can try the driving page against the simulator (see
[Run it against the simulator](#run-it-against-the-simulator)).

## What's in here

| Part | What it does |
|---|---|
| `beamctl/protocol.py` | The motor board wire protocol: commands, status packets, STM32 CRC. Spec in [docs/motor-protocol.md](docs/motor-protocol.md). |
| `beamctl/server.py` | **Motor server.** Runs on the robot, owns the motor board, and serves the driving page. Sends drive commands at 100 Hz, keeps the power watchdog refreshed, and enforces the safety rules. |
| `beamctl/sim.py` | **Simulator.** A fake motor board on a pseudo-terminal, so everything can be developed without the robot. |
| `beamctl/web/index.html` | **Driving page.** Live video from both cameras, robot audio, Talk, keyboard (WASD/arrows), on-screen joystick, gamepad, e-stop, telemetry. Works on phones. |
| `beamctl/web/robot.html` | **Robot screen.** Runs in a full-screen browser on the robot. Captures the cameras and microphone, plays the driver's voice, shows a friendly face. |
| `beamctl/signaling.py` | Relays WebRTC connection setup between the robot screen and driving pages. No media passes through the server. |
| `tests/` | Protocol unit tests, plus integration tests that run the real server against the simulator, and signaling tests. |
| `image/` | **Robot USB stick.** Builds a bootable Debian image that starts the server, robot screen and Tailscale on boot, in two variants: stable and lab (experimental). See [docs/robot-usb.md](docs/robot-usb.md). |
| `beambrain/`, `brain-host/` | **Brain (experimental).** Face recognition and spoken greetings, running on another computer. See [docs/brain.md](docs/brain.md). |

## How video and audio work

```
 Driving page (your laptop/phone)                    Robot
┌──────────────────────────────┐  WebRTC (direct, ┌──────────────────────────────┐
│ head + floor camera, robot   │◀── peer-to-peer)─│ robot.html in Chromium kiosk │
│ audio; your mic when Talk on │─────────────────▶│ cameras, mic, speaker, screen│
└──────────────┬───────────────┘                  └───────────────┬──────────────┘
               │  WebSocket: driving + connection setup           │
               └────────────────▶ beamctl.server ◀─────────────────┘
```

A browser on the robot handles the cameras and audio. That gives hardware video encoding and,
more importantly, **echo cancellation**: the robot's speaker sits next to its microphone, so
without it you'd hear yourself back. The server only relays the setup messages.

The robot page picks the head camera (`25a1:0006`) and floor camera (`25a1:0007`) by USB id.
Override with `/robot?head=<label text>&nav=<label text>` if needed.

**Talk (your microphone) needs HTTPS.** Browsers only allow the microphone on secure pages.
Seeing and hearing the robot works over plain HTTP. Use Tailscale (below), or for LAN-only use
`scripts/make-cert.sh <robot-ip>` and start the server with
`--tls-cert certs/beam.crt --tls-key certs/beam.key` (accept the browser warning once).

## Remote access with Tailscale (recommended)

Tailscale gives you a private network between your own devices, and `tailscale serve` adds a
trusted HTTPS certificate. Together they let you drive from anywhere, with Talk working, and
nothing exposed to the public internet.

On the robot (or the dev VM):

```bash
# Install from Tailscale's official repo (see tailscale.com/download/linux), then:
sudo tailscale up --hostname=beam          # prints a link: open it and approve the device
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
```

Then open `https://beam.<your-tailnet>.ts.net/` on any device signed in to your Tailscale
account (phone app, laptop). The serve setting persists across reboots, and `sudo tailscale serve --https=443 off` removes it.

- Your tailnet needs MagicDNS and HTTPS certificates enabled (admin console → DNS).
- **Don't use `tailscale funnel`.** That publishes the page to the whole internet.
- Behind `tailscale serve`, every visitor reaches the server from 127.0.0.1. The server
  recognises the proxy headers and still refuses the robot-screen role to remote visitors.
  The robot's own kiosk keeps using `http://localhost:8080/robot`.
- Video goes peer-to-peer over Tailscale too. On networks that block direct connections,
  Tailscale relays it (DERP), which still works but adds some latency.

## Run it against the simulator

Needs Python 3.10+ and `aiohttp` (`sudo apt install python3-aiohttp`; for tests also `python3-pytest python3-pytest-asyncio`).

```bash
scripts/dev-run.sh          # starts the simulator + server on :8080
scripts/dev-run.sh stop
```

Open `http://<host>:8080/` and drive. For video without the robot, open
`http://<host>:8080/robot?test=1` in another tab: it streams moving test patterns and a beep
in place of the cameras. Logs are in `/tmp/beam-sim.log` and `/tmp/beam-server.log`.
Simulator options (`python3 -m beamctl.sim --help`) can inject corrupted or dropped replies.

```bash
python3 -m pytest -q        # 65 tests, about a minute and a half
```

## Safety behaviour

- **Dead-man:** if the page stops sending for 0.5 s (tab closed, Wi-Fi dropout, lost focus), the target speed goes to 0. Beam's own software waited 5 s.
- **Releasing every control** sends an explicit stop. Disconnecting the page also stops the robot immediately.
- **E-stop:** the STOP button, `Space` or gamepad B. It stays latched until released on the page.
- **Limits and ramps:** 0.6 m/s by default (`--max-linear`, capped at the board's 1.0), 1.0 rad/s, and smooth acceleration with faster braking.
- **One driver at a time:** a second page gets "another driver has control" while the first is active.
- **Board link loss:** after 2 s of silence the server reopens the port and restarts from speed 0.
- **Power watchdog:** on opening the port the server immediately sends Beam's "power-cycle in 300 s" order and re-sends it every 5 s. Without this, the robot powers off about 5 minutes after you boot your own OS (see the spec, section 6).
- **No authentication by default.** Use `--token SECRET` and open `http://robot:8080/?token=SECRET` for anything beyond your home LAN.

## Things that may surprise you on the real robot

- **On blocks the wheels twitch,** and turns keep going for a second or two after you let go. The motor
  board steers by its gyro, which can't work while the robot's body doesn't move. Judge only
  directions on blocks; on the floor it drives and stops cleanly.
- **Turns start slowly.** The board ramps up turning speed: hold the key rather than tapping it.
- **If the motor board isn't connected, the robot powers itself off about 5 minutes after boot.**
  That's the board's own watchdog, which our server normally keeps refreshing. Check the small USB
  cable on the motor board in the base: lifting the robot's top can pull it loose.
- **Cameras appear a few seconds after the screen.** They need their firmware loaded at every
  power-on; the stick copies it from the robot's own disk the first time.

What we learned about the hardware is in [docs/motor-protocol.md](docs/motor-protocol.md), section 8.

## Third-party components

Nothing below is bundled in this repository. Each is installed or downloaded separately and keeps
its own licence:

- Python packages: aiohttp, NumPy, OpenCV, pytest (installed from your distribution or PyPI)
- Face models **YuNet** and **SFace** from the [OpenCV model zoo](https://github.com/opencv/opencv_zoo),
  downloaded by `brain-host/install.sh`
- Text to speech: [Piper](https://github.com/OHF-Voice/piper1-gpl) (`piper-tts`, GPL-3.0) and its
  `en_US-lessac-medium` voice (see that voice's model card on Hugging Face), or `espeak-ng` as a fallback.
  They run as a separate, optional dependency of the brain.
- Robot USB image: Debian 13, Chromium, cage, PipeWire, Tailscale and the other Debian packages listed in
  `image/build-image.sh`, fetched from their official repositories when you build the image
- AI models: any OpenAI-compatible provider. The bake-off was run on [Synthetic](https://synthetic.new).
  Bring your own API key; it's never stored in this repository.

## Licence

MIT (see `LICENSE`).
