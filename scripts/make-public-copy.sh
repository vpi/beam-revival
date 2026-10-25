#!/bin/bash
# Build a publish-ready copy of this project (default: ../beam-revival-public), for GitHub.
#
#   scripts/make-public-copy.sh [destination]
#
# Leaves out everything private or not ours to share, replaces personal names/addresses with
# placeholders, then lists anything that still looks like a secret. Your working folder is untouched.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=${1:-"$SRC/../beam-revival-public"}
MARK=".beam-revival-public-copy"

if [ -e "$DEST" ] && [ ! -e "$DEST/$MARK" ]; then
  echo "refusing to overwrite $DEST: it isn't a previous public copy" >&2
  exit 1
fi
# Empty the folder (keeping the git history of an earlier copy) rather than deleting it: it may be
# open in an editor or terminal.
mkdir -p "$DEST"
find "$DEST" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +

# Never published: real settings, keys, certificates, USB images, test photos, caches, and the
# private list of personal details itself.
(cd "$SRC" && tar \
  --exclude='./beam.conf' --exclude='*.key' --exclude='*.pem' --exclude='./certs' \
  --exclude='./.public-copy-substitutions' \
  --exclude='beam-usb*.img' --exclude='beam-usb*.img.xz*' --exclude='*.img' \
  --exclude='./tests/data/*.jpg' --exclude='./tests/data/*.png' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.git' \
  -cf - .) | (cd "$DEST" && tar -xf -)
touch "$DEST/$MARK"

# Personal details -> placeholders. The list lives in a private file next to this project
# (.public-copy-substitutions, lines "text|replacement"), so this script contains none of them.
SUBS="$SRC/.public-copy-substitutions"
PRIVATE=()
if [ -f "$SUBS" ]; then
  while IFS='|' read -r text repl; do
    [ -z "$text" ] || [ "${text:0:1}" = "#" ] && continue
    PRIVATE+=("$text")
    { grep -rlIF --exclude-dir=.git -- "$text" "$DEST" || true; } | while read -r f; do
      TEXT="$text" REPL="$repl" perl -pi -e 's/\Q$ENV{TEXT}\E/$ENV{REPL}/g' "$f"
    done
  done < <(tr -d '\r' < "$SUBS")
fi

echo "public copy: $DEST"
echo "files: $(find "$DEST" -type f -not -path '*/.git/*' | wc -l)"
echo "--- anything that still looks personal or secret (review before publishing):"
found=0
for text in "${PRIVATE[@]}"; do
  grep -rnIF --exclude-dir=.git -- "$text" "$DEST" && found=1
done
grep -rnI -E 'tskey-|BEGIN [A-Z ]*PRIVATE KEY|WIFI_PASSWORD=[^(\r]|TAILSCALE_AUTHKEY=[^(\r]|^BRAIN_KEY=[A-Za-z0-9]' \
  --exclude-dir=.git --exclude=make-public-copy.sh "$DEST" && found=1
[ "$found" = 0 ] && echo "(nothing found)"
exit 0
