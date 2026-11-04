#!/bin/bash
# Set up the Beam+ brain on an Ubuntu/Debian host (run as the user who will own it, with sudo).
#   brain-host/install.sh <robot ws url> <brain key>
# e.g. brain-host/install.sh wss://beam-lab.<your-tailnet>.ts.net/ws "$(cat ~/beam-brain/brain.key)"
set -euo pipefail
URL=${1:?robot WebSocket URL}
KEY=${2:?brain key}
PROJECT=$(cd "$(dirname "$0")/.." && pwd)
HOME_DIR=$HOME
VENV=$HOME/beam-brain-venv

sudo apt-get install -y -qq python3-opencv python3-aiohttp python3-venv espeak-ng >/dev/null
[ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV"
# Voice; speech recognition for Rocky (its models download on first start, pinned in beambrain/ears.py).
# Pinned versions: speech.py needs piper-tts's synthesize_wav API. To upgrade, change a version and test.
"$VENV/bin/pip" install -q piper-tts==1.8.0 faster-whisper==1.2.1

# Models: each pinned to a fixed revision and checked against its SHA-256, like the speech models in ears.py.
# To upgrade one: change the revision in its URL, download it, check it yourself, update the hash.
M=$HOME/beam-brain/models
mkdir -p "$M" "$HOME/beam-brain/data"
fetch() {   # name url sha256: kept if it already matches, else downloaded; a mismatch stops the install
  if [ -s "$M/$1" ] && echo "$3  $M/$1" | sha256sum -c --status; then return; fi
  curl -fsSL -o "$M/$1.part" "$2"
  if ! echo "$3  $M/$1.part" | sha256sum -c --status; then
    rm -f "$M/$1.part"
    echo "$1: the download doesn't match its pinned SHA-256; not installing it" >&2
    exit 1
  fi
  mv "$M/$1.part" "$M/$1"
}
ZOO=https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models
PIPER=https://huggingface.co/rhasspy/piper-voices/resolve/e7229f7d33a10e145e109b886d661984201ac3c4/en/en_US/lessac/medium
fetch face_detection_yunet_2023mar.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch face_recognition_sface_2021dec.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
fetch en_US-lessac-medium.onnx "$PIPER/en_US-lessac-medium.onnx" \
  5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f
fetch en_US-lessac-medium.onnx.json "$PIPER/en_US-lessac-medium.onnx.json" \
  efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0
chmod 700 "$HOME/beam-brain/data"

# Written once: re-running this keeps the file, and with it any BEAM_BRAIN_ARGS (--home-url, --home-drivers, ...).
if [ -e /etc/beam-brain.env ]; then
  echo "keeping /etc/beam-brain.env (edit it to change the robot URL or key)"
else
  printf 'BEAM_BRAIN_URL=%s\nBEAM_BRAIN_KEY=%s\nBEAM_BRAIN_ARGS=\n' "$URL" "$KEY" | sudo tee /etc/beam-brain.env >/dev/null
fi
sudo chmod 600 /etc/beam-brain.env
sed -e "s|__USER__|$USER|" -e "s|__PROJECT__|$PROJECT|" -e "s|__PYTHON__|$VENV/bin/python|" -e "s|__HOME__|$HOME_DIR|g" \
  "$PROJECT/brain-host/beam-brain.service" | sudo tee /etc/systemd/system/beam-brain.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now beam-brain.service
sleep 3
systemctl --no-pager --lines=5 status beam-brain.service || true
