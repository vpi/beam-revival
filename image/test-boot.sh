#!/bin/bash
# Boot the USB image in QEMU without modifying it: the stick is a copy-on-write overlay
# attached as a USB mass-storage device, plus a small SATA disk standing in for the robot's
# internal disk (to exercise the backup).
#
#   image/test-boot.sh bios|uefi [image]     then: ssh -p 2222 beam@127.0.0.1
#                                            (the robot's port 8080 is on 127.0.0.1:18080 when LAN_ACCESS=yes)
#   image/test-boot.sh stop
#   image/test-boot.sh screenshot out.png
set -euo pipefail
MODE=${1:-bios}
IMG=${2:-/var/tmp/beam-usb-stable.img}
WORK=/var/tmp/beam-qemu
MON=$WORK/monitor.sock

monitor() { python3 - "$MON" "$1" <<'EOF'
import socket, sys, time
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(0.2); s.recv(4096)
s.sendall((sys.argv[2] + "\n").encode()); time.sleep(1.0); s.close()
EOF
}

case "$MODE" in
  stop)
    [ -f "$WORK/qemu.pid" ] && kill "$(cat "$WORK/qemu.pid")" 2>/dev/null || true
    exit 0 ;;
  screenshot)
    monitor "screendump $WORK/screen.ppm"
    pnmtopng "$WORK/screen.ppm" > "${2:-$WORK/screen.png}" 2>/dev/null
    echo "${2:-$WORK/screen.png}"
    exit 0 ;;
esac

"$0" stop
mkdir -p "$WORK"
rm -f "$WORK/stick.qcow2" "$WORK/internal.raw" "$MON"
# 16 GB virtual stick so first boot has room to grow the root partition.
qemu-img create -q -f qcow2 -b "$IMG" -F raw "$WORK/stick.qcow2" 16G
# Fake internal disk with recognisable content.
truncate -s 4G "$WORK/internal.raw"
printf 'BEAM-INTERNAL-DISK-TEST' | dd of="$WORK/internal.raw" bs=1 seek=1048576 conv=notrunc status=none

FW=()
[ "$MODE" = uefi ] && FW=(-bios /usr/share/ovmf/OVMF.fd)
qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 3072 "${FW[@]}" \
  -device qemu-xhci,id=xhci \
  -drive if=none,id=stick,file="$WORK/stick.qcow2",format=qcow2 \
  -device usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0 \
  -drive if=none,id=internal,file="$WORK/internal.raw",format=raw \
  -device ahci,id=ahci -device ide-hd,drive=internal,bus=ahci.0 \
  -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:18080-:8080 \
  -vga virtio -display none -monitor unix:"$MON",server,nowait \
  -serial file:"$WORK/serial.log" \
  -daemonize -pidfile "$WORK/qemu.pid"
echo "booting ($MODE); serial log in $WORK/serial.log"
