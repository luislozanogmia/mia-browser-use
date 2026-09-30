"""Ghost room relay: share who is working on what across machines.

Every machine's Ghost bridge joins a room. Bots and humans publish presence
("Ledger is working on B2:B6 of this sheet"), activity events and suggestions;
the room relays them to everyone else, and each bridge draws them over the
same page in its own browser.

`RoomHub` holds all protocol logic and never touches sockets: it takes a
connection id and a message and returns the messages to deliver. The local
WebSocket server below wraps it, and the same hub can sit behind AWS API
Gateway WebSockets + Lambda by swapping `MemoryRoomStore` for a shared store.

Client -> room (the `action` field is the API Gateway route key):
    join      {room, key, actor}             first message on a connection
    actor     {actor}                        add a bot run from this machine
    share     {page: {url, title}}           make a page visible to the room
    unshare   {url}
    presence  {actor_id, url, target, pointer, label, status, ttl_ms}
    activity  {actor_id, url, call_id, op, status, target, error}   op: fill, click, ...
    suggest   {actor_id, url, id, target, title, body}
    resolve   {id, decision: accept|reject}
    leave     {}

Room -> client: joined, member, page, presence, activity, suggestion,
resolved, error. Everything a client sends is untrusted and re-validated here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

MAX_MESSAGE_BYTES = 16 * 1024
MAX_MEMBERS_PER_ROOM = 64
MAX_ACTORS_PER_CONNECTION = 16
MAX_PAGES_PER_ROOM = 32
MAX_SUGGESTIONS_PER_ROOM = 200
DEFAULT_PRESENCE_TTL_MS = 5 * 60 * 1000
MAX_PRESENCE_TTL_MS = 60 * 60 * 1000
RATE_LIMIT_PER_SECOND = 30
MIN_KEY_BYTES = 32

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
KINDS = {"human", "bot"}
STATUSES = {"working", "done", "failed", "idle", "gone"}
ACTIVITY_STATUSES = {"started", "done", "failed"}


class RoomError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def page_key(url: Any) -> Optional[str]:
    """The part of a URL that identifies a shared page: origin + path."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{host}{parts.path.rstrip('/') or '/'}"


def key_digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def new_room_key() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Validation of untrusted client fields
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _ident(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise RoomError("INVALID", f"{name} must be 1-64 of A-Z a-z 0-9 _ . : -")
    return value


def _color(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and COLOR_RE.match(value) else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or abs(value) > 1e7:  # NaN or absurd
        return None
    return float(value)


def clean_rect(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    rect = {k: _number(value.get(k)) for k in ("x", "y", "w", "h")}
    return rect if all(v is not None for v in rect.values()) else None


def clean_anchor(value: Any) -> Optional[dict]:
    """A target another browser can find: selector, text snippet, page rect, or a sheet range."""
    if not isinstance(value, dict):
        return None
    anchor: dict[str, Any] = {}
    if isinstance(value.get("selector"), str) and value["selector"]:
        anchor["selector"] = value["selector"][:512]
    text = _text(value.get("text"), 200)
    if text:
        anchor["text"] = text
    rect = clean_rect(value.get("rect"))
    if rect:
        anchor["rect"] = rect
    if isinstance(value.get("range"), str) and re.match(r"^[A-Za-z0-9!:$' ._-]{1,64}$", value["range"]):
        anchor["range"] = value["range"]
    return anchor or None


def clean_pointer(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    anchor = clean_anchor(value.get("anchor"))
    fx, fy = _number(value.get("fx")), _number(value.get("fy"))
    if not anchor or fx is None or fy is None:
        return None
    return {"anchor": anchor, "fx": min(max(fx, 0.0), 1.0), "fy": min(max(fy, 0.0), 1.0)}


def clean_actor(value: Any) -> dict:
    if not isinstance(value, dict):
        raise RoomError("INVALID", "actor must be an object")
    actor = {
        "id": _ident(value.get("id"), "actor.id"),
        "kind": value.get("kind") if value.get("kind") in KINDS else "bot",
        "name": _text(value.get("name"), 40) or value.get("id"),
    }
    for key in ("color", "owner_color"):
        color = _color(value.get(key))
        if color:
            actor[key] = color
    if isinstance(value.get("owner"), str) and ID_RE.match(value["owner"]):
        actor["owner"] = value["owner"]
    return actor


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Member:
    conn_id: str
    room: str
    actors: dict[str, dict] = field(default_factory=dict)  # actor id -> actor
    tokens: float = RATE_LIMIT_PER_SECOND
    refilled: float = 0.0


@dataclass
class Room:
    name: str
    key_digest: bytes
    members: dict[str, Member] = field(default_factory=dict)  # conn id -> member
    pages: dict[str, dict] = field(default_factory=dict)  # page key -> {url, title, by}
    presence: dict[str, dict] = field(default_factory=dict)  # actor id -> last presence
    suggestions: dict[str, dict] = field(default_factory=dict)  # id -> suggestion


class MemoryRoomStore:
    """In-process room state. A Lambda deployment replaces this with a shared store."""

    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.connections: dict[str, str] = {}  # conn id -> room name

    def create_room(self, name: str, key: str) -> Room:
        _ident(name, "room")
        if len(key.encode()) < MIN_KEY_BYTES:
            raise RoomError("INVALID", f"room key must have at least {MIN_KEY_BYTES} bytes")
        room = Room(name=name, key_digest=key_digest(key))
        self.rooms[name] = room
        return room


Outbound = list[tuple[str, dict]]


class RoomHub:
    def __init__(self, store: Optional[MemoryRoomStore] = None, clock: Callable[[], float] = time.time):
        self.store = store or MemoryRoomStore()
        self.clock = clock

    # -- helpers ---------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _member(self, conn_id: str) -> tuple[Room, Member]:
        name = self.store.connections.get(conn_id)
        room = self.store.rooms.get(name) if name else None
        if not room or conn_id not in room.members:
            raise RoomError("NOT_JOINED", "Send join first")
        return room, room.members[conn_id]

    @staticmethod
    def _to_others(room: Room, conn_id: str, message: dict) -> Outbound:
        return [(other, message) for other in room.members if other != conn_id]

    @staticmethod
    def _to_all(room: Room, message: dict) -> Outbound:
        return [(other, message) for other in room.members]

    def _owned_actor(self, member: Member, value: Any) -> dict:
        actor_id = _ident(value, "actor_id")
        actor = member.actors.get(actor_id)
        if not actor:
            raise RoomError("NOT_YOUR_ACTOR", f"{actor_id} was not registered by this connection")
        return actor

    def _shared_page(self, room: Room, url: Any) -> tuple[str, dict]:
        key = page_key(url)
        if not key or key not in room.pages:
            raise RoomError("PAGE_NOT_SHARED", "Share the page with the room first")
        return key, room.pages[key]

    def _rate_ok(self, member: Member) -> bool:
        now = self.clock()
        member.tokens = min(RATE_LIMIT_PER_SECOND, member.tokens + (now - member.refilled) * RATE_LIMIT_PER_SECOND)
        member.refilled = now
        if member.tokens < 1:
            return False
        member.tokens -= 1
        return True

    def _expire(self, room: Room) -> None:
        now = self._now_ms()
        for actor_id in [a for a, p in room.presence.items() if p["expires_at"] <= now]:
            del room.presence[actor_id]

    def _register_actor(self, room: Room, member: Member, actor: dict) -> None:
        for other in room.members.values():
            if other is not member and actor["id"] in other.actors:
                raise RoomError("ACTOR_TAKEN", f"{actor['id']} is already in this room")
        if actor["id"] not in member.actors and len(member.actors) >= MAX_ACTORS_PER_CONNECTION:
            raise RoomError("LIMIT", "Too many actors on one connection")
        member.actors[actor["id"]] = actor

    # -- entry points ------------------------------------------------------

    def handle_raw(self, conn_id: str, raw: str | bytes) -> Outbound:
        if len(raw) > MAX_MESSAGE_BYTES:
            return [(conn_id, {"type": "error", "code": "TOO_LARGE", "message": "Message too large"})]
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return [(conn_id, {"type": "error", "code": "INVALID", "message": "Invalid JSON"})]
        return self.handle(conn_id, message)

    def handle(self, conn_id: str, message: Any) -> Outbound:
        try:
            if not isinstance(message, dict):
                raise RoomError("INVALID", "Message must be an object")
            action = message.get("action")
            handler = getattr(self, f"_on_{action}", None) if isinstance(action, str) and action.isidentifier() else None
            if handler is None:
                raise RoomError("UNKNOWN_ACTION", f"Unknown action: {action}")
            if action != "join":
                _room, member = self._member(conn_id)
                if not self._rate_ok(member):
                    raise RoomError("RATE_LIMITED", "Too many messages")
            return handler(conn_id, message)
        except RoomError as exc:
            reply = {"type": "error", "code": exc.code, "message": str(exc)}
            if isinstance(message, dict) and isinstance(message.get("ref"), str):
                reply["ref"] = message["ref"][:64]
            return [(conn_id, reply)]

    def disconnect(self, conn_id: str) -> Outbound:
        name = self.store.connections.pop(conn_id, None)
        room = self.store.rooms.get(name) if name else None
        if not room or conn_id not in room.members:
            return []
        member = room.members.pop(conn_id)
        out: Outbound = []
        for actor_id, actor in member.actors.items():
            room.presence.pop(actor_id, None)
            out += self._to_all(room, {"type": "member", "event": "left", "actor": actor})
        return out

    # -- actions -----------------------------------------------------------

    def _on_join(self, conn_id: str, message: dict) -> Outbound:
        if conn_id in self.store.connections:
            raise RoomError("ALREADY_JOINED", "This connection already joined a room")
        name = _ident(message.get("room"), "room")
        room = self.store.rooms.get(name)
        key = message.get("key")
        # Same failure for a missing room and a wrong key, compared in constant time.
        expected = room.key_digest if room else key_digest(secrets.token_hex(32))
        if not isinstance(key, str) or not hmac.compare_digest(key_digest(key), expected) or not room:
            raise RoomError("AUTH_FAILED", "Unknown room or wrong key")
        if len(room.members) >= MAX_MEMBERS_PER_ROOM:
            raise RoomError("ROOM_FULL", "The room is full")
        actor = clean_actor(message.get("actor"))
        member = Member(conn_id=conn_id, room=name, refilled=self.clock())
        room.members[conn_id] = member
        try:
            self._register_actor(room, member, actor)
        except RoomError:
            del room.members[conn_id]
            raise
        self.store.connections[conn_id] = name
        self._expire(room)
        snapshot = {
            "type": "joined",
            "room": name,
            "you": actor,
            "members": [a for m in room.members.values() for a in m.actors.values()],
            "pages": list(room.pages.values()),
            "presence": list(room.presence.values()),
            "suggestions": [s for s in room.suggestions.values() if s["state"] == "open"],
        }
        return [(conn_id, snapshot)] + self._to_others(room, conn_id, {"type": "member", "event": "joined", "actor": actor})

    def _on_actor(self, conn_id: str, message: dict) -> Outbound:
        room, member = self._member(conn_id)
        actor = clean_actor(message.get("actor"))
        self._register_actor(room, member, actor)
        return self._to_all(room, {"type": "member", "event": "joined", "actor": actor})

    def _on_share(self, conn_id: str, message: dict) -> Outbound:
        room, member = self._member(conn_id)
        page = message.get("page") if isinstance(message.get("page"), dict) else {}
        key = page_key(page.get("url"))
        if not key:
            raise RoomError("INVALID", "page.url must be an http(s) URL")
        if key not in room.pages and len(room.pages) >= MAX_PAGES_PER_ROOM:
            raise RoomError("LIMIT", "Too many shared pages")
        by = next(iter(member.actors))
        room.pages[key] = {"url": key, "title": _text(page.get("title"), 200), "by": by}
        return self._to_all(room, {"type": "page", "event": "shared", "page": room.pages[key]})

    def _on_unshare(self, conn_id: str, message: dict) -> Outbound:
        room, _member = self._member(conn_id)
        key, page = self._shared_page(room, message.get("url"))
        del room.pages[key]
        for actor_id in [a for a, p in room.presence.items() if p["url"] == key]:
            del room.presence[actor_id]
        return self._to_all(room, {"type": "page", "event": "unshared", "page": page})

    def _on_presence(self, conn_id: str, message: dict) -> Outbound:
        room, member = self._member(conn_id)
        actor = self._owned_actor(member, message.get("actor_id"))
        key, _page = self._shared_page(room, message.get("url"))
        status = message.get("status") if message.get("status") in STATUSES else "working"
        if status == "gone":
            room.presence.pop(actor["id"], None)
            return self._to_others(room, conn_id, {"type": "presence", "actor": actor, "url": key, "status": "gone"})
        ttl = message.get("ttl_ms")
        ttl = int(min(max(ttl, 1000), MAX_PRESENCE_TTL_MS)) if isinstance(ttl, int) and not isinstance(ttl, bool) else DEFAULT_PRESENCE_TTL_MS
        presence = {
            "type": "presence",
            "actor": actor,
            "url": key,
            "target": clean_anchor(message.get("target")),
            "pointer": clean_pointer(message.get("pointer")),
            "label": _text(message.get("label"), 80),
            "status": status,
            "ts": self._now_ms(),
            "expires_at": self._now_ms() + ttl,
        }
        room.presence[actor["id"]] = presence
        return self._to_others(room, conn_id, presence)

    def _on_activity(self, conn_id: str, message: dict) -> Outbound:
        room, member = self._member(conn_id)
        actor = self._owned_actor(member, message.get("actor_id"))
        key, _page = self._shared_page(room, message.get("url"))
        status = message.get("status")
        if status not in ACTIVITY_STATUSES:
            raise RoomError("INVALID", "status must be started, done or failed")
        event = {
            "type": "activity",
            "actor": actor,
            "url": key,
            "call_id": _text(message.get("call_id"), 64),
            "op": _text(message.get("op"), 40),
            "status": status,
            "target": clean_anchor(message.get("target")),
            "ts": self._now_ms(),
        }
        if status == "failed":
            event["error"] = _text(message.get("error"), 200)
        return self._to_others(room, conn_id, event)

    def _on_suggest(self, conn_id: str, message: dict) -> Outbound:
        room, member = self._member(conn_id)
        actor = self._owned_actor(member, message.get("actor_id"))
        key, _page = self._shared_page(room, message.get("url"))
        sid = _ident(message.get("id") or secrets.token_hex(6), "id")
        open_count = sum(1 for s in room.suggestions.values() if s["state"] == "open")
        if sid not in room.suggestions and open_count >= MAX_SUGGESTIONS_PER_ROOM:
            raise RoomError("LIMIT", "Too many open suggestions")
        suggestion = {
            "type": "suggestion",
            "id": sid,
            "actor": actor,
            "url": key,
            "target": clean_anchor(message.get("target")),
            "title": _text(message.get("title"), 120),
            "body": _text(message.get("body"), 600),
            "state": "open",
            "ts": self._now_ms(),
        }
        room.suggestions[sid] = suggestion
        return self._to_all(room, suggestion)

    def _on_resolve(self, conn_id: str, message: dict) -> Outbound:
        room, member = self._member(conn_id)
        sid = _ident(message.get("id"), "id")
        suggestion = room.suggestions.get(sid)
        if not suggestion or suggestion["state"] != "open":
            raise RoomError("NOT_FOUND", "No open suggestion with that id")
        decision = message.get("decision")
        if decision not in {"accept", "reject"}:
            raise RoomError("INVALID", "decision must be accept or reject")
        # Only a human decides; the bot that made the suggestion applies it.
        humans = [a for a in member.actors.values() if a["kind"] == "human"]
        if not humans:
            raise RoomError("FORBIDDEN", "Only a human can accept or reject a suggestion")
        suggestion["state"] = "accepted" if decision == "accept" else "rejected"
        resolved = {"type": "resolved", "id": sid, "decision": decision, "by": humans[0], "actor": suggestion["actor"], "url": suggestion["url"]}
        return self._to_all(room, resolved)

    def _on_leave(self, conn_id: str, _message: dict) -> Outbound:
        return self.disconnect(conn_id)


# ---------------------------------------------------------------------------
# Local WebSocket server
# ---------------------------------------------------------------------------

async def serve_room(hub: RoomHub, host: str = "127.0.0.1", port: int = 9390):
    """Run the hub on a WebSocket server; returns the server object."""
    from websockets.asyncio.server import serve

    connections: dict[str, Any] = {}

    async def deliver(outbound: Outbound):
        for target, message in outbound:
            ws = connections.get(target)
            if ws is not None:
                try:
                    await ws.send(json.dumps(message, ensure_ascii=False))
                except Exception:
                    pass

    async def handler(websocket):
        conn_id = secrets.token_hex(8)
        connections[conn_id] = websocket
        try:
            async for raw in websocket:
                await deliver(hub.handle_raw(conn_id, raw))
        except Exception:
            pass
        finally:
            connections.pop(conn_id, None)
            await deliver(hub.disconnect(conn_id))

    return await serve(handler, host, port, max_size=MAX_MESSAGE_BYTES)
