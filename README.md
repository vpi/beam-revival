# beam-revival

A local, cloud-free control stack for the Suitable Technologies **Beam+** telepresence robot,
whose cloud service is gone.

> **Unofficial hobby project.** Not affiliated with, endorsed by or supported by Suitable Technologies,
> Blue Ocean Robotics, GoBe Robots or Awabot. "Beam" and "Beam+" are their trademarks. This repository
> contains no Beam software, firmware or disk images. The motor-board protocol notes in
> `docs/motor-protocol.md` describe how to talk to the hardware, for interoperability with robots people
> already own. Use at your own risk: a robot that moves can hurt people, pets and itself. Test with the
> wheels off the ground, and never leave the AI driving unsupervised.

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

**Dev VM:** `https://beam-dev.<your-tailnet>.ts.net/` serves the simulator setup. It runs with
`--allow-remote-robot` so `/robot?test=1` can be opened from another machine. Don't use that flag on the robot.

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
python3 -m pytest -q        # 29 tests, ~20 s
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

## First session on the real robot (wheels off the ground)

Use the USB stick ([docs/robot-usb.md](docs/robot-usb.md)). It boots everything and backs up the
internal disk automatically. Then:

1. Check the board is seen: the robot screen shouldn't say "Motor board not connected"
   (or run `lsusb | grep 25a1:000b` over SSH).
2. On blocks, drive gently. Check direction (forward = forward, left = left), stop behaviour,
   and battery readings.
3. Settle the **limiter tag** question (spec, section 7): compare `limited_linear` in
   `/api/status` with the commanded speed under `--tag-mode board_time`, `zero` and `counter`
   (edit `/etc/beam/server.env`, then `sudo systemctl restart beam-server`).
4. Check that the head camera is the large picture on the driving page, and that sound works both ways.

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
