import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_setup


def fake_claude(folder: Path, status: dict | None, login_exit: int = 0, login_sleep: float = 0) -> str:
    if sys.platform == "win32":
        path = folder / "claude.cmd"
        answer = json.dumps(status) if status is not None else "oops"
        path.write_text(
            "@echo off\r\n"
            f"if \"%1 %2\"==\"auth status\" (echo {answer}& exit /b 0)\r\n"
            f"if \"%1 %2\"==\"auth login\" (ping -n {int(login_sleep) + 1} 127.0.0.1 >nul & exit /b {login_exit})\r\n"
            "exit /b 2\r\n")
        return str(path)
    path = folder / "claude"
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1 $2\" = \"auth status\" ]; then echo '{json.dumps(status) if status is not None else 'oops'}'; exit 0; fi\n"
        f"if [ \"$1 $2\" = \"auth login\" ]; then sleep {login_sleep}; exit {login_exit}; fi\n"
        "exit 2\n")
    path.chmod(0o755)
    return str(path)


class ClaudeSetupTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._dir.name)
        claude_setup._cache.update(at=0.0, value=None)
        self.home = mock.patch.object(claude_setup.Path, "home", return_value=self.tmp)
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self._dir.cleanup()

    def test_not_installed(self):
        with mock.patch.object(claude_setup.shutil, "which", return_value=None):
            self.assertEqual(claude_setup.status(True), {"installed": False, "signed_in": False})

    def test_signed_in_and_signed_out(self):
        for logged, expected in ((True, True), (False, False)):
            binary = fake_claude(self.tmp, {"loggedIn": logged})
            with mock.patch.object(claude_setup.shutil, "which", return_value=binary):
                self.assertEqual(claude_setup.status(True), {"installed": True, "signed_in": expected})

    def test_garbled_status_means_signed_out(self):
        binary = fake_claude(self.tmp, None)
        with mock.patch.object(claude_setup.shutil, "which", return_value=binary):
            self.assertFalse(claude_setup.status(True)["signed_in"])

    def test_status_is_cached(self):
        binary = fake_claude(self.tmp, {"loggedIn": True})
        with mock.patch.object(claude_setup.shutil, "which", return_value=binary):
            claude_setup.status(True)
        with mock.patch.object(claude_setup.shutil, "which", return_value=None):
            self.assertTrue(claude_setup.status()["installed"])

    def test_login_waiting_for_browser_opens_no_terminal(self):
        binary = fake_claude(self.tmp, {"loggedIn": False}, login_sleep=5)
        with mock.patch.object(claude_setup.shutil, "which", return_value=binary), \
             mock.patch.object(claude_setup, "LOGIN_WAIT_SECONDS", 0.5), \
             mock.patch.object(claude_setup.subprocess, "run") as run:
            self.assertTrue(claude_setup.login())
        run.assert_not_called()

    def test_failed_login_reports_it_and_never_opens_terminal(self):
        binary = fake_claude(self.tmp, {"loggedIn": False}, login_exit=1)
        with mock.patch.object(claude_setup.shutil, "which", return_value=binary), \
             mock.patch.object(claude_setup.subprocess, "run") as run:
            self.assertFalse(claude_setup.login())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
