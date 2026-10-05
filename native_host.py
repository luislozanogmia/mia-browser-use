"""Pair the Chrome extension with the bridge, and start the bridge if it's down.

Chrome starts this program when the Ghost extension asks for it
(chrome.runtime.sendNativeMessage). Chrome only lets extensions listed in the
host manifest's allowed_origins start it, and install_native_host() lists just
this checkout's extension. The token goes over the program's stdin/stdout,
never through a URL, a web page or a log.

It also makes sure `mia-browser-use up` is running (see ghost_up.py), which starts
and watches the bridge and the local room relay.
ghost_eval is never turned on this way.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from bridge_auth import load_bridge_token

HOST_NAME = "com.ghost.bridge"
DEFAULT_PORT = 9377
MAX_MESSAGE_BYTES = 4096
REPO_ROOT = Path(__file__).resolve().parent
GHOST_DIR = Path.home() / ".ghost"
CONFIG_PATH = GHOST_DIR / "bridge.json"
LOG_PATH = GHOST_DIR / "bridge.log"
LOCK_PATH = GHOST_DIR / "autostart.lock"
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
START_WAIT_SECONDS = 6.0


def read_message(stream) -> dict | None:
    header = stream.read(4)
    if len(header) != 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        return None
    try:
        message = json.loads(stream.read(length))
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def write_message(stream, message: dict) -> None:
    data = json.dumps(message).encode()
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


def clean_bridge_config(raw) -> dict:
    """Keep only well-formed serve settings; anything else is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    config: dict = {}
    port = raw.get("port")
    config["port"] = port if isinstance(port, int) and 1024 <= port <= 65535 else DEFAULT_PORT
    room, me = raw.get("room"), raw.get("me")
    if isinstance(room, str) and ID_RE.match(room) and isinstance(me, str) and ID_RE.match(me):
        config["room"], config["me"] = room, me
        url = raw.get("room_url")
        if isinstance(url, str) and len(url) <= 300 and urlparse(url).scheme in ("ws", "wss") and urlparse(url).hostname:
            config["room_url"] = url
        name = raw.get("name")
        if isinstance(name, str) and 0 < len(name) <= 64 and name.isprintable():
            config["name"] = name
        color = raw.get("color")
        if isinstance(color, str) and COLOR_RE.match(color):
            config["color"] = color
    return config


def save_bridge_config(port: int, room: str | None = None, room_url: str | None = None,
                       me: str | None = None, name: str | None = None, color: str | None = None) -> None:
    """Remember how the bridge was started, so the extension can start it the same way."""
    config = clean_bridge_config({"port": port, "room": room, "room_url": room_url, "me": me, "name": name, "color": color})
    GHOST_DIR.mkdir(mode=0o700, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)


def load_bridge_config() -> dict:
    try:
        return clean_bridge_config(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        return clean_bridge_config({})


def is_listening(host: str, port: int) -> bool:
    """True when something holds the port. Binds instead of connecting, so the
    bridge never sees (or logs) a stray connection."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
        return False


def wait_listening(host: str, port: int, seconds: float = START_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_listening(host, port):
            return True
        time.sleep(0.2)
    return False


def child_path() -> str:
    """Chrome starts hosts with PATH=/usr/bin:/bin; add where the claude CLI usually lives."""
    home = Path.home()
    extra = [home / ".local" / "bin", home / ".claude" / "local", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    parts = [str(d) for d in extra if d.is_dir()] + os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
    return os.pathsep.join(dict.fromkeys(p for p in parts if p))


def spawn(args: list[str], detach: bool = True) -> subprocess.Popen:
    """Start mia-browser-use. Detached, it outlives this short-lived host."""
    GHOST_DIR.mkdir(mode=0o700, exist_ok=True)
    with open(LOG_PATH, "ab") as log:
        os.chmod(LOG_PATH, 0o600)
        return subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "ghost_cli.py"), *args],
            # The app folder may be read-only (installed copy), so run from ~/.ghost.
            cwd=str(GHOST_DIR), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=detach, close_fds=True,
            env={**os.environ, "PATH": child_path(), "PYTHONUNBUFFERED": "1"},
        )


def ensure_up(config: dict) -> str:
    """Make sure `mia-browser-use up` (ghost_up.py) runs; it starts and watches everything else."""
    from ghost_up import up_running

    GHOST_DIR.mkdir(mode=0o700, exist_ok=True)
    # Two quick asks from the extension must not start two supervisors.
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if up_running():
            return "running"
        bridge_was_up = is_listening("127.0.0.1", config["port"])
        spawn(["up"])
        if bridge_was_up:
            return "running"
        return "started" if wait_listening("127.0.0.1", config["port"]) else "starting"


def answer(message: dict | None) -> dict:
    if not message or message.get("type") != "pair":
        return {"ok": False, "error": "Unknown request"}
    try:
        token = load_bridge_token(create=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    from ghost_up import load_or_create_config

    config = load_or_create_config()
    if "GHOST_BRIDGE_PORT" in os.environ:
        config["port"] = int(os.environ["GHOST_BRIDGE_PORT"])
    try:
        bridge = ensure_up(config)
    except Exception as exc:
        bridge = f"failed: {str(exc)[:160]}"
    return {"ok": True, "token": token, "port": config["port"], "bridge": bridge}


def extension_id(extension_dir: Path) -> str:
    """The id Chrome gives an unpacked extension loaded from this folder."""
    digest = hashlib.sha256(str(extension_dir.resolve()).encode()).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def manifest_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        browsers = ["Google/Chrome", "Google/Chrome Beta", "Google/Chrome Canary", "Chromium", "BraveSoftware/Brave-Browser"]
    else:
        base = home / ".config"
        browsers = ["google-chrome", "google-chrome-beta", "chromium", "BraveSoftware/Brave-Browser"]
    return [base / b / "NativeMessagingHosts" for b in browsers if (base / b).is_dir()]


def install_native_host(extension_dir: Path | None = None, python: str | None = None) -> dict:
    """Write the launcher and the host manifest for each installed Chrome-family browser."""
    extension_dir = extension_dir or REPO_ROOT / "extension"
    ghost_dir = Path.home() / ".ghost"
    ghost_dir.mkdir(mode=0o700, exist_ok=True)
    # Chrome starts hosts with a bare environment, so name the interpreter exactly.
    launcher = ghost_dir / "native-host"
    launcher.write_text(f'#!/bin/sh\nexec "{python or sys.executable}" "{REPO_ROOT / "native_host.py"}" "$@"\n')
    launcher.chmod(0o700)
    ext_id = extension_id(extension_dir)
    manifest = {
        "name": HOST_NAME,
        "description": "Ghost bridge pairing",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }
    written = []
    for folder in manifest_dirs():
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{HOST_NAME}.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        written.append(str(path))
    return {"extension_id": ext_id, "launcher": str(launcher), "manifests": written}


def main() -> None:
    # Chrome passes the caller's origin as the first argument.
    write_message(sys.stdout.buffer, answer(read_message(sys.stdin.buffer)))


if __name__ == "__main__":
    main()
