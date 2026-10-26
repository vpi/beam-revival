#!/bin/bash
# Update a running robot (booted from our USB stick) with the current beamctl code and image/rootfs files,
# like image/update-image.sh does for an image file. Keeps the old code in /var/tmp for rollback.
#
#   scripts/deploy-robot.sh beam@beam [ssh options...]
#
# Restarts the motor server (a second or two without drive commands; the power watchdog is refreshed
# again as soon as it's back) and the robot screen.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(dirname "$HERE")
TARGET=${1:?usage: deploy-robot.sh user@robot [ssh options...]}
shift

tar -C "$PROJECT" -czf - --exclude=__pycache__ beamctl docs README.md -C image rootfs \
  | ssh "$@" "$TARGET" 'set -euo pipefail
      STAGE=$(mktemp -d); trap "rm -rf $STAGE" EXIT
      tar -xzf - -C "$STAGE"
      sudo cp -a /opt/beam-revival/beamctl "/var/tmp/beamctl.bak.$(date +%Y%m%d-%H%M%S)"
      sudo rm -rf /opt/beam-revival/beamctl
      sudo cp -r "$STAGE/beamctl" "$STAGE/docs" "$STAGE/README.md" /opt/beam-revival/
      sudo python3 -m compileall -q /opt/beam-revival/beamctl
      sudo cp -r "$STAGE/rootfs/." /
      sudo chmod 755 /usr/local/sbin/beam-setup /usr/local/bin/beam-kiosk
      sudo systemctl daemon-reload
      for unit in "$STAGE"/rootfs/etc/systemd/system/beam-*.service; do sudo systemctl enable -q "$(basename "$unit")"; done
      sudo udevadm control --reload
      sudo systemctl restart beam-server
      sudo systemctl start beam-cameras || true
      sudo pkill -x cage || true          # robot screen restarts with the new page (getty logs in again)
      sleep 3
      systemctl is-active beam-server'
