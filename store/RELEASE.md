# Shipping Mia: store + one installer per OS

Four artifacts (store zip, Mac .pkg, Windows .exe, Linux .deb), one extension id (`koanlnkpmdohonakopmcjibakmdgehnm`, fixed by the
`key` in extension/manifest.json). Never change or drop that key: the store id,
the installers and every installed helper depend on it. The private half lives
only in 1Password (item "Mia extension key"), nowhere in the repo.

## What a person does

1. Adds Mia from the Chrome Web Store (one click). A welcome tab opens with one
   button: Download the Mia installer. The extension picks the installer for the
   person's OS (`chrome.runtime.getPlatformInfo`).
2. Mac: opens the .pkg, clicks Install, types their Mac password.
   Windows: opens Mia-Browser-Use-Setup.exe, clicks Install (per-user, no
   administrator prompt). Linux: double-clicks mia-browser-use.deb, the software
   installer asks for their password.
3. The panel turns green by itself. First question: Sign in to Claude.

Or the other way round: run the installer first, and Chrome offers the Mia
extension on its next start (click Enable). Both orders end in the same place.

## One-time setup on the release Mac (Luis)

- [ ] Apple Developer: create a **Developer ID Installer** certificate
      (developer.apple.com → Certificates → "+", use
      ~/Documents/CertificateSigningRequest.certSigningRequest) and install it in
      the login keychain. The **Developer ID Application** certificate is already
      there ("Luis Lozano (9F277BG847)").
- [ ] Notarization credentials: `xcrun notarytool store-credentials mia`
      (Apple ID luis@mia-labs.com, team 9F277BG847, an app-specific password
      from appleid.apple.com). Without notarization, other Macs refuse the .pkg.
- [ ] Privacy policy online: enable GitHub Pages on luislozanogmia/mia-browser-use
      (Settings → Pages → Deploy from branch, folder /store) or copy
      store/privacy.html to www.mia-labs.com. The URL is in store/LISTING.md.

## Every release

```bash
# 1. Version: extension/manifest.json, __init__.py and tests/test_version.py agree.
python3 -m unittest discover -s tests -q

# 2. The Mac package, signed and notarized.
GHOST_SIGN_APP="Developer ID Application: Luis Lozano (9F277BG847)" \
GHOST_SIGN_INSTALLER="Developer ID Installer: Luis Lozano (9F277BG847)" \
GHOST_NOTARY_PROFILE=mia packaging/build-pkg.sh
# -> build/Mia-Browser-Use-<version>.pkg and build/Mia-Browser-Use.pkg

# 2b. The Windows installer, on a Windows machine with uv and Inno Setup 6
#     (winget install astral-sh.uv JRSoftware.InnoSetup). Gawain works; its C: is
#     small, so build from a checkout on D: with UV_CACHE_DIR and TEMP on D:.
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
# -> build\Mia-Browser-Use-Setup-<version>.exe and build\Mia-Browser-Use-Setup.exe
#    Unsigned for now: SmartScreen shows "Windows protected your PC" until the exe
#    is signed with a code-signing certificate (More info → Run anyway).

# 2c. The Linux package, on a Debian/Ubuntu machine with uv, dpkg-deb and fakeroot
#     (worker1 works).
packaging/build-deb.sh
# -> build/mia-browser-use_<version>_amd64.deb and build/mia-browser-use.deb

# 3. Publish the packages. The extension links to the fixed names on the latest release.
gh release create v<version> \
  build/Mia-Browser-Use-<version>.pkg build/Mia-Browser-Use.pkg \
  build/Mia-Browser-Use-Setup-<version>.exe build/Mia-Browser-Use-Setup.exe \
  build/mia-browser-use_<version>_amd64.deb build/mia-browser-use.deb \
  --title "Mia <version>" --notes "..."

# 4. The store zip.
packaging/build-store-zip.sh
# -> build/mia-extension-<version>.zip
```

Then on https://chrome.google.com/webstore/devconsole: New item (first time) or
Package → Upload new package, paste from store/LISTING.md (first time), Submit
for review. Reviews take hours to a few days. Keep the store version and the
package in step: an extension that is newer than the installed helper shows
"Update the Mia helper" in its panel, with the download link.

## Review notes for Google (paste in "Notes for reviewer")

Mia needs a helper on the reviewer's Mac to do anything: the extension talks to
it over native messaging. Installer: <release URL>/Mia-Browser-Use.pkg
(Apple-notarized). After installing, open the Mia side panel, click Sign in to
Claude (a Claude account from Anthropic is needed; a free one works), then drag
over any part of a web page and ask a question. Without the helper the panel
shows the setup card and the download link; that is the intended behavior, not
a broken extension.

## Testing a new build the way a new person sees it

1. On a machine without Mia: Mac `sudo "/Library/Application Support/Ghost/uninstall.sh"`,
   Windows Settings → Apps → Mia → Uninstall, Linux `sudo apt remove mia-browser-use`;
   and remove the extension, or use a fresh user account.
2. Install the extension from the store (or the unlisted link), then follow
   the welcome tab. Count the clicks. The target is: Add to Chrome, Download,
   open, Install, password, Sign in to Claude. Nothing else.
