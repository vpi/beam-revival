"""Load the Beam+ camera firmware into the cameras' RAM.

The Beam+ head and nav cameras are Cypress FX3 chips. At power-on they run a small bootloader from
their flash and enumerate as 25a1:4006 (head) and 25a1:4007 (nav), which aren't cameras yet. The
original Beam software then sent the camera firmware (cleveland_head.elf / cleveland_nav.elf) into
their RAM, after which they re-enumerate as ordinary UVC webcams, 25a1:0006 and 25a1:0007.

This does the same with Cypress's standard FX3 boot protocol: vendor request 0xA0 writes RAM in
2 KB chunks (address in wValue/wIndex), and a final empty 0xA0 at the entry point starts the
firmware. Only RAM is written, never flash: a power-cycle returns the camera to its bootloader.

The firmware is Suitable Technologies' and isn't distributed here: `beam-setup cameras` copies it
from the robot's own internal disk.

    python3 -m beamctl.fx3load head /path/to/cleveland_head.elf
    python3 -m beamctl.fx3load --status
"""

import argparse
import ctypes
import fcntl
import os
import struct
import sys
import time

VENDOR = 0x25A1
CAMERAS = {  # name: (bootloader product id, camera product id, firmware file)
    "head": (0x4006, 0x0006, "cleveland_head.elf"),
    "nav": (0x4007, 0x0007, "cleveland_nav.elf"),
}

FX3_REQUEST = 0xA0
VENDOR_OUT = 0x40
CHUNK = 2048
TIMEOUT_MS = 5000


class _CtrlTransfer(ctypes.Structure):
    """struct usbdevfs_ctrltransfer from <linux/usbdevice_fs.h>."""
    _fields_ = [("bRequestType", ctypes.c_uint8), ("bRequest", ctypes.c_uint8),
                ("wValue", ctypes.c_uint16), ("wIndex", ctypes.c_uint16),
                ("wLength", ctypes.c_uint16), ("timeout", ctypes.c_uint32),
                ("data", ctypes.c_void_p)]


# _IOWR('U', 0, struct usbdevfs_ctrltransfer)
USBDEVFS_CONTROL = (3 << 30) | (ctypes.sizeof(_CtrlTransfer) << 16) | (ord("U") << 8) | 0


def find_devices(product_id):
    """USB device nodes (/dev/bus/usb/BBB/DDD) with our vendor id and this product id."""
    found = []
    root = "/sys/bus/usb/devices"
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        try:
            with open(os.path.join(path, "idVendor")) as f:
                vid = int(f.read(), 16)
            with open(os.path.join(path, "idProduct")) as f:
                pid = int(f.read(), 16)
            if (vid, pid) != (VENDOR, product_id):
                continue
            with open(os.path.join(path, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(path, "devnum")) as f:
                dev = int(f.read())
        except (OSError, ValueError):
            continue
        found.append("/dev/bus/usb/%03d/%03d" % (bus, dev))
    return found


def elf_segments(data):
    """(entry point, [(address, bytes)]) for the loadable segments of a 32-bit little-endian ELF."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise ValueError("not a 32-bit little-endian ELF file")
    entry, phoff = struct.unpack_from("<II", data, 24)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    segments = []
    for i in range(phnum):
        p_type, p_offset, _vaddr, p_paddr, p_filesz = struct.unpack_from("<5I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:  # PT_LOAD; .bss is zeroed by the firmware's own start-up code
            segments.append((p_paddr, data[p_offset:p_offset + p_filesz]))
    if not segments:
        raise ValueError("no loadable segments")
    return entry, segments


def _control(fd, address, payload=b""):
    buf = ctypes.create_string_buffer(payload, len(payload)) if payload else None
    xfer = _CtrlTransfer(VENDOR_OUT, FX3_REQUEST, address & 0xFFFF, address >> 16, len(payload),
                         TIMEOUT_MS, ctypes.cast(buf, ctypes.c_void_p) if buf else None)
    fcntl.ioctl(fd, USBDEVFS_CONTROL, xfer)


def load(device, firmware):
    """Write the firmware into the FX3's RAM and start it. The device then disconnects."""
    entry, segments = elf_segments(firmware)
    fd = os.open(device, os.O_RDWR)
    try:
        for address, blob in segments:
            for off in range(0, len(blob), CHUNK):
                _control(fd, address + off, blob[off:off + CHUNK])
        try:
            _control(fd, entry)
        except OSError:
            pass  # the FX3 may jump before it acknowledges; it re-enumerates either way
    finally:
        os.close(fd)
    return entry, sum(len(b) for _, b in segments)


def status():
    out = {}
    for name, (boot_pid, cam_pid, _) in CAMERAS.items():
        out[name] = "camera" if find_devices(cam_pid) else "bootloader" if find_devices(boot_pid) else "missing"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("camera", nargs="?", choices=sorted(CAMERAS), help="which camera to load")
    ap.add_argument("firmware", nargs="?", help="path to the camera's .elf firmware")
    ap.add_argument("--status", action="store_true", help="show whether each camera is loaded")
    ap.add_argument("--wait", type=float, default=10, help="seconds to wait for the camera to appear")
    args = ap.parse_args(argv)

    if args.status or not args.camera:
        for name, state in status().items():
            print("%-5s %s" % (name, state))
        return 0
    if not args.firmware:
        ap.error("firmware path required")

    boot_pid, cam_pid, _ = CAMERAS[args.camera]
    if find_devices(cam_pid):
        print("%s camera already running" % args.camera)
        return 0
    devices = find_devices(boot_pid)
    if not devices:
        print("%s camera not found (no 25a1:%04x)" % (args.camera, boot_pid), file=sys.stderr)
        return 1
    with open(args.firmware, "rb") as f:
        firmware = f.read()
    for device in devices:
        entry, size = load(device, firmware)
        print("%s: loaded %d bytes into %s, started at 0x%08x" % (args.camera, size, device, entry))
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if find_devices(cam_pid):
            print("%s camera is up (25a1:%04x)" % (args.camera, cam_pid))
            return 0
        time.sleep(0.25)
    print("%s camera did not appear within %.0f s" % (args.camera, args.wait), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
