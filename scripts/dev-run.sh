#!/bin/bash
# Start (or restart) the simulated motor board and the motor server for development.
#   scripts/dev-run.sh          start both, server on :8080
#   scripts/dev-run.sh stop     stop both
set -u
cd "$(dirname "$0")/.."
PIDS=/tmp/beam-dev.pids

stop() {
  [ -f "$PIDS" ] || return 0
  while read -r pid; do kill "$pid" 2>/dev/null; done < "$PIDS"
  # Wait for them to exit so the new server isn't refused the board's exclusive lock.
  for _ in $(seq 30); do
    alive=0
    while read -r pid; do kill -0 "$pid" 2>/dev/null && alive=1; done < "$PIDS"
    [ "$alive" = 0 ] && break
    sleep 0.1
  done
  rm -f "$PIDS"
}

stop
[ "${1:-}" = "stop" ] && { echo "stopped"; exit 0; }

# --world: the robot lives in the virtual house; its camera is served on 127.0.0.1:8091 (needs OpenCV).
nohup python3 -m beamctl.sim --link /tmp/beam-sim --world > /tmp/beam-sim.log 2>&1 < /dev/null &
echo $! >> "$PIDS"
for _ in $(seq 20); do [ -e /tmp/beam-sim ] && break; sleep 0.1; done
# --allow-remote-robot: lets you open /robot?test=1 from another machine to fake the robot's cameras.
# A brain key in ~/beam-brain/brain.key enables the brain connection (see brain-host/).
BRAIN=()
[ -s "$HOME/beam-brain/brain.key" ] && BRAIN=(--brain-key "$(cat "$HOME/beam-brain/brain.key")")
nohup python3 -m beamctl.server --device /tmp/beam-sim --port "${PORT:-8080}" --allow-remote-robot \
  --world-url http://127.0.0.1:8091 "${BRAIN[@]}" "$@" \
  > /tmp/beam-server.log 2>&1 < /dev/null &
echo $! >> "$PIDS"
sleep 1.5
echo "--- sim";    tail -n 3 /tmp/beam-sim.log
echo "--- server"; tail -n 5 /tmp/beam-server.log
