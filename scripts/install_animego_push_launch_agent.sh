#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LABEL=com.xxsrez.animego-sync
DOMAIN=gui/$(id -u)
TARGET=${HOME}/Library/LaunchAgents/${LABEL}.plist

mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs/Anime"
install -m 600 "$ROOT/scripts/${LABEL}.plist" "$TARGET"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"

echo "Installed $LABEL; log: ${HOME}/Library/Logs/Anime/animego-push-worker.log"
