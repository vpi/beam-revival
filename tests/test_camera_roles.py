"""Telling the two Beam+ cameras apart (both are "M034-WDR" to the browser)."""

import errno
import os

import beamctl.server as server
from beamctl.server import camera_roles, cameras_in_use


def fake_camera(sysfs, usb, node, index, pid):
    iface = usb / f"{pid}-1:1.0"
    iface.mkdir(parents=True, exist_ok=True)       # videoN/device points at the USB interface
    dev = iface.parent
    (dev / "idVendor").write_text("25a1\n")
    (dev / "idProduct").write_text(f"{pid}\n")
    d = sysfs / f"video{node}"
    d.mkdir()
    (d / "index").write_text(f"{index}\n")
    (d / "device").symlink_to(iface)


def test_roles_follow_device_order_not_load_order(tmp_path):
    sysfs, usb = tmp_path / "video4linux", tmp_path / "usb"
    sysfs.mkdir()
    # This boot the floor camera's firmware loaded first, so it became video0/1.
    fake_camera(sysfs, usb / "a", 0, 0, "0007")
    fake_camera(sysfs, usb / "a", 1, 1, "0007")          # metadata node: not a camera to the browser
    fake_camera(sysfs, usb / "b", 2, 0, "0006")
    fake_camera(sysfs, usb / "b", 3, 1, "0006")
    assert camera_roles(str(sysfs)) == ["nav", "head"]


def test_no_cameras(tmp_path):
    assert camera_roles(str(tmp_path / "missing")) == []


def test_which_camera_the_page_has_open(tmp_path, monkeypatch):
    sysfs, usb, dev = tmp_path / "video4linux", tmp_path / "usb", tmp_path / "dev"
    sysfs.mkdir()
    dev.mkdir()
    fake_camera(sysfs, usb / "a", 0, 0, "0007")
    fake_camera(sysfs, usb / "a", 1, 1, "0007")
    fake_camera(sysfs, usb / "b", 2, 0, "0006")
    fake_camera(sysfs, usb / "b", 3, 1, "0006")
    for n in range(4):
        (dev / f"video{n}").write_text("")
    held = {"video2"}                                    # the page has the head camera open

    def ioctl(fd, request, arg):
        name = os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
        raise OSError(errno.EBUSY if name in held else errno.ENOTTY, "")
    monkeypatch.setattr(server.fcntl, "ioctl", ioctl)
    assert cameras_in_use(str(sysfs), str(dev)) == ["head"]
    held = {"video0"}
    assert cameras_in_use(str(sysfs), str(dev)) == ["nav"]
    held = set()
    assert cameras_in_use(str(sysfs), str(dev)) == []
    assert cameras_in_use(str(tmp_path / "missing"), str(dev)) == []
