#!/bin/bash
# Update a running robot (booted from our USB stick) with the current beamctl code and image/rootfs files,
# like image/update-image.sh does for an image file. Keeps the old code in /var/tmp for rollback.
#
#   scripts/deploy-robot.sh [--force-stable] beam-lab@beam-lab [ssh options...]
#
# The STABLE stick only takes clean release tags (git checkout v1.0 first); new work goes to the lab
# stick. --force-stable overrides that. The deployed version is written to /opt/beam-revival/VERSION.
#
# Restarts the motor server (a second or two without drive commands; the power watchdog is refreshed
# again as soon as it's back) and the robot screen.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(dirname "$HERE")
FORCE=0
if [ "${1:-}" = "--force-stable" ]; then FORCE=1; shift; fi
TARGET=${1:?usage: deploy-robot.sh [--force-stable] user@robot [ssh options...]}
shift

VERSION=$(git -C "$PROJECT" describe --tags --always --dirty 2>/dev/null || echo unknown)
# Sticks built before the variant file existed are stable ones: treat "no file" as stable.
VARIANT=$(ssh "$@" "$TARGET" 'cat /etc/beam/variant 2>/dev/null || echo stable')
if [ "$VARIANT" = stable ] && [ "$FORCE" = 0 ]; then
  if ! git -C "$PROJECT" describe --tags --exact-match >/dev/null 2>&1 || [ -n "$(git -C "$PROJECT" status --porcelain)" ]; then
    echo "Not deploying: $TARGET runs the STABLE stick, which only takes clean release tags." >&2
    echo "This code is $VERSION. Deploy to the lab stick, or check out a release tag." >&2
    echo "(--force-stable overrides this.)" >&2
    exit 1
  fi
fi
echo "deploying $VERSION to $TARGET ($VARIANT stick)"

STAMP=$(mktemp -d); trap 'rm -rf "$STAMP"' EXIT
echo "$VERSION" > "$STAMP/VERSION"
tar -C "$PROJECT" -czf - --exclude=__pycache__ beamctl docs README.md -C image rootfs -C "$STAMP" VERSION \
  | ssh "$@" "$TARGET" 'set -euo pipefail
      STAGE=$(mktemp -d); trap "rm -rf $STAGE" EXIT
      tar -xzf - -C "$STAGE"
      sudo cp "$STAGE/VERSION" /opt/beam-revival/VERSION
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
      sudo systemctl restart beam-display || true
      sudo pkill -x cage || true          # robot screen restarts with the new page (getty logs in again)
      sleep 3
      systemctl is-active beam-server'
