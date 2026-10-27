# Phone panel: an old Android phone as touch screen and rear camera

Optional. A spare Android phone, mounted on the robot below the screen and plugged into its spare
USB port, becomes:

- **a touch screen for people in the room** (the Beam+ screen isn't touch): who's connected, a clear
  "camera on" warning, **Call owner**, speaker **volume**, and **Sleep / Wake**. There are no driving
  controls on it on purpose: only the remote driver drives.
- **a rear camera**: its back camera looks behind the robot, which has none of its own. Drivers see it
  as a mirrored **REAR** picture at the top right of the video; click it to enlarge it. The 💡 button
  on it switches the phone's flash on as a light behind the robot, handy for backing onto the charger
  in the dark. It goes off by itself when the last driver leaves or the robot sleeps.

The robot controls the phone over the one USB cable with Android's debug bridge (`adb`), forwards its
own web server to the phone, and keeps the phone charged. Nothing is installed on the phone: it's a
page in its Chrome. The rear camera's video goes from the phone to the robot over your Wi-Fi, so the
phone must be on the same network as the robot. The robot passes it on to drivers, wherever they are.

Without a phone plugged in, nothing changes and nothing runs.

## What works

Tested with a JamboPhone JP1 (Android 13, Chrome 136). Any Android phone from roughly the last five
years with USB-C and Chrome should do: it needs USB debugging, and Chrome for the page and the camera.

## Setting it up

On the phone, once:

1. **Settings → About phone**: tap *Build number* seven times to turn on Developer options.
2. **Settings → System → Developer options**: turn on **USB debugging**.
3. **Settings → Security → Screen lock: None.** With a PIN or pattern the robot can't bring the panel
   back, and people in the room can't reach the Wake button.
4. Plug the phone into the robot's spare USB port (the one used for the keyboard).
5. The phone asks **"Allow USB debugging?"**: tick **Always allow from this computer** and tap **Allow**.
   It's the robot asking. If nothing appears, unlock the phone's screen.
6. Chrome opens the panel. Tap **Tap to start** (full screen).
7. The first time a driver connects, Chrome asks for the **camera**: tap **Allow**.

Each new USB stick has its own key, so step 5 comes once per stick.

## How it behaves

| Robot | Phone |
|---|---|
| Awake, nobody connected | panel on screen, "Nobody is watching", rear camera **closed** |
| Awake, a driver connected | amber "Camera on · *name* can see and hear you", rear camera streaming |
| **Call owner** pressed | every connected driving page shows a banner, plays a chime and blinks its tab |
| Asleep | phone screen off, camera closed. Press the phone's power button to get the **Wake up** button; the screen goes dark again after 30 s |

The robot keeps the screen on while awake and reopens the panel if Chrome is closed or the phone is
replugged (the `beam-phone` service: `journalctl -u beam-phone`).

## Power and the phone's battery

The robot's USB port gives 0.5 A. With the screen on and the camera streaming, a phone can need about
that much, so it may charge only slowly or even drain during long drives. The `beam-phone` service logs
the phone's battery level, charging state and temperature every 10 minutes
(`journalctl -u beam-phone | grep battery`). If it drains, power the phone from a small 5 V converter on
the robot's 12 V battery, or a powered USB hub.

An old phone kept charging for months can swell its battery. If the phone has a "limit charging to
80 %" option, turn it on, check the phone now and then, and don't use one whose battery already bulges.

## Mounting

Below the screen, display towards the room, **back camera looking backwards and unobstructed**. Keep
the USB cable short and secured: a pulled cable is the most likely failure.

## Later

With a SIM and data plan, the same cable can give the robot a **cellular connection** when Wi-Fi is out
of range (USB tethering), and the phone can **text you** (low battery, Call owner, stuck). Not built yet.
