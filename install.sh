#!/usr/bin/env bash
# install.sh — build the photo sorter's helper and run the sorter under launchd.
#
# Do the setup in README.md first: aura-login.json and config.json in the sorter's folder,
# plus gallery.npz if any frame lists people. This checks them (read-only) before loading.
#
# A LaunchAgent, not a LaunchDaemon: Photos belongs to the login session. launchd can't read
# ~/Documents or ~/.config, so everything lives in Application Support, and rerunning this is
# how a change to sorter.py reaches the service. macOS asks once for Photos access, and asks
# again after a rebuild because the helper's signature changes.
#
# The first install marks everything already in the album as handled, so nothing in it gets
# uploaded. Add photos to the album after installing.
#
# Usage: install.sh [--no-load]
#   --no-load  build, copy and check, but don't start the service
# Stop it:     launchctl bootout gui/$(id -u)/local.aura-photo-sorter
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="${AURA_SORTER_HOME:-$HOME/Library/Application Support/aura-photo-sorter}"
LABEL=local.aura-photo-sorter
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/aura-photo-sorter"
UV="$(command -v uv)" || { echo "needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }

[ -f "$SUPPORT/aura-login.json" ] || { echo "no aura-login.json; run: uv run login.py" >&2; exit 1; }
[ -f "$SUPPORT/config.json" ] || { echo "no config.json in $SUPPORT; see config.example.json" >&2; exit 1; }

mkdir -p "$LOGS" "$SUPPORT"
chmod 700 "$SUPPORT"
swiftc -O "$HERE/aura-photo-album.swift" -o "$SUPPORT/aura-photo-album.new" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$HERE/Info.plist"
codesign -f -s - -i "$LABEL.album" "$SUPPORT/aura-photo-album.new"
mv "$SUPPORT/aura-photo-album.new" "$SUPPORT/aura-photo-album"
install -m 600 "$HERE/sorter.py" "$SUPPORT/sorter.py"

# Also fills uv's cache now, so the service's first photo doesn't wait on a download.
"$UV" run --script "$SUPPORT/sorter.py" check
# Asks for Photos access the first time.
[ -f "$SUPPORT/state.json" ] || "$UV" run --script "$SUPPORT/sorter.py" baseline
[ "${1:-}" = "--no-load" ] && exit 0

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$UV</string><string>run</string><string>--script</string><string>$SUPPORT/sorter.py</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OMP_NUM_THREADS</key><string>4</string>
    <key>PATH</key><string>$(dirname "$UV"):/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>AURA_SORTER_HOME</key><string>$SUPPORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOGS/sorter.log</string>
  <key>StandardErrorPath</key><string>$LOGS/sorter.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "loaded $LABEL; log: $LOGS/sorter.log"
