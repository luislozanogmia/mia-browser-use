"""Get Claude Code ready for Mia without anyone opening Terminal.

Mia's agents run the `claude` command with the person's own
Claude account. This checks whether it's installed and signed in, installs it
with Anthropic's official script when it's missing, and starts the browser
sign-in. The panel shows a button for it (see ChatHub, action "claude_setup").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

INSTALL_SCRIPT = "https://claude.ai/install.sh"
STATUS_TTL = 30.0
LOGIN_WAIT_SECONDS = 3.0

_cache: dict = {"at": 0.0, "value": None}


def binary() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    # Where the official installer puts it, in case PATH doesn't have it yet.
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def status(fresh: bool = False) -> dict:
    """{"installed": bool, "signed_in": bool}, cached briefly: the panel asks often."""
    now = time.monotonic()
    if not fresh and _cache["value"] is not None and now - _cache["at"] < STATUS_TTL:
        return _cache["value"]
    claude = binary()
    value = {"installed": bool(claude), "signed_in": False}
    if claude:
        try:
            out = subprocess.run([claude, "auth", "status", "--json"], capture_output=True, text=True,
                                 timeout=15, stdin=subprocess.DEVNULL).stdout
            value["signed_in"] = bool(json.loads(out).get("loggedIn"))
        except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
            pass
    _cache.update(at=now, value=value)
    return value


def _log():
    path = Path.home() / ".ghost" / "claude-setup.log"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    return open(path, "ab")


def install() -> bool:
    """Run the official installer and wait for it. True when `claude` exists afterwards."""
    with _log() as log:
        try:
            subprocess.run(["/bin/bash", "-c", f"set -o pipefail; /usr/bin/curl -fsSL {INSTALL_SCRIPT} | /bin/bash"],
                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        except (OSError, subprocess.SubprocessError):
            return False
    _cache["value"] = None
    return binary() is not None


def _in_terminal(claude: str) -> None:
    """Fallback: a Terminal window that runs only the sign-in, for a CLI that wants one."""
    script = Path(tempfile.mkdtemp(prefix="ghost-")) / "Sign in to Claude.command"
    script.write_text(
        "#!/bin/sh\nclear\necho 'Sign in to Claude in the browser window that opens.'\necho\n"
        f"'{claude}' auth login --claudeai && echo && echo \"You're signed in. You can close this window.\"\n")
    script.chmod(0o700)
    subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=False)


def login() -> None:
    """Start the browser sign-in for the person's Claude account."""
    claude = binary()
    if not claude:
        raise RuntimeError("Claude Code isn't installed")
    log = _log()
    proc = subprocess.Popen([claude, "auth", "login", "--claudeai"], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    try:
        code = proc.wait(LOGIN_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return  # waiting for the browser, as it should
    _cache["value"] = None
    if code != 0:
        _in_terminal(claude)
