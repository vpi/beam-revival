#!/bin/bash
# Refresh an already-built image with the current beamctl code and image/rootfs files,
# without rebuilding the whole system (seconds instead of ~10 minutes).
#
#   sudo image/update-image.sh /var/tmp/beam-usb-stable.img   (or beam-usb-lab.img)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(dirname "$HERE")
IMG=${1:?which image: /var/tmp/beam-usb-stable.img or /var/tmp/beam-usb-lab.img}
MNT=$(mktemp -d)
LOOP=$(losetup -fP --show "$IMG")
trap 'umount "$MNT" 2>/dev/null; losetup -d "$LOOP"; rmdir "$MNT"' EXIT
mount "${LOOP}p2" "$MNT"
rm -rf "$MNT/opt/beam-revival/beamctl"
cp -r "$PROJECT/beamctl" "$PROJECT/docs" "$PROJECT/README.md" "$MNT/opt/beam-revival/"
find "$MNT/opt/beam-revival" -name __pycache__ -prune -exec rm -rf {} +
chroot "$MNT" python3 -m compileall -q /opt/beam-revival/beamctl
cp -r "$HERE/rootfs/." "$MNT/"
chmod 755 "$MNT/usr/local/sbin/beam-setup" "$MNT/usr/local/bin/beam-kiosk"
umount "$MNT"
rm -f "$IMG.xz"
xz -T0 -6 -k "$IMG"
ls -la "$IMG" "$IMG.xz"
