#!/bin/bash
# Build mia-browser-use_<version>_amd64.deb: the Linux installer for people who never
# open a terminal. Double-click it and the software installer does the rest.
#
# Run on a Debian/Ubuntu machine (needs dpkg-deb, fakeroot and uv):
#   packaging/build-deb.sh            -> build/mia-browser-use_<version>_amd64.deb
#                                        and build/mia-browser-use.deb (fixed name for the download link)
#
# Same shape as the Mac package: its own Python with Mia's packages, the app, the
# helper Chrome starts, the host manifests for Chrome/Chromium/Brave/Edge, and the
# registration that makes Chrome offer the store extension by itself.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BUILD="$REPO/build/deb"
ROOT="$BUILD/root"
PREFIX="/opt/mia-browser-use"
STAGE="$ROOT$PREFIX"
VERSION="$(python3 -c "import json;print(json.load(open('$REPO/extension/manifest.json'))['version'])")"
PY_VERSION="${GHOST_PYTHON_VERSION:-3.13}"
ARCH="$(dpkg --print-architecture)"
OUT="$REPO/build/mia-browser-use_${VERSION}_${ARCH}.deb"
export PATH="$HOME/.local/bin:$PATH"

rm -rf "$BUILD" && mkdir -p "$STAGE/app" "$ROOT/DEBIAN"

echo "→ Python $PY_VERSION (standalone)"
uv python install "$PY_VERSION" >/dev/null
PY_SRC="$(dirname "$(dirname "$(uv python find --system "$PY_VERSION")")")"
PY_SRC="$(cd "$PY_SRC" && pwd -P)"
cp -R "$PY_SRC" "$STAGE/python"
rm -f "$STAGE"/python/lib/python*/EXTERNALLY-MANAGED
PY="$STAGE/python/bin/python3"
uv pip install --quiet --python "$PY" -r "$REPO/requirements.txt"

echo "→ Mia $VERSION"
for f in "$REPO"/*.py "$REPO/mia-browser-use" "$REPO/ghost-cli"; do cp "$f" "$STAGE/app/"; done
rsync -a --exclude '.DS_Store' "$REPO/extension" "$STAGE/app/"
rsync -a --exclude '.DS_Store' "$REPO/mia_skills" "$STAGE/app/"
"$PY" -m compileall -q "$STAGE/app" "$STAGE/python/lib" 2>/dev/null || true

cat > "$STAGE/native-host" <<LAUNCH
#!/bin/sh
exec "$PREFIX/python/bin/python3" "$PREFIX/app/native_host.py" "\$@"
LAUNCH
chmod 755 "$STAGE/native-host"

EXT_ID="$(cd "$REPO" && "$PY" -c "from pathlib import Path; from native_host import extension_id; print(extension_id(Path('extension')))")"
[[ "$EXT_ID" =~ ^[a-p]{32}$ ]] || { echo "bad extension id: $EXT_ID" >&2; exit 1; }

# Host manifests: Chrome reads /etc/opt/chrome, Chromium /etc/chromium, Brave and Edge their own.
for dir in "etc/opt/chrome" "etc/chromium" "etc/brave" "etc/opt/edge"; do
  mkdir -p "$ROOT/$dir/native-messaging-hosts"
  cat > "$ROOT/$dir/native-messaging-hosts/com.ghost.bridge.json" <<JSON
{
  "name": "com.ghost.bridge",
  "description": "Mia helper pairing",
  "path": "$PREFIX/native-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
JSON
done
# Chrome offers the store extension by itself on its next start.
for dir in "usr/share/google-chrome/extensions" "usr/share/chromium/extensions"; do
  mkdir -p "$ROOT/$dir"
  echo '{"external_update_url": "https://clients2.google.com/service/update2/crx"}' > "$ROOT/$dir/$EXT_ID.json"
done

SIZE_KB="$(du -sk "$ROOT" --exclude=DEBIAN | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: mia-browser-use
Version: $VERSION
Section: web
Priority: optional
Architecture: $ARCH
Installed-Size: $SIZE_KB
Maintainer: Mia Labs <luis@mia-labs.com>
Homepage: https://github.com/luislozanogmia/mia-browser-use
Description: Mia, your browser assistant: the helper for the Chrome extension
 Mia and her bots read, compare and act on your tabs with you. This package is
 the part that runs on your computer: Mia's programs with their own Python, and
 the small program Chrome starts when the Mia extension asks for it. Mia answers
 with your own Claude account (Anthropic); if Claude Code is missing, Mia's
 panel offers to install it. Nothing runs until Chrome asks for it.
CONTROL
cat > "$ROOT/DEBIAN/postinst" <<'POST'
#!/bin/sh
# An older helper would keep running old code; Chrome starts the new one.
pkill -f "/opt/mia-browser-use/app/ghost_cli.py" 2>/dev/null || true
exit 0
POST
cat > "$ROOT/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
pkill -f "/opt/mia-browser-use/app/ghost_cli.py" 2>/dev/null || true
exit 0
PRERM
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm"
find "$ROOT" -name '._*' -delete
chmod -R go-w "$ROOT"  # system files aren't group-writable

echo "→ Package"
fakeroot dpkg-deb --build --root-owner-group -Zxz "$ROOT" "$OUT" >/dev/null
cp "$OUT" "$REPO/build/mia-browser-use.deb"
echo "✓ $OUT ($(du -h "$OUT" | cut -f1)), also build/mia-browser-use.deb"
echo "  extension id: $EXT_ID"
