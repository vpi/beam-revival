#!/bin/bash
# Build a bootable USB image for the Beam+ robot (Debian 13 "trixie", BIOS + UEFI).
#
#   sudo image/build-image.sh [--ssh-key ~/.ssh/id_ed25519.pub] [--out /var/tmp/beam-usb-stable.img]
#   sudo image/build-image.sh --variant lab --brain-key <key> ...    # experimental stick
#
# Variants: "stable" (default; robot name "beam") and "lab" (robot name "beam-lab", brain key
# pre-filled, separate output file) so the two sticks can live side by side on one tailnet.
#
# Produces <out> and <out>.xz. Write it to a 16 GB+ USB stick with balenaEtcher or Rufus.
# Layout (MBR, so old BIOSes boot it):
#   p1  FAT32 "BEAMCONFIG"  512 MiB  beam.conf (editable on Windows/macOS) + UEFI boot files
#   p2  ext4  "beamroot"    rest     the system; grown to fill the stick on first boot
# Needs: debootstrap, debian-archive-keyring, parted, dosfstools, e2fsprogs, xz-utils, curl.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(dirname "$HERE")
OUT=""
SIZE=6G
SUITE=trixie
MIRROR=http://deb.debian.org/debian
SSH_KEY_FILE=""
VARIANT=stable
BRAIN_KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh-key) SSH_KEY_FILE=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --size) SIZE=$2; shift 2 ;;
    --variant) VARIANT=$2; shift 2 ;;
    --brain-key) BRAIN_KEY=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
case "$VARIANT" in
  stable) ROBOT=beam;     LABEL="Beam+";                 : "${OUT:=/var/tmp/beam-usb-stable.img}" ;;
  lab)    ROBOT=beam-lab; LABEL="Beam+ LAB (experimental)"; : "${OUT:=/var/tmp/beam-usb-lab.img}" ;;
  *) echo "variant must be stable or lab" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

ROOT=$(mktemp -d /var/tmp/beam-root.XXXX)
LOOP=""
cleanup() {
  set +e
  for m in dev/pts dev proc sys run boot/efi; do mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"; done
  mountpoint -q "$ROOT" && umount -l "$ROOT"
  [ -n "$LOOP" ] && losetup -d "$LOOP"
  rmdir "$ROOT" 2>/dev/null
}
trap cleanup EXIT

step() { echo; echo "=== $*"; }

step "Creating ${SIZE} image $OUT"
rm -f "$OUT" "$OUT.xz"
truncate -s "$SIZE" "$OUT"
parted -s "$OUT" mklabel msdos \
  mkpart primary fat32 1MiB 513MiB \
  mkpart primary ext4 513MiB 100% \
  set 1 boot on
LOOP=$(losetup -fP --show "$OUT")
mkfs.vfat -F 32 -n BEAMCONFIG "${LOOP}p1" >/dev/null
mkfs.ext4 -q -L beamroot "${LOOP}p2"
mount "${LOOP}p2" "$ROOT"
mkdir -p "$ROOT/boot/efi"
mount "${LOOP}p1" "$ROOT/boot/efi"

step "debootstrap $SUITE"
debootstrap --arch=amd64 --variant=minbase --components=main,contrib,non-free,non-free-firmware \
  "$SUITE" "$ROOT" "$MIRROR" >/var/tmp/beam-debootstrap.log

for m in dev dev/pts proc sys run; do mount --bind "/$m" "$ROOT/$m"; done
in_root() { chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 "$@"; }

cat > "$ROOT/etc/apt/sources.list" <<EOF
deb $MIRROR $SUITE main contrib non-free non-free-firmware
deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware
EOF
echo "$ROBOT" > "$ROOT/etc/hostname"
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s\n::1\tlocalhost ip6-localhost ip6-loopback\n' "$ROBOT" > "$ROOT/etc/hosts"

step "Installing packages"
in_root apt-get update -qq
in_root apt-get install -y -qq --no-install-recommends \
  systemd-sysv systemd-timesyncd udev dbus dbus-user-session libpam-systemd kmod \
  linux-image-amd64 initramfs-tools intel-microcode amd64-microcode \
  firmware-linux-free firmware-misc-nonfree firmware-atheros firmware-realtek firmware-intel-sound firmware-iwlwifi \
  grub-pc-bin grub-efi-amd64-bin grub2-common \
  network-manager wpasupplicant iw wireless-regdb openssh-server sudo ca-certificates curl gnupg \
  python3 python3-aiohttp \
  chromium cage fonts-dejavu-core xwayland \
  pipewire pipewire-pulse pipewire-alsa wireplumber alsa-utils alsa-tools \
  mesa-va-drivers intel-media-va-driver-non-free i965-va-driver \
  usbutils v4l-utils cloud-guest-utils e2fsprogs dosfstools zstd less nano procps iproute2 iputils-ping \
  >/var/tmp/beam-apt.log

step "Installing Tailscale"
curl -fsSL "https://pkgs.tailscale.com/stable/debian/$SUITE.noarmor.gpg" -o "$ROOT/usr/share/keyrings/tailscale-archive-keyring.gpg"
curl -fsSL "https://pkgs.tailscale.com/stable/debian/$SUITE.tailscale-keyring.list" -o "$ROOT/etc/apt/sources.list.d/tailscale.list"
in_root apt-get update -qq
in_root apt-get install -y -qq tailscale >>/var/tmp/beam-apt.log

step "Installing beam-revival"
mkdir -p "$ROOT/opt/beam-revival"
cp -r "$PROJECT/beamctl" "$PROJECT/docs" "$PROJECT/README.md" "$ROOT/opt/beam-revival/"
find "$ROOT/opt/beam-revival" -name __pycache__ -prune -exec rm -rf {} +
in_root python3 -m compileall -q /opt/beam-revival/beamctl
cp -r "$HERE/rootfs/." "$ROOT/"
chmod 755 "$ROOT/usr/local/sbin/beam-setup" "$ROOT/usr/local/bin/beam-kiosk"

step "Users and services"
in_root useradd -m -s /bin/bash -G sudo beam
in_root useradd -m -s /bin/bash -G video,audio,render,input kiosk
in_root useradd -r -M -s /usr/sbin/nologin -G dialout beamsrv
in_root passwd -l root >/dev/null
in_root passwd -l beam >/dev/null
echo 'beam ALL=(ALL) NOPASSWD:ALL' > "$ROOT/etc/sudoers.d/beam"
chmod 440 "$ROOT/etc/sudoers.d/beam"
# Kiosk: tty1 autologin starts the robot screen.
echo '[ "$(tty)" = /dev/tty1 ] && exec /usr/local/bin/beam-kiosk' > "$ROOT/home/kiosk/.bash_profile"
in_root chown kiosk:kiosk /home/kiosk/.bash_profile
in_root systemctl enable -q beam-firstboot beam-config beam-server beam-tailscale beam-audio beam-backup \
  NetworkManager ssh tailscaled systemd-timesyncd
in_root systemctl --global enable -q pipewire.socket pipewire-pulse.socket wireplumber.service 2>/dev/null || true

step "fstab and bootloader"
ROOT_UUID=$(blkid -s UUID -o value "${LOOP}p2")
EFI_UUID=$(blkid -s UUID -o value "${LOOP}p1")
cat > "$ROOT/etc/fstab" <<EOF
UUID=$ROOT_UUID /         ext4 defaults,noatime,commit=60 0 1
UUID=$EFI_UUID  /boot/efi vfat umask=0022,flush,nofail      0 2
EOF
cat > "$ROOT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=1
GRUB_DISTRIBUTOR="$LABEL (beam-revival)"
GRUB_CMDLINE_LINUX_DEFAULT="quiet consoleblank=0"
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_OS_PROBER=true
GRUB_TERMINAL=console
EOF
in_root grub-install --target=i386-pc --boot-directory=/boot --modules="part_msdos ext2" "$LOOP" 2>&1 | tail -1
in_root grub-install --target=x86_64-efi --efi-directory=/boot/efi --boot-directory=/boot --removable --no-nvram 2>&1 | tail -1
in_root update-initramfs -u -k all >/dev/null
in_root update-grub 2>&1 | tail -1
# grub-probe inside a chroot may name the loop device; make sure the kernel is told the UUID.
sed -i "s|root=/dev/loop[0-9]*p2|root=UUID=$ROOT_UUID|g" "$ROOT/boot/grub/grub.cfg"
grep -q "root=UUID=$ROOT_UUID" "$ROOT/boot/grub/grub.cfg" || { echo "grub.cfg has no root=UUID" >&2; exit 1; }

step "Config partition"
cp "$HERE/bootpart/beam.conf" "$ROOT/boot/efi/beam.conf"
if [ -n "$SSH_KEY_FILE" ]; then
  sed -i "s|^SSH_KEY=.*|SSH_KEY=$(head -1 "$SSH_KEY_FILE")|" "$ROOT/boot/efi/beam.conf"
fi
sed -i "s|^ROBOT_NAME=.*|ROBOT_NAME=$ROBOT|" "$ROOT/boot/efi/beam.conf"
[ -n "$BRAIN_KEY" ] && sed -i "s|^BRAIN_KEY=.*|BRAIN_KEY=$BRAIN_KEY|" "$ROOT/boot/efi/beam.conf"
mkdir -p "$ROOT/etc/beam"
echo "$VARIANT" > "$ROOT/etc/beam/variant"
sed -i 's/$/\r/' "$ROOT/boot/efi/beam.conf"           # Windows line endings for Notepad
cat > "$ROOT/boot/efi/README.txt" <<'EOF'
Beam+ robot USB stick (beam-revival)

1. Open beam.conf in this drive with Notepad and fill in WIFI_SSID and WIFI_PASSWORD.
   Optionally add a Tailscale auth key; otherwise the robot's screen shows a login link.
2. Eject the stick, plug it into the robot with a USB keyboard, and boot from USB.
3. The robot's screen tells you where to drive it from.

Leave the other files on this drive alone: they're the robot's boot loader.
EOF
sed -i "1s/.*/$LABEL robot USB stick (beam-revival)/" "$ROOT/boot/efi/README.txt"
sed -i 's/$/\r/' "$ROOT/boot/efi/README.txt"

step "Cleaning up"
in_root apt-get clean
rm -rf "$ROOT/var/lib/apt/lists/"* "$ROOT/var/log/"*.log "$ROOT/tmp/"*
rm -f "$ROOT/etc/ssh/ssh_host_"*                      # regenerated per robot on first boot
: > "$ROOT/etc/machine-id"; rm -f "$ROOT/var/lib/dbus/machine-id"
rm -f "$ROOT/etc/resolv.conf"                         # NetworkManager writes its own
du -sh --exclude=proc --exclude=sys --exclude=dev "$ROOT" 2>/dev/null | cut -f1 | sed 's/^/root filesystem: /'

cleanup
trap - EXIT
step "Compressing"
xz -T0 -6 -k "$OUT"
ls -la "$OUT" "$OUT.xz"
echo "done"
