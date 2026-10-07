#!/bin/bash
# Build Mia-Browser-Use.pkg: a double-click Mac installer for people who never open Terminal.
#
# It bundles its own Python with Ghost's packages, the extension, and the helper Chrome
# uses to start Ghost. Nothing else needs to be installed first.
#
#   packaging/build-pkg.sh
#
# The extension's id is fixed by the key in extension/manifest.json, so the same id
# covers the unpacked copy, the Chrome Web Store version and this package. The
# package registers that id with Chrome ("External Extensions"), so Chrome offers the
# store version by itself on its next start; people who added Mia from the store
# first just see the panel turn green.
#
# Optional environment:
#   GHOST_SIGN_APP       "Developer ID Application: ..." identity, signs the bundled programs.
#   GHOST_SIGN_INSTALLER "Developer ID Installer: ..." identity, signs the .pkg.
#   GHOST_NOTARY_PROFILE notarytool keychain profile; notarizes and staples the .pkg.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BUILD="$REPO/build/pkg"
ROOT="$BUILD/root"
GHOST="/Library/Application Support/Ghost"
STAGE="$ROOT$GHOST"
VERSION="$(python3 -c "import json;print(json.load(open('$REPO/extension/manifest.json'))['version'])")"
PY_VERSION="${GHOST_PYTHON_VERSION:-3.13}"
OUT="$REPO/build/Mia-Browser-Use-$VERSION.pkg"

rm -rf "$BUILD" && mkdir -p "$STAGE/app"

echo "→ Python $PY_VERSION (standalone, arm64)"
uv python install "$PY_VERSION" >/dev/null
PY_SRC="$(dirname "$(dirname "$(uv python find "$PY_VERSION")")")"
PY_SRC="$(cd "$PY_SRC" && pwd -P)"
cp -R "$PY_SRC" "$STAGE/python"
rm -f "$STAGE"/python/lib/python*/EXTERNALLY-MANAGED
PY="$STAGE/python/bin/python3"
uv pip install --quiet --python "$PY" -r "$REPO/requirements.txt"

echo "→ Ghost $VERSION"
for f in "$REPO"/*.py "$REPO/mia-browser-use"; do cp "$f" "$STAGE/app/"; done
cp "$REPO/ghost-cli" "$STAGE/app/"  # old name, kept for existing scripts
rsync -a --exclude '.DS_Store' "$REPO/extension" "$STAGE/app/"
rsync -a --exclude ".DS_Store" "$REPO/mia_skills" "$STAGE/app/"
"$PY" -m compileall -q "$STAGE/app" "$STAGE/python/lib" 2>/dev/null || true

# The helper Chrome starts (see native_host.py). Chrome gives it a bare environment.
cat > "$STAGE/native-host" <<LAUNCH
#!/bin/sh
exec "$GHOST/python/bin/python3" "$GHOST/app/native_host.py" "\$@"
LAUNCH
chmod 755 "$STAGE/native-host"
cp "$HERE/uninstall.sh" "$STAGE/uninstall.sh" && chmod 755 "$STAGE/uninstall.sh"

# Which extension may start the helper: the one id the manifest key fixes.
EXT_ID="$(cd "$REPO" && "$PY" -c "from pathlib import Path; from native_host import extension_id; print(extension_id(Path('extension')))")"
[[ "$EXT_ID" =~ ^[a-p]{32}$ ]] || { echo "bad extension id: $EXT_ID" >&2; exit 1; }
ORIGINS="\"chrome-extension://$EXT_ID/\""
for dir in "Google/Chrome" "Chromium" "BraveSoftware/Brave-Browser" "Microsoft Edge"; do
  case "$dir" in Google/Chrome) target="$ROOT/Library/Google/Chrome/NativeMessagingHosts" ;;
                 *) target="$ROOT/Library/Application Support/$dir/NativeMessagingHosts" ;; esac
  mkdir -p "$target"
  cat > "$target/com.ghost.bridge.json" <<JSON
{
  "name": "com.ghost.bridge",
  "description": "Ghost bridge pairing",
  "path": "$GHOST/native-host",
  "type": "stdio",
  "allowed_origins": [$ORIGINS]
}
JSON
done

# Chrome offers the store version of the extension by itself on its next start.
ext="$ROOT/Library/Application Support/Google/Chrome/External Extensions"
mkdir -p "$ext"
echo '{"external_update_url": "https://clients2.google.com/service/update2/crx"}' > "$ext/$EXT_ID.json"

if [ -n "${GHOST_SIGN_APP:-}" ]; then
  echo "→ Signing bundled programs"
  find "$STAGE" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0 |
    while IFS= read -r -d '' f; do
      if file -b "$f" | grep -q Mach-O; then
        codesign --force --timestamp --options runtime --sign "$GHOST_SIGN_APP" "$f" 2>&1 | grep -v 'replacing existing signature' || true
      fi
    done
fi

echo "→ Package"
pkgbuild --quiet --root "$ROOT" --identifier com.ghost.multiplayer --version "$VERSION" \
  --scripts "$HERE/scripts" --install-location / "$BUILD/ghost.pkg"
sed "s/@VERSION@/$VERSION/g" "$HERE/distribution.xml" > "$BUILD/distribution.xml"
SIGN=()
[ -n "${GHOST_SIGN_INSTALLER:-}" ] && SIGN=(--sign "$GHOST_SIGN_INSTALLER")
productbuild --quiet --distribution "$BUILD/distribution.xml" --resources "$HERE/resources" \
  --package-path "$BUILD" ${SIGN[@]+"${SIGN[@]}"} "$OUT"

if [ -n "${GHOST_NOTARY_PROFILE:-}" ]; then
  echo "→ Notarizing"
  xcrun notarytool submit "$OUT" --keychain-profile "$GHOST_NOTARY_PROFILE" --wait
  xcrun stapler staple "$OUT"
fi
# The extension links to a fixed name on GitHub Releases (.../releases/latest/download/Mia-Browser-Use.pkg).
cp "$OUT" "$REPO/build/Mia-Browser-Use.pkg"
echo "✓ $OUT ($(du -h "$OUT" | cut -f1)), also build/Mia-Browser-Use.pkg"
echo "  extension id: $EXT_ID"
