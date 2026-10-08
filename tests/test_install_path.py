"""One install for people who never open Terminal.

A person adds Mia from the Chrome Web Store, runs one installer, and the panel
connects by itself. Everything here keeps that path intact: one fixed extension
id shared by the store, the package and the unpacked copy; a setup card with the
download; a welcome tab on first install; a package that registers the store
extension; and the store listing that says all of this plainly.
"""

import base64
import hashlib
import json
import re
import unittest
from pathlib import Path

import native_host

REPO_ROOT = Path(__file__).resolve().parent.parent
EXT = REPO_ROOT / "extension"
PACKAGING = REPO_ROOT / "packaging"
STORE = REPO_ROOT / "store"
EXTENSION_ID = "koanlnkpmdohonakopmcjibakmdgehnm"
RELEASES = "https://github.com/luislozanogmia/mia-browser-use/releases/latest/download/"
# One installer per OS, named the way each build script names its fixed-name output.
INSTALLERS = {"mac": "Mia-Browser-Use.pkg", "win": "Mia-Browser-Use-Setup.exe", "linux": "mia-browser-use.deb"}


class FixedIdTests(unittest.TestCase):
    def test_manifest_key_fixes_the_extension_id_everywhere(self):
        manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        key = base64.b64decode(manifest["key"])
        self.assertEqual(native_host.extension_id(EXT), EXTENSION_ID)
        digest = hashlib.sha256(key).hexdigest()[:32]
        self.assertEqual("".join(chr(ord("a") + int(c, 16)) for c in digest), EXTENSION_ID)

    def test_a_folder_without_a_key_still_gets_chromes_path_id(self):
        self.assertNotEqual(native_host.extension_id(Path("/tmp")), EXTENSION_ID)
        self.assertEqual(len(native_host.extension_id(Path("/tmp"))), 32)

    def test_the_private_key_is_not_in_the_repo(self):
        marker = "PRIVATE " + "KEY-----"  # assembled so this file doesn't match itself
        skip = {".git", "build", "__pycache__", "node_modules"}
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or skip & set(path.parts) or path.stat().st_size > 20_000:
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            self.assertNotIn(marker, text, f"private key material in the repo: {path}")

    def test_release_notes_pin_the_id(self):
        self.assertIn(EXTENSION_ID, (STORE / "RELEASE.md").read_text(encoding="utf-8"))


class ExtensionSetupTests(unittest.TestCase):
    def test_background_tells_the_panel_what_the_helper_needs(self):
        source = (EXT / "background.js").read_text(encoding="utf-8")
        self.assertIn(f'const RELEASES = "{RELEASES}"', source)
        for os_name, filename in INSTALLERS.items():
            self.assertIn(f'{os_name}: "{filename}"', source)
        self.assertIn("installer_url: installerUrl()", source)
        self.assertIn("chrome.runtime.getPlatformInfo()", source)
        # Each name is really what its build script produces.
        self.assertIn(INSTALLERS["mac"], (PACKAGING / "build-pkg.sh").read_text(encoding="utf-8"))
        self.assertIn(INSTALLERS["linux"], (PACKAGING / "build-deb.sh").read_text(encoding="utf-8"))
        self.assertIn(INSTALLERS["win"], (PACKAGING / "windows" / "build.ps1").read_text(encoding="utf-8"))
        for state in ('"missing"', '"outdated"', '"failed"', '"ok"'):
            self.assertIn(state, source)
        self.assertIn('details?.reason === "install"', source)
        self.assertIn('chrome.runtime.getURL("welcome.html")', source)
        # While the helper is missing the extension keeps asking, so the panel turns green by itself.
        self.assertIn("reconnectDelay = SETUP_RETRY_DELAY", source)
        self.assertLessEqual(int(re.search(r"SETUP_RETRY_DELAY = (\d+)", source).group(1)), 5000)

    def test_side_panel_shows_one_download_and_hides_the_manual_fields(self):
        html = (EXT / "sidepanel.html").read_text(encoding="utf-8")
        js = (EXT / "sidepanel.js").read_text(encoding="utf-8")
        self.assertIn('id="downloadBtn"', html)
        self.assertIn("Chrome extensions can't install programs", html)
        self.assertIn("What the installer adds", html)
        self.assertIn('<details class="setup-advanced"', html)
        self.assertIn('$("downloadBtn").href = info.installer_url', js)
        self.assertIn('$("setupCard").hidden = paired', js)
        self.assertNotIn("Run the Mia Browser installer once", js)

    def test_welcome_page_polls_and_never_starts_anything(self):
        html = (EXT / "welcome.html").read_text(encoding="utf-8")
        js = (EXT / "welcome.js").read_text(encoding="utf-8")
        self.assertIn('id="download"', html)
        self.assertIn("uninstall.sh", html)
        self.assertIn('{ type: "get-status" }', js)
        self.assertNotIn("sendNativeMessage", js)
        self.assertNotIn('type: "connect"', js)
        manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("web_accessible_resources", manifest)  # welcome.html is an extension page, not exposed to sites


class PackageTests(unittest.TestCase):
    def test_package_registers_the_store_extension_and_the_fixed_name(self):
        script = (PACKAGING / "build-pkg.sh").read_text(encoding="utf-8")
        self.assertNotIn("GHOST_WEB_STORE_ID", script)
        self.assertIn('External Extensions', script)
        self.assertIn('"$ext/$EXT_ID.json"', script)
        self.assertIn('cp "$OUT" "$REPO/build/Mia-Browser-Use.pkg"', script)
        self.assertIn("^[a-p]{32}$", script)

    def test_postinstall_opens_nothing(self):
        post = (PACKAGING / "scripts" / "postinstall").read_text(encoding="utf-8")
        self.assertNotIn("install-guide", post)
        self.assertNotIn("pbcopy", post)

    def test_uninstaller_removes_the_registration_too(self):
        self.assertIn("External Extensions", (PACKAGING / "uninstall.sh").read_text(encoding="utf-8"))

    def test_store_zip_refuses_a_manifest_without_a_key(self):
        script = (PACKAGING / "build-store-zip.sh").read_text(encoding="utf-8")
        self.assertIn("m.get('key')", script)
        self.assertIn("install-guide.html", script)


class ListingTests(unittest.TestCase):
    def test_listing_is_transparent_about_the_helper_and_claude(self):
        text = (STORE / "LISTING.md").read_text(encoding="utf-8")
        for needed in ("nativeMessaging", "<all_urls>", "Remote code", "Claude Code", "Mia installer",
                       "Apple silicon", "Privacy policy URL", "Single purpose"):
            self.assertIn(needed, text, needed)
        self.assertLessEqual(len(re.search(r"\*\*Summary.*?\n(.+)\n", text).group(1)), 132)
        manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
        for permission in manifest["permissions"]:
            self.assertIn(f"`{permission}`", text, permission)

    def test_privacy_policy_matches_the_manifest(self):
        policy = (STORE / "privacy.html").read_text(encoding="utf-8")
        for needed in ("127.0.0.1", "Anthropic", "no Mia server", "uninstall.sh", "luis@mia-labs.com"):
            self.assertIn(needed, policy, needed)


if __name__ == "__main__":
    unittest.main()
