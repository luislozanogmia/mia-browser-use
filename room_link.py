"""The bridge's connection to a Ghost room.

One link per machine. It joins the room as the local human, registers the
bots that act through this bridge, publishes their presence and activity for
pages the room shares, and hands everything it receives to the bridge so the
bridge can draw other people's bots and cursors in this browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ghost_room import ID_RE, page_key, room_origin, room_safe_href

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
OPAQUE_PAGE_PREFIX = "https://room.invalid/p/"


class RoomLink:
    def __init__(
        self,
        url: str,
        room: str,
        key: str,
        me: dict,
        on_message: Callable[[dict], Awaitable[None]],
    ):
        if not url.startswith(("ws://", "wss://")):
            raise ValueError("room url must start with ws:// or wss://")
        if url.startswith("ws://") and not re.match(r"^ws://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", url):
            raise ValueError("Use wss:// for a room that is not on this machine")
        self.url = url
        self.room = room
        self.key = key
        self.me = me
        self.on_message = on_message
        self.bots: dict[str, dict] = {}
        self.shared: dict[str, dict] = {}  # page key -> page
        self.local_pages: dict[str, str] = {}  # local origin+path -> opaque room page URL
        self.my_shares: dict[str, dict] = {}  # opaque room page URL -> the share sent for it, re-sent on rejoin
        self.presence: dict[str, dict] = {}  # remote actor id -> last presence
        self.members: dict[str, dict] = {}
        self.suggestions: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}  # suggestion id -> resolved message
        self.asks: dict[str, dict] = {}  # open questions from humans
        self.changed = asyncio.Event()  # set when a question arrives or the shared pages change
        self.connected = False
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        delay = RECONNECT_MIN
        while True:
            try:
                # The join snapshot carries every open question (with its selected text) and
                # the latest notes; 64 KB closed the connection on a busy room, every time.
                async with connect(self.url, max_size=4 * 1024 * 1024) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"action": "join", "room": self.room, "key": self.key, "actor": self.me}))
                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        if message.get("type") == "joined":
                            self.connected = True
                            delay = RECONNECT_MIN
                            for bot in self.bots.values():
                                await ws.send(json.dumps({"action": "actor", "actor": bot}))
                            # The room drops a member's pages when they leave: share ours again.
                            have = {p["url"] for p in message.get("pages", [])}
                            for remote, share in self.my_shares.items():
                                if remote not in have:
                                    await ws.send(json.dumps(share, ensure_ascii=False))
                        self._track(message)
                        await self.on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[room] connection lost: {exc}")
            self.connected = False
            self._ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    # -- state from the room ------------------------------------------------

    def _mine(self, actor: dict) -> bool:
        return actor.get("id") == self.me["id"] or actor.get("id") in self.bots

    def _track(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "joined":
            self.shared = {p["url"]: p for p in message.get("pages", [])}
            self.members = {a["id"]: a for a in message.get("members", [])}
            self.presence = {p["actor"]["id"]: p for p in message.get("presence", []) if not self._mine(p["actor"])}
            self.suggestions = {s["id"]: s for s in message.get("suggestions", [])}
            self.asks = {a["id"]: a for a in message.get("asks", [])}
            self.changed.set()
        elif kind == "page":
            page = message["page"]
            if message.get("event") == "shared":
                self.shared[page["url"]] = page
            else:
                self.shared.pop(page["url"], None)
            self.changed.set()
        elif kind == "member":
            actor = message["actor"]
            if message.get("event") == "joined":
                self.members[actor["id"]] = actor
            else:
                self.members.pop(actor["id"], None)
                self.presence.pop(actor["id"], None)
        elif kind == "presence" and not self._mine(message["actor"]):
            if message.get("status") == "gone":
                self.presence.pop(message["actor"]["id"], None)
            else:
                self.presence[message["actor"]["id"]] = message
        elif kind == "suggestion":
            self.suggestions[message["id"]] = message
            self.asks.pop(message.get("reply_to"), None)
            # Notes never resolve; keep the latest ones, as the room does.
            while len(self.suggestions) > 500:
                self.suggestions.pop(next(iter(self.suggestions)))
        elif kind == "ask":
            self.asks[message["id"]] = message
            self.changed.set()
        elif kind == "resolved":
            self.suggestions.pop(message.get("id"), None)
            self.decisions[message["id"]] = message
            while len(self.decisions) > 500:
                self.decisions.pop(next(iter(self.decisions)))

    def is_shared(self, url: Any) -> bool:
        key = self.room_page(url)
        return bool(key and key in self.shared)

    def room_page(self, url: Any) -> str | None:
        key = page_key(url)
        return self.local_pages.get(key, key) if key else None

    def bind_local(self, url: Any, room_url: Any) -> bool:
        local, remote = page_key(url), page_key(room_url)
        if not local or not remote or remote != room_url or not remote.startswith(OPAQUE_PAGE_PREFIX):
            return False
        page = self.shared.get(remote)
        if page and page.get("origin") != room_origin(local):
            return False
        self.local_pages[local] = remote
        return True

    def presence_for(self, url: Any) -> list[dict]:
        key = self.room_page(url)
        return [p for p in self.presence.values() if p.get("url") == key]

    def suggestions_for(self, url: Any) -> list[dict]:
        key = self.room_page(url)
        return [s for s in self.suggestions.values() if s.get("url") == key]

    def asks_for(self, url: Any) -> list[dict]:
        key = self.room_page(url)
        return [a for a in self.asks.values() if a.get("url") == key]

    # -- publishing -----------------------------------------------------------

    async def send(self, message: dict) -> None:
        if self._ws is None or not self.connected:
            return
        try:
            outgoing = dict(message)
            if outgoing.get("action") == "share" and isinstance(outgoing.get("page"), dict):
                source = outgoing["page"]
                local, origin = page_key(source.get("url")), room_origin(source.get("url"))
                if not local or not origin:
                    return
                remote = source.get("room_url") or self.local_pages.get(local) or OPAQUE_PAGE_PREFIX + secrets.token_urlsafe(18)
                if not isinstance(remote, str) or not remote.startswith(OPAQUE_PAGE_PREFIX) or page_key(remote) != remote:
                    return
                self.local_pages[local] = remote
                outgoing["page"] = {"url": remote, "origin": origin, "title": origin,
                                    **({"href": source["url"]} if source.get("share_link") is True else {})}
            elif "url" in outgoing:
                remote = self.room_page(outgoing["url"])
                if not remote or not remote.startswith(OPAQUE_PAGE_PREFIX):
                    return
                outgoing["url"] = remote
            if isinstance(outgoing.get("links"), list):
                outgoing["links"] = [{**link, "href": room_safe_href(link.get("href"))}
                                     for link in outgoing["links"] if isinstance(link, dict) and room_safe_href(link.get("href"))]
            await self._ws.send(json.dumps(outgoing, ensure_ascii=False))
            if outgoing.get("action") == "share":
                self.my_shares[outgoing["page"]["url"]] = outgoing
            if outgoing.get("action") == "unshare":
                self.my_shares.pop(outgoing["url"], None)
                self.local_pages = {local: remote for local, remote in self.local_pages.items()
                                    if remote != outgoing["url"]}
        except Exception:
            pass

    async def ensure_bot(self, actor_id: str, look: Optional[dict] = None) -> None:
        """Register a bot acting through this bridge, owned by the local human."""
        if actor_id == self.me["id"] or not ID_RE.match(actor_id):
            return
        look = look or {}
        bot = dict(self.bots.get(actor_id) or {"id": actor_id, "kind": "bot", "name": actor_id, "owner": self.me["id"]})
        if self.me.get("color"):
            bot["owner_color"] = self.me["color"]
        changed = actor_id not in self.bots
        name = look["label"].split("·")[0].strip()[:40] if isinstance(look.get("label"), str) else ""
        if name and name != bot["name"]:
            bot["name"] = name  # follows the bot's label, e.g. once it's named after its site
            changed = True
        if isinstance(look.get("color"), str) and look["color"] != bot.get("color"):
            bot["color"] = look["color"]
            changed = True
        self.bots[actor_id] = bot
        if changed:
            await self.send({"action": "actor", "actor": bot})

    async def retire_bot(self, actor_id: str) -> None:
        """The bot leaves the room, and isn't brought back when the link reconnects."""
        if self.bots.pop(actor_id, None) is not None:
            self.members.pop(actor_id, None)
            self.presence.pop(actor_id, None)
            await self.send({"action": "retire", "actor_id": actor_id})


ROOM_KEY_DIR = Path.home() / ".ghost" / "rooms"


def room_key_path(room: str) -> Path:
    if not ID_RE.match(room):
        raise ValueError("room must be 1-64 of A-Z a-z 0-9 _ . : -")
    return ROOM_KEY_DIR / f"{room}.key"


def save_room_key(room: str, key: str) -> Path:
    """Store a room key in a private file (dir 0700, file 0600)."""
    path = room_key_path(room)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    os.chmod(path, 0o600)
    return path


def load_room_key(room: str) -> Optional[str]:
    """Room key from GHOST_ROOM_KEY, the file in GHOST_ROOM_KEY_FILE, or ~/.ghost/rooms/<room>.key."""
    key = os.environ.get("GHOST_ROOM_KEY", "").strip()
    if key:
        return key
    path = Path(os.environ.get("GHOST_ROOM_KEY_FILE") or room_key_path(room)).expanduser()
    try:
        info = path.stat()
    except OSError:
        return None
    if info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise ValueError(f"Room key file must be private to you (0600): {path}")
    return path.read_text(encoding="utf-8").strip() or None
