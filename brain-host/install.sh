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
"$VENV/bin/pip" install -q piper-tts

M=$HOME/beam-brain/models
mkdir -p "$M" "$HOME/beam-brain/data"
fetch() { [ -s "$M/$1" ] || curl -fsSL -o "$M/$1" "$2"; }
fetch face_detection_yunet_2023mar.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
fetch face_recognition_sface_2021dec.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
fetch en_US-lessac-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
fetch en_US-lessac-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
chmod 700 "$HOME/beam-brain/data"

printf 'BEAM_BRAIN_URL=%s\nBEAM_BRAIN_KEY=%s\nBEAM_BRAIN_ARGS=\n' "$URL" "$KEY" | sudo tee /etc/beam-brain.env >/dev/null
sudo chmod 600 /etc/beam-brain.env
sed -e "s|__USER__|$USER|" -e "s|__PROJECT__|$PROJECT|" -e "s|__PYTHON__|$VENV/bin/python|" -e "s|__HOME__|$HOME_DIR|g" \
  "$PROJECT/brain-host/beam-brain.service" | sudo tee /etc/systemd/system/beam-brain.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now beam-brain.service
sleep 3
systemctl --no-pager --lines=5 status beam-brain.service || true
