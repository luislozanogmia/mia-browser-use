#!/bin/bash
# Build the zip that goes to the Chrome Web Store Developer Dashboard.
#
#   packaging/build-store-zip.sh      -> build/mia-extension-<version>.zip
#
# The zip is the extension folder as is, minus files the store doesn't need. The
# manifest's "key" keeps the store id equal to the unpacked id, so the Mac package
# (build-pkg.sh) and the store always refer to the same extension.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
VERSION="$(python3 -c "import json;print(json.load(open('$REPO/extension/manifest.json'))['version'])")"
python3 -c "import json,sys;m=json.load(open('$REPO/extension/manifest.json'));sys.exit(0 if m.get('key') else 'manifest.json has no key: the store id would change')"
OUT="$REPO/build/mia-extension-$VERSION.zip"
mkdir -p "$REPO/build" && rm -f "$OUT"
(cd "$REPO/extension" && zip -qr "$OUT" . -x '.DS_Store' '*/.DS_Store' 'install-guide.html' '*.map')
echo "✓ $OUT ($(du -h "$OUT" | cut -f1))"
echo "  upload it at https://chrome.google.com/webstore/devconsole (see store/RELEASE.md)"
