# Booting the Beam+ from the beam-revival USB stick

The stick holds a complete Debian 13 system that runs the robot without Beam's cloud:

- the motor server
- the robot screen (Chromium kiosk) with cameras and audio
- Tailscale for driving from anywhere

**The robot's internal disk is never modified.** Remove the stick and it boots Beam's original
software again.

## You need

- A USB stick of **16 GB or more**. A fast one helps, since the system runs from it.
- A **USB keyboard**, only to pick the boot device. A small USB hub helps if the robot has one free port.
- The robot **charged**, and **on blocks or with its wheels off the ground** for the first boot.
- To build the image: a **64-bit Linux machine** (section 0).

## 0. Build the image

There's no ready-made download yet: you build the image yourself. It's one script, and it takes 15–30 minutes.

**You need** a 64-bit PC or virtual machine with **Ubuntu 24.04 or newer, or Debian 12 or newer**, with
`sudo`, an internet connection and **15 GB of free disk space**. A VirtualBox, VMware or Hyper-V VM works.
The script builds the robot's system from official Debian packages: nothing on your machine changes
apart from the packages in step 1 and the files in `/var/tmp`.

1. Install the tools:
   ```bash
   sudo apt update
   sudo apt install -y git debootstrap debian-archive-keyring parted dosfstools e2fsprogs xz-utils curl
   ```

2. Get the code:
   ```bash
   git clone https://github.com/vpi/beam-revival.git
   cd beam-revival
   ```

3. Optional, but recommended: an **SSH key**, so you can log in to the robot later for maintenance.
   If `ls ~/.ssh/id_ed25519.pub` shows nothing, make one (press Enter at each question):
   ```bash
   ssh-keygen -t ed25519
   ```
   You can also add a key later in `beam.conf` (section 2).

4. Build:
   ```bash
   sudo image/build-image.sh --ssh-key ~/.ssh/id_ed25519.pub
   ```
   Leave out `--ssh-key …` if you skipped step 3. It prints a few `===` steps and ends with `done`.
   The result is **`/var/tmp/beam-usb-stable.img`** plus a compressed copy, **`/var/tmp/beam-usb-stable.img.xz`**
   (about 1.2 GB). If a step fails, its details are in `/var/tmp/beam-debootstrap.log` or `/var/tmp/beam-apt.log`.

5. Copy `beam-usb-stable.img.xz` to the computer you'll write the stick with (a shared folder, a USB
   drive, or `scp`), and continue with section 1.

   Or write the stick right here on Linux. Plug it in and find it with `lsblk` (check the size: **this
   erases the device you name**), then:
   ```bash
   sudo dd if=/var/tmp/beam-usb-stable.img of=/dev/sdX bs=4M status=progress conv=fsync
   ```

To update an image you already built with newer code, `sudo image/update-image.sh /var/tmp/beam-usb-stable.img`
takes seconds instead of a full rebuild. To try an image without a robot, `sudo image/test-boot.sh bios`
boots it in QEMU (needs the `qemu-system-x86` and `ovmf` packages).

## 1. Write the image

This erases the stick.

There are two images. Use a separate stick for each:

| Image | Stick | Robot name on Tailscale |
|---|---|---|
| `beam-usb-stable.img` | everyday use | `beam` |
| `beam-usb-lab.img` | experimental: brain, face recognition ([docs/brain.md](brain.md)) | `beam-lab` |

1. Unpack the `.img.xz` to `.img` (6.4 GB) with 7-Zip, or `xz -dk <file>.img.xz`.
2. **Rufus** (recommended): pick the stick under *Device*, click *SELECT* and choose the `.img`,
   then *START*. Use a USB port on the back of the PC.
3. Check it, in PowerShell **as Administrator** (read-only, about 2 minutes). The disk number comes from Disk Management or Rufus:
   ```bash
   powershell -ExecutionPolicy Bypass -File scripts\verify-stick.ps1 -DiskNumber <n>
   ```
   For the lab stick, add `-Image beam-usb-lab.img`.
   It should end with "The stick matches the image".

**Avoid balenaEtcher on Windows.** It failed verification on a good stick, and the stick it
wrote turned out to differ from the image inside the system partition. Rufus wrote the same stick correctly.

## 2. Fill in beam.conf

Unplug and replug the stick. Windows shows a drive called **BEAMCONFIG**. Open `beam.conf` in Notepad:

| Setting | |
|---|---|
| `WIFI_SSID`, `WIFI_PASSWORD` | Your Wi-Fi (2.4 or 5 GHz). Required. |
| `TAILSCALE_AUTHKEY` | Optional. Create one at login.tailscale.com → Settings → **Keys** → *Generate auth key* (one-off, pre-approved). Without it, the robot screen shows a login link to open on your phone instead. |
| `ROBOT_NAME` | Its name on your tailnet. The default `beam` gives `https://beam.<your-tailnet>.ts.net/`. |
| `MAX_SPEED` | Top speed in m/s (0.6 by default; the board's limit is 1.0). Start low. |
| `ACCESS_TOKEN` | Optional extra lock on top of Tailscale. |
| `LAN_ACCESS` | `yes` also serves plain `http://<robot-ip>:8080` at home (no Talk there). |
| `SSH_KEY` | Keys allowed to SSH in as `beam`. The image comes with the beam-revival project key, so Claude can help troubleshoot. **Delete that line if you don't want that.** |

On boot, the robot moves the Wi-Fi password and Tailscale key into its system and replaces
them in the file with `(saved on robot)`. To change Wi-Fi later, edit the file again.

## 3. Boot the robot from the stick

1. Robot off. Plug in the stick and the keyboard.
2. Power on and tap the boot-menu key: usually **F11** or **F12** (some boards use **F7** or **Esc**).
   Pick the USB stick. If no menu appears, **Del** opens the BIOS setup, where you can put USB first in the boot order.
3. The stick boots in about 20–30 s. The screen shows the robot face and status:

| On screen | Meaning |
|---|---|
| **Finish setup** + a login link | Open the link on your phone and approve the robot in Tailscale (only without an auth key). Links expire, so a new one appears every 10 min. |
| **Drive me at** `https://beam.….ts.net/` | Ready. Open that address on any device signed in to Tailscale. |
| *No Wi-Fi set up yet* | `beam.conf` has no Wi-Fi. Fix it on a PC and reboot. |
| *Motor board not connected* | The server can't see the motor board (`25a1:000b`). See troubleshooting. |
| *Camera not found* | Cameras not ready yet: their firmware loads a few seconds after boot. The page retries every 10 s. |
| *Camera firmware not found…* | The firmware couldn't be copied from the robot's internal disk. See troubleshooting. |

**First boot only**, two things happen automatically:
- the system grows to fill the stick
- a **read-only backup of the robot's internal disk** is saved on the stick at
  `/var/lib/beam/backup/` (a few minutes; it runs in the background)

## 4. First drive (wheels off the ground)

1. Open the robot's address and press **W** briefly. The wheels should turn forward. Check left and right.
2. Check sound both ways. The robot's mic plays on your side, and **Talk** sends your voice
   (allow the microphone when the browser asks).
3. Only then put it on the floor, at a low `MAX_SPEED`.

## How it starts

| Service | Job |
|---|---|
| `beam-server` | Motor server. Starts ~5 s after power-on and refreshes the motor board's power watchdog. |
| `beam-config` | Applies `beam.conf` on every boot. |
| `beam-tailscale` | Joins Tailscale, then `tailscale serve` → HTTPS on your tailnet only. |
| `beam-audio` | Beam+ audio chip setup: turns on the speaker amplifier and mic inputs (ported from Beam). |
| `beam-cameras` | Loads the camera firmware into both cameras at boot (and whenever one resets). See below. |
| `beam-firstboot`, `beam-backup` | One-time: grow the partition, new SSH host keys, internal disk backup. |
| tty1 autologin → `beam-kiosk` | The robot screen: Chromium in `cage`, showing `http://localhost:8080/robot`. |

## Maintenance

Use `ssh beam@beam.<your-tailnet>.ts.net`. Password logins are off; keys come from `beam.conf`.

- Logs: `journalctl -u beam-server -f` (also `beam-tailscale`, `beam-audio`, `beam-config`)
- Restart the robot screen: `sudo systemctl restart getty@tty1`
- Software lives in `/opt/beam-revival`. After updating it: `sudo systemctl restart beam-server`
- Rebuild or refresh the image on a Linux machine: `sudo image/build-image.sh`, or the
  quicker `sudo image/update-image.sh` for code-only changes. Test it in QEMU with `image/test-boot.sh bios|uefi`.

## Troubleshooting

- **Robot switches off about 5 minutes after booting the stick.** The motor server isn't
  refreshing the board's power watchdog. Check `journalctl -u beam-server` and `ls -l /dev/beam-motor`.
- **Motor board not connected.** Run `lsusb | grep 25a1`. `25a1:000b` should be listed. If it's
  missing, the board may be off (power button) or in firmware-update mode.
- **No camera.** The Beam+ cameras power up in a bootloader (`lsusb`: `25a1:4006` head, `25a1:4007` floor)
  and only become webcams (`25a1:0006`, `0007`) once their firmware is loaded into their RAM. `beam-cameras`
  does that on every boot. It copies the firmware (Suitable's `cleveland_head.elf` / `cleveland_nav.elf`, not
  included in this project) once from the robot's internal disk, mounted read-only, to
  `/var/lib/beam/camera-firmware/`. Check `journalctl -u beam-cameras`. If the internal disk was wiped, copy the
  two files there from another Beam+ install. Nothing is ever written to the cameras' flash.
- **No sound from the speaker.** `journalctl -u beam-audio` should say "applied … ALC662
  settings". Check volume with `alsamixer` as the kiosk user.
- **Back to the original Beam software:** shut down, remove the stick, power on.
