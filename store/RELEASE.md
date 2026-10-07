# Shipping Mia: store + Mac package

Two artifacts, one extension id (`koanlnkpmdohonakopmcjibakmdgehnm`, fixed by the
`key` in extension/manifest.json). Never change or drop that key: the store id,
the Mac package and every installed helper depend on it. The private half lives
only in 1Password (item "Mia extension key"), nowhere in the repo.

## What a person does

1. Adds Mia from the Chrome Web Store (one click). A welcome tab opens with one
   button: Download the Mia installer.
2. Opens the .pkg, clicks Install, types their Mac password.
3. The panel turns green by itself. First question: Sign in to Claude.

Or the other way round: run the .pkg first, and Chrome offers the Mia extension
on its next start (click Enable). Both orders end in the same place.

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

# 3. Publish the package. The extension links to the fixed name on the latest release.
gh release create v<version> build/Mia-Browser-Use-<version>.pkg build/Mia-Browser-Use.pkg \
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

1. On a Mac without Mia: `sudo "/Library/Application Support/Ghost/uninstall.sh"`
   and remove the extension, or use a fresh user account.
2. Install the extension from the store (or the unlisted link), then follow
   the welcome tab. Count the clicks. The target is: Add to Chrome, Download,
   open, Install, password, Sign in to Claude. Nothing else.
