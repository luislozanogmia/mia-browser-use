"""Run everything Ghost needs on this Mac, and keep it running: `mia-browser-use up`.

Chrome starts this through the native host (native_host.py) whenever the
extension can't reach the bridge, so nobody has to start anything by hand.
It runs, as child processes:

- the room relay, when the room lives on this machine;
- the bridge the extension talks to. Its agents (one per tab) answer
  questions asked on pages and do the chat's tasks (ghost_chat.py).

A child that exits is started again, waiting longer each time it keeps
failing. A port someone else already holds (a bridge started by hand while
developing) is left alone and checked again later. On first run it creates
a personal room, so a fresh install works with no settings at all.
"""

from __future__ import annotations

import getpass
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import native_host
from native_host import GHOST_DIR, LOCAL_HOSTS, is_listening

UP_PID_PATH = GHOST_DIR / "up.pid"
DEFAULT_RELAY_PORT = 9390
CHECK_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
HEALTHY_SECONDS = 30.0  # a child that ran this long starts over at the shortest wait


def personal_config() -> dict:
    """Settings for a fresh install: a room of one's own on this Mac."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "me"
    me = re.sub(r"[^A-Za-z0-9_.-]", "", user)[:64] or "me"
    name = me
    try:
        full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=5).stdout.strip()
        if full and len(full) <= 64 and full.isprintable():
            name = full
    except Exception:
        pass
    return native_host.clean_bridge_config({
        "port": native_host.DEFAULT_PORT, "room": f"home-{me}"[:64], "me": me, "name": name,
        "room_url": f"ws://127.0.0.1:{DEFAULT_RELAY_PORT}", "color": "#3b82f6",
    })


def load_or_create_config() -> dict:
    if native_host.CONFIG_PATH.is_file():
        config = native_host.load_bridge_config()
        if "room" in config:
            return config
    config = personal_config()
    native_host.save_bridge_config(config["port"], config["room"], config.get("room_url"),
                                   config["me"], config.get("name"), config.get("color"))
    return config


def local_relay(config: dict) -> int | None:
    """The relay port when the room lives on this machine (we run its relay)."""
    if "room" not in config:
        return None
    url = urlparse(config.get("room_url") or f"ws://127.0.0.1:{DEFAULT_RELAY_PORT}")
    if url.scheme != "ws" or url.hostname not in LOCAL_HOSTS:
        return None
    return url.port or 80


def children(config: dict) -> list[dict]:
    """What to run, in start order. `port` is the port the child listens on, if any."""
    out = []
    relay = local_relay(config)
    if relay:
        out.append({"name": "room", "port": relay,
                    "args": ["room", "serve", "--room", config["room"], "--port", str(relay)]})
    bridge = ["serve", "--port", str(config["port"])]
    if "room" in config:
        bridge += ["--room", config["room"], "--me", config["me"]]
        for key in ("room_url", "name", "color"):
            if key in config:
                bridge += [f"--{key.replace('_', '-')}", config[key]]
    out.append({"name": "bridge", "port": config["port"], "args": bridge})
    return out


class Supervisor:
    def __init__(self, config: dict, spawn=None, listening=is_listening, clock=time.monotonic):
        self.specs = children(config)
        # Children share this process group, so stopping `up` stops them too.
        self.spawn = spawn or (lambda args: native_host.spawn(args, detach=False))
        self.listening = listening
        self.clock = clock
        self.procs: dict[str, subprocess.Popen] = {}
        self.started: dict[str, float] = {}
        self.backoff: dict[str, float] = {}
        self.next_try: dict[str, float] = {}
        self.stopping = False

    def tick(self) -> None:
        now = self.clock()
        for spec in self.specs:
            name = spec["name"]
            proc = self.procs.get(name)
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                # It stopped: wait longer each time it keeps failing quickly.
                ran = now - self.started.get(name, now)
                wait = 1.0 if ran >= HEALTHY_SECONDS else min(self.backoff.get(name, 0.5) * 2, MAX_BACKOFF_SECONDS)
                self.backoff[name] = wait
                self.next_try[name] = now + wait
                del self.procs[name]
                print(f"[up] {name} stopped (exit {proc.returncode}); starting it again in {wait:.0f}s", flush=True)
            if now < self.next_try.get(name, 0):
                continue
            if spec["port"] and self.listening("127.0.0.1", spec["port"]):
                continue  # someone else runs it; look again later
            self.procs[name] = self.spawn(spec["args"])
            self.started[name] = now
            print(f"[up] started {name}", flush=True)
            if spec["port"]:
                # Later children connect to this one, so give it a moment.
                native_host.wait_listening("127.0.0.1", spec["port"], 5.0)

    def stop(self, *_):
        self.stopping = True
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 5
        for proc in self.procs.values():
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
        try:
            while not self.stopping:
                self.tick()
                time.sleep(CHECK_SECONDS)
        finally:
            self.stop()


def up_running() -> bool:
    try:
        pid = int(UP_PID_PATH.read_text().strip())
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except (OSError, ValueError):
        return False
    return "ghost_cli.py up" in command


def main() -> None:
    GHOST_DIR.mkdir(mode=0o700, exist_ok=True)
    UP_PID_PATH.write_text(f"{os.getpid()}\n")
    config = load_or_create_config()
    print(f"[up] room {config.get('room', '(none)')} as {config.get('me', '-')}; bridge port {config['port']}", flush=True)
    try:
        Supervisor(config).run()
    finally:
        try:
            if UP_PID_PATH.read_text().strip() == str(os.getpid()):
                UP_PID_PATH.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
