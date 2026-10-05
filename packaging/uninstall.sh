#!/bin/bash
# Remove Ghost. Run with: sudo "/Library/Application Support/Ghost/uninstall.sh"
set -e
pkill -f "/Library/Application Support/Ghost/app/ghost_cli.py" 2>/dev/null || true
rm -f "/Library/Google/Chrome/NativeMessagingHosts/com.ghost.bridge.json"
for dir in Chromium "BraveSoftware/Brave-Browser" "Microsoft Edge"; do
  rm -f "/Library/Application Support/$dir/NativeMessagingHosts/com.ghost.bridge.json"
done
rm -rf "/Library/Application Support/Ghost"
pkgutil --forget com.ghost.multiplayer >/dev/null 2>&1 || true
echo "Ghost removed. Remove the extension from chrome://extensions."
