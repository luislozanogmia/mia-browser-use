"""Small command-line client for Ghost's supported browser transports."""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

from bridge_auth import load_bridge_token, token_path
from bridge_transport import BridgeTransport
from ghost_tool_defs import TOOL_NAMES
from in_app_browser_transport import InAppBrowserTransport
from page_context import find_active_page, page_note


HERMES_COMMANDS = {
    "ghost_status": "status",
    "ghost_tab_list": "tab_list",
    "ghost_tab_open": "tab_open",
    "ghost_tab_switch": "tab_switch",
    "ghost_tab_close": "tab_close",
    "ghost_navigate": "navigate",
    "ghost_vacuum": "vacuum",
    "ghost_read": "read",
    "ghost_click": "click",
    "ghost_fill": "fill",
    "ghost_key": "key",
    "ghost_eval": "eval",
    "ghost_screenshot": "screenshot",
    "ghost_scroll": "scroll",
    "ghost_wait": "wait",
    "ghost_show": "show",
    "ghost_suggest": "suggest",
}


ACTOR_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def validate_actor(actor: str | None) -> str | None:
    if actor in (None, ""):
        return None
    if not ACTOR_RE.match(actor):
        raise ValueError("actor must be 1-64 of A-Z a-z 0-9 _ . : -")
    return actor


class BrowserClient:
    def __init__(self, backend: str = "auto", allow_eval: bool = False, actor: str | None = None):
        self.backend = backend
        self.allow_eval = allow_eval
        # Who is acting. Set by whoever runs the bot, never chosen by the model.
        self.actor = validate_actor(actor)
        self.transport: Any = None

    def connect(self):
        if self.backend in {"auto", "hermes"}:
            hermes = InAppBrowserTransport()
            status = hermes.status()
            if status.get("connected"):
                self.backend = "hermes"
                self.transport = hermes
                return status
            if self.backend == "hermes":
                raise RuntimeError(status.get("error", "Hermes Desktop browser is unavailable"))

        chrome = BridgeTransport()
        status = chrome.status()
        if not status.get("connected"):
            raise RuntimeError(status.get("error", "Mia is running, but the Chrome extension isn't connected. Open Chrome with the Mia extension."))
        self.backend = "chrome"
        self.transport = chrome
        return status

    def call(self, command: str, args: dict[str, Any]):
        if command not in TOOL_NAMES:
            raise ValueError(f"Unsupported command: {command}")
        if command == "ghost_eval" and not self.allow_eval:
            raise ValueError("ghost_eval is disabled; pass --allow-eval to opt in")
        if command == "ghost_pdf_read" and self.backend == "hermes":
            raise ValueError("ghost_pdf_read is available through the Chrome extension only")
        if command == "ghost_pdf_read" and self.backend == "auto":
            self.backend = "chrome"
        if self.transport is None:
            status = self.connect()
        else:
            status = None
        if command == "ghost_status":
            return status if status is not None else self.transport.status()
        if self.actor:
            args = {**args, "actor_id": self.actor}
        if self.backend == "hermes":
            mapped = HERMES_COMMANDS.get(command)
            if not mapped:
                raise ValueError(f"{command} is not available in Hermes Desktop")
            return self.transport.call(mapped, args)
        return self.transport.call(command, args)


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("arguments must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mia-browser-use", description="Control Chrome or Hermes Desktop")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    status = sub.add_parser("status", help="Show the selected browser connection")
    status.add_argument("--backend", choices=("auto", "chrome", "hermes"), default=os.getenv("GHOST_BROWSER_BACKEND", "auto"))

    call = sub.add_parser("call", help="Call one supported Ghost command")
    call.add_argument("command", choices=sorted(TOOL_NAMES))
    call.add_argument("--args", type=_json_object, default={})
    call.add_argument("--backend", choices=("auto", "chrome", "hermes"), default=os.getenv("GHOST_BROWSER_BACKEND", "auto"))
    call.add_argument("--allow-eval", action="store_true", help="Explicitly allow ghost_eval for this call")
    call.add_argument("--actor", default=os.getenv("GHOST_ACTOR_ID"), help="Act as this bot or human (multiplayer); calls then need tab_id and never change the human's view")

    context = sub.add_parser("context", help="Print a note about the page the user has open, for prompt hooks")
    context.add_argument("--backend", choices=("auto", "chrome", "hermes"), default=os.getenv("GHOST_BROWSER_BACKEND", "auto"))
    context.add_argument("--format", choices=("text", "hook"), default="text", help="hook prints Claude Code/Codex UserPromptSubmit JSON")

    token = sub.add_parser("bridge-token", help="Create and print the Chrome extension pairing token")
    token.add_argument("--path-only", action="store_true", help="Print only the token file path")

    sub.add_parser("pair-chrome", help="Let the Chrome extension pair itself (installs a native messaging host)")
    sub.add_parser("up", help="Run and watch everything Ghost needs (bridge and local room); Chrome starts this by itself")

    serve = sub.add_parser("serve", help="Run the Chrome extension bridge")
    serve.add_argument("--port", type=int, default=9377)
    serve.add_argument("--allow-eval", action="store_true", help="Explicitly enable ghost_eval in the bridge")
    serve.add_argument("--room", help="Join this multiplayer room (key from GHOST_ROOM_KEY or ~/.ghost/rooms/<room>.key)")
    serve.add_argument("--room-url", default=os.getenv("GHOST_ROOM_URL", "ws://127.0.0.1:9390"), help="Room relay URL (wss:// unless on this machine)")
    serve.add_argument("--me", default=os.getenv("GHOST_ME"), help="Your id in the room, e.g. luis")
    serve.add_argument("--name", help="Your display name in the room")
    serve.add_argument("--color", help="Your player color, e.g. #3b82f6")

    room = sub.add_parser("room", help="Multiplayer rooms")
    room_sub = room.add_subparsers(dest="room_command", required=True)
    host = room_sub.add_parser("serve", help="Run a room relay on this machine")
    host.add_argument("--room", required=True)
    host.add_argument("--host", default="127.0.0.1", help="Listen address (keep 127.0.0.1 unless behind TLS)")
    host.add_argument("--port", type=int, default=9390)
    for name, help_text in (("status", "Show the room: members, shared pages, presence, suggestions"),):
        room_sub.add_parser(name, help=help_text)
    share = room_sub.add_parser("share", help="Share a page with the room")
    share.add_argument("url")
    unshare = room_sub.add_parser("unshare", help="Stop sharing a page")
    unshare.add_argument("url")
    answer = room_sub.add_parser("answer", help="Run a bot that answers questions people ask about selected text")
    answer.add_argument("--as", dest="actor", default="claude", help="The bot's id in the room")
    answer.add_argument("--model", default=os.getenv("GHOST_ASK_MODEL", "sonnet"), help="Model for the answers (claude --model)")
    answer.add_argument("--claude", default=os.getenv("GHOST_CLAUDE_BIN", "claude"), help="Path to the claude CLI")
    answer.add_argument("--color", default="#d97706")
    resolve = room_sub.add_parser("resolve", help="Accept or reject a suggestion")
    resolve.add_argument("id")
    resolve.add_argument("decision", choices=("accept", "reject"))
    return parser


def run_room_command(args) -> Any:
    if args.room_command == "serve":
        import asyncio
        from ghost_room import RoomHub, new_room_key
        from room_link import load_room_key, save_room_key

        key = load_room_key(args.room) or new_room_key()
        path = save_room_key(args.room, key)
        hub = RoomHub()
        hub.store.create_room(args.room, key)

        async def run():
            from ghost_room import serve_room

            server = await serve_room(hub, args.host, args.port)
            print(f"[room] {args.room} on ws://{args.host}:{args.port}  key file: {path}")
            await server.serve_forever()

        asyncio.run(run())
        return None
    if args.room_command == "answer":
        from ask_bot import AskBot, claude_answer, claude_report

        validate_actor(args.actor)
        bot = AskBot(args.actor, claude_answer(args.model, args.claude, room_only=True), color=args.color,
                     report=claude_report(args.model, args.claude),
                     label=f"{args.actor.capitalize()} · {args.model}")
        bot.run_forever()
        return None
    bridge = BridgeTransport()
    command = {"status": "ghost_room", "share": "room_share", "unshare": "room_unshare", "resolve": "room_resolve"}[args.room_command]
    params = {k: getattr(args, k) for k in ("url", "id", "decision") if hasattr(args, k)}
    return bridge.call(command, params)


def page_context_note(client: BrowserClient) -> str:
    """Return the note for the user's open web page, or "" when there is none."""
    try:
        status = client.connect()
        page = find_active_page(status, client.call)
    except Exception:
        return ""
    return page_note(page) if page else ""


def print_context(note: str, fmt: str) -> None:
    if not note:
        return
    if fmt == "hook":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}, ensure_ascii=False))
    else:
        print(note)


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.subcommand == "bridge-token":
            value = load_bridge_token(create=True)
            print(token_path() if args.path_only else value)
            return
        if args.subcommand == "pair-chrome":
            from native_host import install_native_host

            print(json.dumps(install_native_host(), indent=2))
            return
        if args.subcommand == "up":
            import ghost_up

            ghost_up.main()
            return
        if args.subcommand == "serve":
            import asyncio
            from bridge_server import BridgeServer

            room = None
            if args.room:
                from room_link import RoomLink, load_room_key

                key = load_room_key(args.room)
                if not key:
                    raise SystemExit(f"No key for room {args.room}; set GHOST_ROOM_KEY or put it in ~/.ghost/rooms/{args.room}.key")
                me_id = validate_actor(args.me)
                if not me_id:
                    raise SystemExit("--me is required with --room")
                me = {"id": me_id, "kind": "human", "name": args.name or me_id}
                if args.color:
                    me["color"] = args.color
                room = RoomLink(args.room_url, args.room, key, me, on_message=None)
            # The extension restarts the bridge with these settings when it finds it down.
            from native_host import save_bridge_config

            save_bridge_config(args.port, args.room, args.room_url, args.me, args.name, args.color)
            asyncio.run(BridgeServer(port=args.port, allow_eval=args.allow_eval, room=room).run())
            return
        if args.subcommand == "room":
            result = run_room_command(args)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.subcommand == "context":
            # Prompt hooks must never block the user's message, so failures print nothing.
            print_context(page_context_note(BrowserClient(args.backend)), args.format)
            return

        client = BrowserClient(args.backend, allow_eval=getattr(args, "allow_eval", False), actor=getattr(args, "actor", None))
        if args.subcommand == "status":
            result = client.connect()
        else:
            result = client.call(args.command, args.args)
        print(json.dumps({"backend": client.backend, "result": result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
