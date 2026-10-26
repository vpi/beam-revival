#!/bin/bash
# Self-signed HTTPS certificate so pilot browsers allow the microphone (Talk button).
#   scripts/make-cert.sh 192.168.1.50 beam.local
# Then: python3 -m beamctl.server --tls-cert certs/beam.crt --tls-key certs/beam.key ...
# Browsers will warn once about the self-signed certificate; accept it for this address.
# With Tailscale, `tailscale cert <name>.ts.net` gives a trusted certificate instead.
set -eu
cd "$(dirname "$0")/.."
mkdir -p certs
SAN="DNS:localhost,IP:127.0.0.1"
for name in "$@"; do
  if [[ "$name" =~ ^[0-9.]+$ ]]; then SAN="$SAN,IP:$name"; else SAN="$SAN,DNS:$name"; fi
done
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=beam-robot" \
  -addext "subjectAltName=$SAN" -keyout certs/beam.key -out certs/beam.crt 2>/dev/null
chmod 600 certs/beam.key
echo "wrote certs/beam.crt and certs/beam.key for $SAN"
