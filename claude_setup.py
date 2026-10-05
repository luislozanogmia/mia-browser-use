"""Get Claude Code ready for Mia without anyone opening Terminal.

Mia's agents run the `claude` command with the person's own
Claude account. This checks whether it's installed and signed in, installs a
verified pinned Anthropic binary when it's missing, and starts the browser
sign-in. The panel shows a button for it (see ChatHub, action "claude_setup").
"""

from __future__ import annotations

import json
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

CLAUDE_VERSION = "2.1.281"
CLAUDE_RELEASE = f"https://downloads.claude.ai/claude-code-releases/{CLAUDE_VERSION}"
CLAUDE_MAC_SHA256 = {
    "arm64": "a922981f6f3b55a251ef9f9dbaa0621a5f99cbcb5ca67f8a797476ccfc83f626",
    "x64": "a9355cbb0d291ce948efcf61a6ef397401672f64fa5e5e67bca092fed6cd9088",
}
MAX_CLAUDE_BYTES = 300 * 1024 * 1024
STATUS_TTL = 30.0
LOGIN_WAIT_SECONDS = 3.0

_cache: dict = {"at": 0.0, "value": None}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def binary() -> str | None:
    managed = Path.home() / ".ghost" / "bin" / "claude"
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine == "x86_64" else None
    if managed.is_file() and not managed.is_symlink() and os.access(managed, os.X_OK) and arch:
        try:
            if _sha256_file(managed) == CLAUDE_MAC_SHA256[arch]:
                return str(managed)
        except OSError:
            pass
    return None


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
                                 timeout=15, stdin=subprocess.DEVNULL,
                                 env={**os.environ, "DISABLE_AUTOUPDATER": "1"}).stdout
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
    """Install one pinned official macOS binary after checking its committed SHA-256."""
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine == "x86_64" else None
    if platform.system() != "Darwin" or arch is None:
        return False
    expected = CLAUDE_MAC_SHA256[arch]
    target = Path.home() / ".ghost" / "bin" / "claude"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_file() and not target.is_symlink() and _sha256_file(target) == expected:
        _cache["value"] = None
        return True
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temp = Path(output.name)
            digest = hashlib.sha256()
            size = 0
            with urlopen(f"{CLAUDE_RELEASE}/darwin-{arch}/claude", timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_CLAUDE_BYTES:
                        raise ValueError("Claude download exceeded size limit")
                    output.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Claude download did not match pinned SHA-256")
        temp.chmod(0o700)
        os.replace(temp, target)
    except (OSError, ValueError) as exc:
        with _log() as log:
            log.write(f"Claude Code {CLAUDE_VERSION} installation failed: {exc}\n".encode())
        return False
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    _cache["value"] = None
    return target.is_file()


def _in_terminal(claude: str) -> None:
    """Fallback: a Terminal window that runs only the sign-in, for a CLI that wants one."""
    script = Path(tempfile.mkdtemp(prefix="ghost-")) / "Sign in to Claude.command"
    script.write_text(
        "#!/bin/sh\nclear\necho 'Sign in to Claude in the browser window that opens.'\necho\n"
        f"DISABLE_AUTOUPDATER=1 '{claude}' auth login --claudeai && echo && echo \"You're signed in. You can close this window.\"\n")
    script.chmod(0o700)
    subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=False)


def login() -> None:
    """Start the browser sign-in for the person's Claude account."""
    claude = binary()
    if not claude:
        raise RuntimeError("Claude Code isn't installed")
    log = _log()
    proc = subprocess.Popen([claude, "auth", "login", "--claudeai"], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**os.environ, "DISABLE_AUTOUPDATER": "1"})
    log.close()
    try:
        code = proc.wait(LOGIN_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return  # waiting for the browser, as it should
    _cache["value"] = None
    if code != 0:
        _in_terminal(claude)


if __name__ == "__main__":
    import sys
    sys.exit(0 if len(sys.argv) == 2 and sys.argv[1] == "install" and install() else 1)
