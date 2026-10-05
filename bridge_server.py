"""Ghost Bridge Server for the Chrome extension.

Agents call the authenticated loopback HTTP API, which forwards commands to an
authenticated extension connection and returns bounded JSON results.

Usage:
    python3 bridge_server.py [--port 9377]

The server exposes:
    ws://127.0.0.1:9377/ghost-bridge  — Chrome extension connects here
    http://127.0.0.1:9378/call         — Agents POST commands here (JSON-RPC style)
    http://127.0.0.1:9378/status       — GET connection status
"""

import asyncio
import json
import argparse
import hashlib
import hmac
import re
import secrets
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pdf_reader import (
    DEFAULT_MAX_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_PDF_BYTES,
    PdfReadError,
    extract_pdf,
)
from bridge_auth import (
    AUTH_NONCE_HEADER,
    AUTH_SIGNATURE_HEADER,
    AUTH_TIMESTAMP_HEADER,
    AUTH_INSTANCE_HEADER,
    AUTH_CHALLENGE_HEADER,
    AUTH_WINDOW_SECONDS,
    RESPONSE_SIGNATURE_HEADER,
    BridgeAuthError,
    challenge_init_signature,
    challenge_signature,
    load_bridge_token,
    response_signature,
    token_path,
)
from bridge_auth import _request_message
from ghost_tool_defs import TOOL_NAMES
from ghost_room import clean_language, page_key
from reel_story import write_story
from chat_store import ChatStore
from ghost_chat import ChatHub

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    print("Install websockets: pip install websockets>=14.0")
    sys.exit(1)

try:
    from aiohttp import web
except ImportError:
    print("Install aiohttp: pip install aiohttp")
    sys.exit(1)


MAX_HTTP_BODY_BYTES = 1024 * 1024
# One extension message: a cropped picture (up to 1 MiB as a data URL), a full-page
# screenshot result, or the reel's digest for its PDF. The websockets default of
# 1 MiB closed the connection on any of those.
MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024
WS_NONCE_BYTES = 32
# room_* commands are answered by the bridge itself and are not model tools.
ROOM_COMMANDS = {"ghost_room", "room_share", "room_unshare", "room_resolve", "room_ask_image", "room_report", "room_approved_tabs", "room_read"}
MAX_REPORT_CHARS = 200_000
MAX_ASK_IMAGE_CHARS = 1024 * 1024  # a cropped area, as a JPEG data URL
MAX_ASK_IMAGES = 20
HTTP_COMMANDS = TOOL_NAMES | {"ping"} | ROOM_COMMANDS
# Actor calls that don't touch a page produce no room activity.
QUIET_COMMANDS = {"ghost_status", "ghost_tab_list", "ghost_tab_open", "ping"}
MAX_PENDING_CHALLENGES = 1024
MAX_INIT_NONCES = 4096


class BridgeServer:
    def __init__(self, port=9377, token=None, allow_eval=False, room=None):
        self.port = port
        # Optional RoomLink; without it Ghost is single-machine as before.
        self.room = room
        self.tab_urls = {}  # tab id -> url, from command results and tab events
        self.room_accepted_tabs = {}  # tab id -> {url: exact local URL, room_url: opaque page ID}
        # Pictures of cropped areas asked about here. They stay on this machine:
        # the relay only carries the question and where the area is.
        self.ask_images = {}
        self.ask_threads = {}  # conversation -> the question whose picture it is about
        self.bot_looks = {}  # actor id -> last label/color it showed
        self.token = token or load_bridge_token(create=True)
        self.extension_ws = None
        self.pending = {}  # id -> Future
        self.story_busy = False  # one reel PDF written at a time
        self.pending_pdf_uploads = {}  # one-time token -> Future[bytes]
        self.connected = False
        self.allow_eval = allow_eval
        self.http_nonces = {}
        self.instance_id = secrets.token_hex(16)
        self.http_challenges = {}
        self.http_init_nonces = {}
        # Mia's side panel: its conversation, and the workers acting on tabs.
        self.chat = ChatHub(self._chat_call, self._chat_push, self._chat_room,
                            me=lambda: self.room.me["id"] if self.room else "", retire=self._chat_retire,
                            store=ChatStore())

    # ------------------------------------------------------------------
    # WebSocket handler — Chrome extension connects here
    # ------------------------------------------------------------------

    @staticmethod
    def _origin(websocket):
        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            headers = getattr(websocket, "request_headers", {})
        return headers.get("Origin") if headers else None

    async def ws_handler(self, websocket):
        origin = self._origin(websocket)
        if not origin or not origin.startswith("chrome-extension://"):
            await websocket.close(code=4003, reason="unauthorized origin")
            return
        try:
            raw_auth = await asyncio.wait_for(websocket.recv(), timeout=5)
            auth = json.loads(raw_auth)
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError):
            await websocket.close(code=4003, reason="authentication required")
            return

        client_nonce = auth.get("client_nonce", "") if auth.get("type") == "auth_init" else ""
        if (
            not isinstance(client_nonce, str)
            or len(client_nonce) != WS_NONCE_BYTES * 2
            or any(char not in "0123456789abcdef" for char in client_nonce)
        ):
            await websocket.close(code=4003, reason="authentication failed")
            return

        server_nonce = secrets.token_hex(WS_NONCE_BYTES)
        server_message = f"ghost-ws-server-v1:{client_nonce}:{server_nonce}".encode()
        server_proof = hmac.new(self.token.encode(), server_message, hashlib.sha256).hexdigest()
        await websocket.send(json.dumps({
            "type": "auth_challenge",
            "server_nonce": server_nonce,
            "server_proof": server_proof,
        }))
        try:
            raw_response = await asyncio.wait_for(websocket.recv(), timeout=5)
            response = json.loads(raw_response)
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError):
            await websocket.close(code=4003, reason="authentication failed")
            return
        supplied = response.get("client_proof", "") if response.get("type") == "auth_response" else ""
        client_message = f"ghost-ws-client-v1:{client_nonce}:{server_nonce}".encode()
        expected = hmac.new(self.token.encode(), client_message, hashlib.sha256).hexdigest()
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
            await websocket.close(code=4003, reason="authentication failed")
            return

        print("[bridge] Authenticated extension connected")
        previous = self.extension_ws
        if previous is not None and previous is not websocket:
            await previous.close(code=4000, reason="replaced by a newer extension connection")
        self.extension_ws = websocket
        self.connected = True
        await websocket.send(json.dumps({"type": "authenticated"}))
        if self.room:
            await self._push_shared_pages()

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Hello handshake
                if msg.get("type") == "hello":
                    print(f"[bridge] Extension v{msg.get('version', '?')} ready")
                    continue

                # Heartbeat
                if msg.get("type") == "heartbeat":
                    continue

                # Response to a pending command
                msg_id = msg.get("id")
                if msg_id and msg_id in self.pending:
                    self._remember_tab(msg.get("meta"))
                    self.pending[msg_id].set_result(msg)
                    continue

                # The reel asks for the words of its PDF; works with or without a room.
                if msg.get("type") == "reel_story":
                    asyncio.get_running_loop().create_task(self._reel_story(msg))
                    continue

                # Mia's side panel: messages, approvals and stops.
                if msg.get("type") == "chat" and isinstance(msg.get("chat"), dict):
                    print(f"[chat] from the panel: {str(msg['chat'].get('action'))[:20]}")
                    asyncio.get_running_loop().create_task(self.chat.handle(msg["chat"]))
                    continue

                # Events the extension pushes for multiplayer
                if msg.get("type") == "ask_cancel" and isinstance(msg.get("id"), str):
                    asyncio.get_running_loop().create_task(self.chat.cancel_ask(msg["id"][:64]))
                    continue
                if msg.get("type") in {"human", "tab_ready", "share", "unshare", "resolve", "ask", "room_access"}:
                    asyncio.get_running_loop().create_task(self._extension_event(msg))
                    continue

        except websockets.ConnectionClosed:
            pass
        finally:
            print("[bridge] Extension disconnected")
            if self.extension_ws is websocket:
                self.extension_ws = None
                self.connected = False
                self.room_accepted_tabs.clear()
                # Fail all pending requests
                for future in self.pending.values():
                    if not future.done():
                        future.set_result({"error": "Extension disconnected"})
                self.pending.clear()

    # ------------------------------------------------------------------
    # Send a command to the extension and wait for response
    # ------------------------------------------------------------------

    async def send_command(self, command, args=None, timeout=60):
        if not self.connected or not self.extension_ws:
            raise Exception("NO_EXTENSION: Chrome extension is not connected. "
                            "Install the Mia extension and click Connect.")

        msg_id = secrets.token_hex(8)
        future = asyncio.get_event_loop().create_future()
        self.pending[msg_id] = future

        try:
            await self.extension_ws.send(json.dumps({
                "id": msg_id,
                "command": command,
                "args": args or {},
            }))

            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            raise Exception(f"TIMEOUT: Command '{command}' timed out after {timeout}s")
        finally:
            self.pending.pop(msg_id, None)

    # ------------------------------------------------------------------
    # HTTP API — agents POST commands here
    # ------------------------------------------------------------------

    def _authorized(self, request, body=b""):
        timestamp = request.headers.get(AUTH_TIMESTAMP_HEADER, "")
        nonce = request.headers.get(AUTH_NONCE_HEADER, "")
        supplied = request.headers.get(AUTH_SIGNATURE_HEADER, "")
        instance = request.headers.get(AUTH_INSTANCE_HEADER, "")
        challenge = request.headers.get(AUTH_CHALLENGE_HEADER, "")
        try:
            timestamp_value = int(timestamp)
        except (TypeError, ValueError):
            return False
        now = int(time.time())
        challenge_state = self.http_challenges.get(challenge)
        if challenge_state is None:
            return False
        expires, _client_nonce = challenge_state
        if instance != self.instance_id or expires < now:
            return False
        if abs(now - timestamp_value) > AUTH_WINDOW_SECONDS:
            return False
        if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
            return False
        self.http_nonces = {
            value: seen for value, seen in self.http_nonces.items()
            if now - seen <= AUTH_WINDOW_SECONDS
        }
        if nonce in self.http_nonces:
            return False
        expected = hmac.new(
            self.token.encode(),
            _request_message(
                request.method,
                request.path,
                timestamp,
                nonce,
                instance,
                challenge,
                body,
            ),
            hashlib.sha256,
        ).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            return False
        self.http_challenges.pop(challenge, None)
        self.http_nonces[nonce] = now
        return True

    async def handle_challenge(self, request):
        now = int(time.time())
        timestamp = request.headers.get(AUTH_TIMESTAMP_HEADER, "")
        client_nonce = request.headers.get(AUTH_NONCE_HEADER, "")
        supplied = request.headers.get(AUTH_SIGNATURE_HEADER, "")
        try:
            timestamp_value = int(timestamp)
        except (TypeError, ValueError):
            return self._reject_unauthorized()
        if (
            abs(now - timestamp_value) > AUTH_WINDOW_SECONDS
            or len(client_nonce) != 32
            or any(char not in "0123456789abcdef" for char in client_nonce)
        ):
            return self._reject_unauthorized()
        expected = challenge_init_signature(self.token, client_nonce, timestamp)
        self.http_init_nonces = {
            value: seen for value, seen in self.http_init_nonces.items()
            if now - seen <= AUTH_WINDOW_SECONDS
        }
        if (
            client_nonce in self.http_init_nonces
            or not supplied
            or not hmac.compare_digest(supplied, expected)
        ):
            return self._reject_unauthorized()
        if len(self.http_init_nonces) >= MAX_INIT_NONCES:
            return web.json_response({"error": "Challenge rate limit reached"}, status=503)
        self.http_init_nonces[client_nonce] = now
        self.http_challenges = {
            value: state for value, state in self.http_challenges.items()
            if state[0] >= now
        }
        if len(self.http_challenges) >= MAX_PENDING_CHALLENGES:
            return web.json_response({"error": "Challenge capacity reached"}, status=503)
        challenge = secrets.token_hex(32)
        expires = now + AUTH_WINDOW_SECONDS
        self.http_challenges[challenge] = (expires, client_nonce)
        return web.json_response({
            "instance": self.instance_id,
            "client_nonce": client_nonce,
            "challenge": challenge,
            "expires": expires,
            "proof": challenge_signature(
                self.token,
                self.instance_id,
                client_nonce,
                challenge,
                expires,
            ),
        })

    def _signed_json_response(self, request, data, status=200):
        body = json.dumps(data, separators=(",", ":")).encode()
        nonce = request.headers.get(AUTH_NONCE_HEADER, "")
        headers = {
            RESPONSE_SIGNATURE_HEADER: response_signature(self.token, nonce, status, body)
        }
        return web.Response(body=body, status=status, content_type="application/json", headers=headers)

    def _reject_unauthorized(self):
        return web.json_response(
            {"error": "UNAUTHORIZED"},
            status=401,
        )

    async def handle_call(self, request):
        if request.content_length is not None and request.content_length > MAX_HTTP_BODY_BYTES:
            return web.json_response({"error": "Request body too large"}, status=413)
        try:
            raw_body = await request.read()
        except Exception:
            return web.json_response({"error": "Invalid request body"}, status=400)
        if len(raw_body) > MAX_HTTP_BODY_BYTES:
            return web.json_response({"error": "Request body too large"}, status=413)
        if not self._authorized(request, raw_body):
            return self._reject_unauthorized()
        try:
            body = json.loads(raw_body)
        except Exception:
            return self._signed_json_response(request, {"error": "Invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return self._signed_json_response(
                request, {"error": "JSON body must be an object"}, status=400
            )

        command = body.get("command")
        args = body.get("args", {})
        timeout = body.get("timeout", 60)

        if not isinstance(command, str) or command not in HTTP_COMMANDS:
            return self._signed_json_response(request, {"error": "Unsupported command"}, status=400)
        if command == "ghost_eval" and not self.allow_eval:
            return self._signed_json_response(
                request,
                {"error": "ghost_eval is disabled; restart with --allow-eval to enable it"},
                status=403,
            )
        if not isinstance(args, dict):
            return self._signed_json_response(request, {"error": "'args' must be an object"}, status=400)
        if isinstance(timeout, bool):
            return self._signed_json_response(request, {"error": "'timeout' must be a number"}, status=400)
        try:
            timeout = max(1, min(float(timeout), 300))
        except (TypeError, ValueError):
            return self._signed_json_response(request, {"error": "'timeout' must be a number"}, status=400)

        try:
            ok, value = await self.execute(command, args, timeout)
            if not ok:
                return self._signed_json_response(request, {"error": value}, status=502)
            return self._signed_json_response(request, {"result": value})
        except Exception as e:
            return self._signed_json_response(request, {"error": str(e)}, status=502)

    async def execute(self, command, args, timeout):
        """Run one authenticated command; returns (ok, result or error)."""
        if command in ROOM_COMMANDS:
            return True, await self.handle_room_command(command, args)
        if command == "ghost_pdf_read":
            return True, await self.handle_pdf_read(args, timeout)
        if command == "ghost_suggest" and self.room and isinstance(args, dict):
            ask = self.room.asks.get(args.get("reply_to"))
            if ask and not any(args.get(k) is not None for k in ("choice", "selector", "text", "rect")):
                # An answer goes where the question was asked.
                args = {**args, "anchor": ask.get("target"), "tab_id": args.get("tab_id") or self._tab_for(ask.get("url")),
                        "thread": ask.get("thread", ask["id"]), "href": ask.get("href", ""), "question": ask.get("question", "")}
        call_id = secrets.token_hex(6)
        await self._announce(command, args, call_id, "started")
        result = await self.send_command(command, args, timeout)
        if "error" in result:
            await self._announce(command, args, call_id, "failed", error=result["error"])
            return False, result["error"]
        value = result.get("result", result)
        await self._announce(command, args, call_id, "done", value=value)
        return True, value

    # ------------------------------------------------------------------
    # Multiplayer: publish local actors, draw remote ones
    # ------------------------------------------------------------------

    def _tab_for(self, url):
        key = self.room.room_page(url) if self.room else None
        return next((tab for tab, seen in self.tab_urls.items()
                     if key and self.room_accepted_tabs.get(tab) == {"url": seen, "room_url": key}), None)

    def _remember_tab(self, meta):
        if isinstance(meta, dict) and isinstance(meta.get("tab_id"), int) and isinstance(meta.get("url"), str):
            self.tab_urls[meta["tab_id"]] = meta["url"]

    async def _announce(self, command, args, call_id, status, value=None, error=None):
        """Tell the room what an actor did: an activity event, and its presence."""
        actor = args.get("actor_id") if isinstance(args, dict) else None
        if not self.room or not isinstance(actor, str) or command in QUIET_COMMANDS:
            return
        if command == "ghost_show" and not args.get("clear"):
            look = {k: args[k] for k in ("label", "color") if isinstance(args.get(k), str)}
            self.bot_looks[actor] = {**self.bot_looks.get(actor, {}), **look}
        await self.room.ensure_bot(actor, self.bot_looks.get(actor))
        tab_id = args.get("tab_id")
        url = (value or {}).get("url") if isinstance(value, dict) and command in {"ghost_navigate", "ghost_vacuum"} else None
        url = url or self.tab_urls.get(tab_id)
        accepted = self.room_accepted_tabs.get(tab_id, {})
        if accepted.get("url") != url or not self.room.is_shared(accepted.get("room_url")):
            return
        url = accepted["room_url"]
        anchor = value.get("anchor") if isinstance(value, dict) else None
        if command not in {"ghost_show", "ghost_suggest"}:
            await self.room.send({
                "action": "activity", "actor_id": actor, "url": url, "call_id": call_id,
                "op": command.removeprefix("ghost_"), "status": status, "target": anchor,
                **({"error": str(error)[:200]} if error else {}),
            })
        if status != "done":
            return
        if command == "ghost_suggest":
            await self.room.send({
                "action": "suggest", "actor_id": actor, "url": url, "id": value.get("id"),
                "title": args.get("title", ""), "body": args.get("body", ""), "target": anchor,
                **{k: args[k] for k in ("kind", "reply_to") if isinstance(args.get(k), str)},
            })
        elif command == "ghost_show":
            await self.room.send({
                "action": "presence", "actor_id": actor, "url": url,
                "status": "gone" if args.get("clear") else (args.get("status") or "working"),
                "target": anchor, "label": args.get("label") or self.bot_looks.get(actor, {}).get("label", ""),
                **({"ttl_ms": args["ttl_ms"]} if isinstance(args.get("ttl_ms"), int) else {}),
            })
        elif anchor:
            await self.room.send({
                "action": "presence", "actor_id": actor, "url": url, "status": "working",
                "target": anchor, "label": self.bot_looks.get(actor, {}).get("label", ""),
            })

    async def _push_shared_pages(self):
        if self.connected and self.extension_ws and self.room:
            with suppress(Exception):
                await self.extension_ws.send(json.dumps({
                    "type": "shared_pages", "pages": list(self.room.shared.values()), "me": self.room.me,
                }))

    async def _tabs_showing(self, url):
        key = self.room.room_page(url) if self.room else None
        if not key or not self.connected:
            return []
        listing = await self.send_command("ghost_tab_list", {}, timeout=10)
        tabs = (listing.get("result") or {}).get("tabs", [])
        return [t["id"] for t in tabs if isinstance(t, dict)
                and self.room_accepted_tabs.get(t.get("id")) == {"url": t.get("url"), "room_url": key}]

    def _show_args(self, presence, tab_id):
        actor = presence["actor"]
        status = presence.get("status", "working")
        args = {
            "tab_id": tab_id,
            "actor_id": actor["id"],
            "kind": actor.get("kind", "bot"),
            "label": presence.get("label") or actor.get("name") or actor["id"],
            "status": status if status in {"working", "done", "failed"} else "done",
            "ttl_ms": max(1000, min(int(presence.get("expires_at", 0)) - int(time.time() * 1000), 3600000)) if presence.get("expires_at") else 300000,
        }
        for key in ("color", "owner_color"):
            if actor.get(key):
                args[key] = actor[key]
        if presence.get("target"):
            args["anchor"] = presence["target"]
        if presence.get("pointer"):
            args["pointer"] = presence["pointer"]
        return args

    async def _draw(self, presence, tab_ids=None):
        try:
            tab_ids = tab_ids if tab_ids is not None else await self._tabs_showing(presence.get("url"))
            for tab_id in tab_ids:
                if presence.get("status") == "gone":
                    await self.send_command("ghost_show", {"tab_id": tab_id, "actor_id": presence["actor"]["id"], "clear": True, "room_only": True}, timeout=10)
                else:
                    await self.send_command("ghost_show", {**self._show_args(presence, tab_id), "room_only": True}, timeout=10)
        except Exception as exc:
            print(f"[room] could not draw {presence.get('actor', {}).get('id')}: {exc}")

    async def _draw_suggestion(self, suggestion, tab_ids=None, clear=False):
        try:
            tab_ids = tab_ids if tab_ids is not None else await self._tabs_showing(suggestion.get("url"))
            for tab_id in tab_ids:
                args = {"tab_id": tab_id, "id": suggestion["id"], "clear": clear, "room_only": True}
                if not clear:
                    args.update({
                        "actor": suggestion["actor"], "title": suggestion.get("title", ""),
                        "body": suggestion.get("body", ""), "anchor": suggestion.get("target"),
                        "kind": suggestion.get("kind", "edit"), "question": suggestion.get("question", ""),
                        "href": suggestion.get("href", ""), "thread": suggestion.get("thread", ""),
                        "reply_to": suggestion.get("reply_to", ""), "text": suggestion.get("text", ""),
                    })
                await self.send_command("ghost_suggestion", args, timeout=10)
        except Exception as exc:
            print(f"[room] could not draw suggestion {suggestion.get('id')}: {exc}")

    async def _draw_ask(self, ask, tab_ids=None, clear=False):
        """A human's question shows as a card until a bot answers it."""
        await self._draw_suggestion({
            "id": ask["id"], "url": ask.get("url"), "actor": ask["by"], "kind": "ask",
            "title": ask.get("question", ""), "body": "", "target": ask.get("target"), "href": ask.get("href", ""),
            "thread": ask.get("thread", ""), "text": ask.get("text", ""),
        }, tab_ids, clear)

    async def on_room_message(self, message):
        """Called by RoomLink for everything the room sends."""
        kind = message.get("type")
        if kind in {"joined", "page"}:
            await self._push_shared_pages()
            if kind == "page" and message.get("event") == "shared":
                for tab_id in await self._tabs_showing(message["page"]["url"]):
                    await self._extension_event({"type": "tab_ready", "tab_id": tab_id,
                                                 "url": self.room_accepted_tabs[tab_id]["url"]})
            if kind == "joined":
                for presence in list(self.room.presence.values()):
                    await self._draw(presence)
                for suggestion in list(self.room.suggestions.values()):
                    await self._draw_suggestion(suggestion)
                for ask in list(self.room.asks.values()):
                    await self._draw_ask(ask)
        elif kind == "presence" and not self.room._mine(message["actor"]):
            await self._draw(message)
        elif kind == "member" and message.get("event") == "left":
            for tab_id, url in list(self.tab_urls.items()):
                if self.room.is_shared(url):
                    with suppress(Exception):
                        await self.send_command("ghost_show", {"tab_id": tab_id, "actor_id": message["actor"]["id"], "clear": True, "room_only": True}, timeout=10)
        elif kind == "suggestion":
            if message.get("reply_to"):
                await self._draw_ask({"id": message["reply_to"], "url": message.get("url"), "by": message["actor"]}, clear=True)
            await self._draw_suggestion(message)
        elif kind == "ask":
            await self._draw_ask(message)
            print(f"[room] {message['by']['id']} asked: {message['question']}")
        elif kind == "resolved":
            await self._draw_suggestion(message, clear=True)
            print(f"[room] {message['by']['id']} {message['decision']}ed suggestion {message['id']} from {message['actor']['id']}")
        elif kind == "error":
            print(f"[room] {message.get('code')}: {message.get('message')}")

    async def _reel_story(self, msg):
        """A model writes the reel's booklet; the answer goes back to the reel page."""
        story_id = msg.get("id")
        if not isinstance(story_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,32}", story_id):
            return
        moments = msg.get("moments") if isinstance(msg.get("moments"), list) else []
        if self.story_busy:
            reply = {"id": story_id, "error": "A PDF is already being written; try again in a moment."}
        else:
            self.story_busy = True
            try:
                story = await asyncio.to_thread(write_story, moments, clean_language(msg.get("language")))
                reply = {"id": story_id, "story": story}
            except Exception as exc:  # the reel falls back to a plain layout
                reply = {"id": story_id, "error": str(exc)[:200] or "The model did not answer"}
            finally:
                self.story_busy = False
        with suppress(Exception):
            await self.send_command("ghost_reel_story", reply, timeout=10)

    async def _chat_call(self, command, args):
        """A chat worker's browser call: the same path, checks and room announcements as any actor."""
        if command == "ghost_eval":
            return False, "ghost_eval is never available to chat workers"
        return await self.execute(command, args, 45)

    async def _chat_retire(self, actor_id):
        if self.room:
            await self.room.retire_bot(actor_id)

    async def _chat_push(self, state):
        await self.send_command("ghost_chat_state", state, timeout=10)

    def _chat_room(self):
        if not self.room:
            return None
        look = lambda a: {"name": str(a.get("name") or a.get("id", ""))[:40], "color": a.get("color", ""),
                          "kind": a.get("kind", "human")}
        return {"name": self.room.room, "connected": self.room.connected, "me": look(self.room.me),
                "members": [look(a) for a in list(self.room.members.values())[:30]]}

    async def _extension_event(self, msg):
        """Things the extension tells the bridge without being asked."""
        if not self.room:
            return
        kind = msg.get("type")
        tab_id, url = msg.get("tab_id"), msg.get("url")
        if isinstance(tab_id, int) and isinstance(url, str):
            self.tab_urls[tab_id] = url
        if kind == "room_access" and isinstance(tab_id, int):
            remote = msg.get("room_url")
            if (msg.get("accepted") is True and isinstance(url, str) and isinstance(remote, str)
                    and self.room.bind_local(url, remote)
                    and (self.room.is_shared(remote) or remote in self.room.local_pages.values())):
                self.room_accepted_tabs[tab_id] = {"url": url, "room_url": remote}
            else:
                self.room_accepted_tabs.pop(tab_id, None)
        elif kind == "tab_ready" and self.room_accepted_tabs.get(tab_id, {}).get("url") == url:
            remote = self.room_accepted_tabs[tab_id]["room_url"]
            if not self.room.is_shared(remote):
                return
            for presence in self.room.presence_for(remote):
                await self._draw(presence, [tab_id])
            for suggestion in self.room.suggestions_for(remote):
                await self._draw_suggestion(suggestion, [tab_id])
            for ask in self.room.asks_for(remote):
                await self._draw_ask(ask, [tab_id])
        elif kind == "human" and self.room_accepted_tabs.get(tab_id, {}).get("url") == url and self.room.is_shared(self.room_accepted_tabs[tab_id]["room_url"]):
            await self.room.send({
                "action": "presence", "actor_id": self.room.me["id"], "url": self.room_accepted_tabs[tab_id]["room_url"],
                "status": msg.get("status") if msg.get("status") in {"working", "idle"} else "working",
                "target": msg.get("focus"), "pointer": msg.get("pointer"), "label": self.room.me.get("name", ""),
            })
        elif kind == "share" and isinstance(url, str):
            await self.room.send({"action": "share", "page": {"url": url, "room_url": msg.get("room_url"),
                                                            "share_link": msg.get("share_link") is True}})
        elif kind == "unshare" and isinstance(url, str):
            await self.room.send({"action": "unshare", "url": url})
        elif kind == "resolve" and msg.get("decision") in {"accept", "reject"} and isinstance(msg.get("id"), str):
            await self.room.send({"action": "resolve", "id": msg["id"], "decision": msg["decision"]})
        elif kind == "ask" and self.room_accepted_tabs.get(tab_id, {}).get("url") == url and self.room.is_shared(self.room_accepted_tabs[tab_id]["room_url"]) and isinstance(msg.get("question"), str):
            aid = secrets.token_hex(6)
            thread = msg.get("thread") if isinstance(msg.get("thread"), str) else None
            image = msg.get("image")
            if isinstance(image, str) and image.startswith("data:image/jpeg;base64,") and len(image) <= MAX_ASK_IMAGE_CHARS:
                self.ask_images[aid] = image
                self.ask_threads[thread or aid] = aid
            elif thread and self.ask_threads.get(thread) in self.ask_images:
                # A follow-up is about the same cropped area: the same picture, by reference.
                self.ask_images[aid] = self.ask_images[self.ask_threads[thread]]
            while len(self.ask_images) > MAX_ASK_IMAGES:
                self.ask_images.pop(next(iter(self.ask_images)))
            while len(self.ask_threads) > MAX_ASK_IMAGES:
                self.ask_threads.pop(next(iter(self.ask_threads)))
            ask = {
                "action": "ask", "id": aid, "url": self.room_accepted_tabs[tab_id]["room_url"], "question": msg["question"][:600],
                "text": str(msg.get("text", ""))[:4000], "target": msg.get("target"),
                **({"thread": msg["thread"]} if isinstance(msg.get("thread"), str) else {}),
                "links": msg.get("links") if isinstance(msg.get("links"), list) else [],
                **({"language": msg["language"]} if isinstance(msg.get("language"), str) else {}),
            }
            await self.room.send(ask)
            # The tab's agent answers its own person's question (see ChatHub.explain).
            if isinstance(tab_id, int) and not isinstance(tab_id, bool):
                await self.chat.explain({**ask, "image": aid in self.ask_images}, tab_id, url)

    async def handle_room_command(self, command, args):
        if not self.room:
            raise Exception("NO_ROOM: start the bridge with --room to use multiplayer")
        if command == "ghost_room":
            wait_ms = args.get("wait_ms") if isinstance(args, dict) else None
            if isinstance(wait_ms, int) and not isinstance(wait_ms, bool) and wait_ms > 0 and not self.room.asks:
                # Long-poll: a bot waiting for a question, or for a page to be shared or unshared.
                self.room.changed.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.room.changed.wait(), min(wait_ms, 50000) / 1000)
            mine = set(self.room.bots) | {self.room.me["id"]}
            return {
                "connected": self.room.connected, "room": self.room.room, "me": self.room.me,
                "members": list(self.room.members.values()), "shared_pages": list(self.room.shared.values()),
                "presence": list(self.room.presence.values()), "suggestions": list(self.room.suggestions.values()),
                "asks": [{**a, "image": True} if a.get("id") in self.ask_images else a for a in self.room.asks.values()],
                "decisions_on_your_suggestions": [d for d in self.room.decisions.values() if d["actor"]["id"] in mine],
            }
        if command == "room_approved_tabs":
            listing = await self.send_command("ghost_tab_list", {}, timeout=10)
            tabs = (listing.get("result") or {}).get("tabs", [])
            return {"tabs": [{**t, "room_url": self.room_accepted_tabs[t["id"]]["room_url"]}
                             for t in tabs if isinstance(t, dict) and isinstance(t.get("id"), int)
                             and self.room_accepted_tabs.get(t["id"], {}).get("url") == t.get("url")
                             and self.room.is_shared(self.room_accepted_tabs[t["id"]]["room_url"])]}
        if command == "room_read":
            tab_id = args.get("tab_id") if isinstance(args, dict) else None
            key = self.room.room_page(args.get("url")) if isinstance(args, dict) else None
            if not isinstance(tab_id, int) or isinstance(tab_id, bool) or not key or not self.room.is_shared(key):
                raise Exception("ROOM_ACCESS_DENIED: page is not shared")
            accepted = self.room_accepted_tabs.get(tab_id)
            if not accepted or accepted["room_url"] != key:
                raise Exception("ROOM_ACCESS_DENIED: this tab was not accepted")
            accepted_url = accepted["url"]
            listing = await self.send_command("ghost_tab_list", {}, timeout=10)
            tabs = (listing.get("result") or {}).get("tabs", [])
            if not any(t.get("id") == tab_id and t.get("url") == accepted_url for t in tabs if isinstance(t, dict)):
                raise Exception("ROOM_ACCESS_DENIED: tab moved away")
            result = await self.send_command("ghost_read", {
                "tab_id": tab_id, "actor_id": args.get("actor_id"),
                "max_chars": max(1, min(int(args.get("max_chars") or 60000), 60000)),
            }, timeout=45)
            page = result.get("result") or {}
            if "error" in result or page.get("url") != accepted_url or self.room_accepted_tabs.get(tab_id) != accepted:
                raise Exception("ROOM_ACCESS_DENIED: tab changed during read")
            return page
        if command in {"room_share", "room_unshare"}:
            url = args.get("url")
            if not isinstance(url, str):
                raise Exception("url is required")
            if command == "room_share":
                await self.room.send({"action": "share", "page": {"url": url,
                                                                "share_link": args.get("share_link") is True}})
            else:
                await self.room.send({"action": "unshare", "url": url})
            return {"sent": True}
        if command == "room_report":
            # A bot's long write-up of the session: it goes to this browser's reel.
            title, markdown = args.get("title"), args.get("markdown")
            if not isinstance(markdown, str) or not markdown.strip():
                raise Exception("markdown is required")
            return await self.send_command("ghost_reel_add", {
                "kind": "report", "title": str(title or "Session report")[:200],
                "markdown": markdown[:MAX_REPORT_CHARS], "by": str(args.get("by", ""))[:64],
            }, timeout=15)
        if command == "room_ask_image":
            image = self.ask_images.get(args.get("id")) if isinstance(args, dict) else None
            if not image:
                raise Exception("NOT_FOUND: no picture for that question on this bridge")
            return {"id": args["id"], "image": image}
        if command == "room_resolve":
            await self.room.send({"action": "resolve", "id": args.get("id"), "decision": args.get("decision")})
            return {"sent": True}
        raise Exception(f"Unknown room command {command}")

    async def handle_pdf_upload(self, request):
        """Accept one bounded upload created for a single ghost_pdf_read call."""
        token = request.match_info["token"]
        future = self.pending_pdf_uploads.pop(token, None)
        if future is None or future.done():
            return web.json_response({"error": "INVALID_UPLOAD_TOKEN"}, status=404)
        if request.headers.get("X-Ghost-PDF-Token") != token:
            future.set_exception(PdfReadError("INVALID_UPLOAD_TOKEN", "Upload token mismatch."))
            return web.json_response({"error": "INVALID_UPLOAD_TOKEN"}, status=403)

        try:
            declared_size = request.content_length
            if declared_size is not None and declared_size > MAX_PDF_BYTES:
                raise PdfReadError("PDF_TOO_LARGE", f"PDF exceeds the {MAX_PDF_BYTES} byte limit.")
            chunks = []
            total = 0
            async for chunk in request.content.iter_chunked(256 * 1024):
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise PdfReadError("PDF_TOO_LARGE", f"PDF exceeds the {MAX_PDF_BYTES} byte limit.")
                chunks.append(chunk)
            pdf_bytes = b"".join(chunks)
            if not pdf_bytes.lstrip().startswith(b"%PDF-"):
                raise PdfReadError("NOT_A_PDF", "Downloaded content does not have a PDF signature.")
            future.set_result(pdf_bytes)
            return web.json_response({"accepted": True, "bytes": total})
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            status = 413 if "PDF_TOO_LARGE" in str(exc) else 400
            return web.json_response({"error": str(exc)}, status=status)

    @staticmethod
    def _integer_arg(args, name, default, minimum, maximum):
        value = args.get(name, default)
        if isinstance(value, bool):
            raise PdfReadError("INVALID_INPUT", f"{name} must be an integer.")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise PdfReadError("INVALID_INPUT", f"{name} must be an integer.") from exc
        if not minimum <= value <= maximum:
            raise PdfReadError(
                "INVALID_INPUT", f"{name} must be between {minimum} and {maximum}."
            )
        return value

    async def handle_pdf_read(self, args, timeout):
        """Fetch through Chrome, receive bytes over loopback, then extract text locally."""
        if not isinstance(args, dict):
            raise PdfReadError("INVALID_INPUT", "args must be an object.")
        mode = args.get("mode", "auto")
        if mode not in {"auto", "text", "ocr"}:
            raise PdfReadError("INVALID_MODE", "mode must be one of: auto, text, ocr.")
        page_start = self._integer_arg(args, "page_start", 1, 1, 300)
        page_end = args.get("page_end")
        if page_end is not None:
            page_end = self._integer_arg(args, "page_end", None, page_start, 300)
        max_chars = self._integer_arg(
            args, "max_chars", DEFAULT_MAX_CHARS, 1, MAX_OUTPUT_CHARS
        )
        try:
            timeout = max(1, min(int(timeout), 300))
        except (TypeError, ValueError):
            timeout = 60

        token = secrets.token_urlsafe(32)
        upload_future = asyncio.get_running_loop().create_future()
        self.pending_pdf_uploads[token] = upload_future
        fetch_args = {
            "tab_id": args.get("tab_id"),
            "upload_url": f"http://127.0.0.1:{self.port + 1}/pdf-upload/{token}",
            "upload_token": token,
            "max_bytes": MAX_PDF_BYTES,
        }

        try:
            response = await self.send_command("ghost_pdf_fetch", fetch_args, timeout)
            if "error" in response:
                raise PdfReadError("PDF_FETCH_FAILED", response["error"])
            fetch_metadata = response.get("result", response)
            pdf_bytes = await asyncio.wait_for(upload_future, timeout=timeout)
            extracted = await asyncio.to_thread(
                extract_pdf,
                pdf_bytes,
                page_start=page_start,
                page_end=page_end,
                mode=mode,
                max_chars=max_chars,
                password=args.get("password"),
            )
            return {
                "url": fetch_metadata.get("url"),
                "title": fetch_metadata.get("title"),
                "mime_type": fetch_metadata.get("content_type") or "application/pdf",
                "bytes": len(pdf_bytes),
                "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                **extracted,
            }
        except asyncio.TimeoutError as exc:
            raise PdfReadError("PDF_FETCH_TIMEOUT", "Timed out receiving PDF bytes from Chrome.") from exc
        finally:
            self.pending_pdf_uploads.pop(token, None)
            if not upload_future.done():
                upload_future.cancel()
            else:
                with suppress(asyncio.CancelledError, Exception):
                    upload_future.exception()

    async def handle_status(self, request):
        if not self._authorized(request):
            return self._reject_unauthorized()
        return self._signed_json_response(request, {
            "connected": self.connected,
            "port": self.port,
            "pending_commands": len(self.pending),
        })

    async def handle_health(self, request):
        return web.json_response({"ok": True, "bridge": "ghost"})

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def serve_extension(self, port=None):
        """The WebSocket server the extension connects to (loopback only)."""
        return await ws_serve(
            self.ws_handler,
            "127.0.0.1",
            self.port if port is None else port,
            max_size=MAX_WS_MESSAGE_BYTES,
            # Serve the WS on /ghost-bridge path
        )

    async def run(self):
        # WebSocket server for the extension
        ws_server = await self.serve_extension()

        # HTTP server for agent commands
        app = web.Application(client_max_size=MAX_HTTP_BODY_BYTES)
        app.router.add_get("/challenge", self.handle_challenge)
        app.router.add_post("/call", self.handle_call)
        app.router.add_post("/pdf-upload/{token}", self.handle_pdf_upload)
        app.router.add_get("/status", self.handle_status)
        app.router.add_get("/health", self.handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        http_site = web.TCPSite(runner, "127.0.0.1", self.port + 1)
        await http_site.start()

        print(f"[bridge] WebSocket server on ws://127.0.0.1:{self.port}/ghost-bridge")
        print(f"[bridge] HTTP API on http://127.0.0.1:{self.port + 1}/call")
        print(f"[bridge] Pair the extension with the token stored at {token_path()}")
        print(f"[bridge] Waiting for Chrome extension...")
        if self.room:
            self.room.on_message = self.on_room_message
            self.room.start()
            print(f"[bridge] Joining room {self.room.room} at {self.room.url} as {self.room.me['id']}")

        # Wait forever
        stop = asyncio.get_event_loop().create_future()

        def shutdown():
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_event_loop().add_signal_handler(sig, shutdown)
            except NotImplementedError:
                signal.signal(sig, lambda *_: shutdown())

        try:
            await stop
        finally:
            if self.room:
                await self.room.stop()
            ws_server.close()
            await ws_server.wait_closed()
            await runner.cleanup()
            print("\n[bridge] Shut down.")


def main():
    parser = argparse.ArgumentParser(description="Ghost Bridge Server")
    parser.add_argument("--port", type=int, default=9377, help="WebSocket port (HTTP = port+1)")
    parser.add_argument(
        "--allow-eval",
        action="store_true",
        help="Explicitly enable arbitrary page JavaScript through ghost_eval",
    )
    args = parser.parse_args()

    try:
        server = BridgeServer(port=args.port, allow_eval=args.allow_eval)
        asyncio.run(server.run())
    except BridgeAuthError as exc:
        raise SystemExit(f"Bridge authentication error: {exc}") from exc


if __name__ == "__main__":
    main()
