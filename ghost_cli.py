"""Small command-line client for Ghost's supported browser transports."""

from __future__ import annotations

import argparse
import json
import os
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
}


class BrowserClient:
    def __init__(self, backend: str = "auto", allow_eval: bool = False):
        self.backend = backend
        self.allow_eval = allow_eval
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
            raise RuntimeError(status.get("error", "Chrome extension bridge is unavailable"))
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
    parser = argparse.ArgumentParser(prog="ghost-cli", description="Control Chrome or Hermes Desktop")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    status = sub.add_parser("status", help="Show the selected browser connection")
    status.add_argument("--backend", choices=("auto", "chrome", "hermes"), default=os.getenv("GHOST_BROWSER_BACKEND", "auto"))

    call = sub.add_parser("call", help="Call one supported Ghost command")
    call.add_argument("command", choices=sorted(TOOL_NAMES))
    call.add_argument("--args", type=_json_object, default={})
    call.add_argument("--backend", choices=("auto", "chrome", "hermes"), default=os.getenv("GHOST_BROWSER_BACKEND", "auto"))
    call.add_argument("--allow-eval", action="store_true", help="Explicitly allow ghost_eval for this call")

    context = sub.add_parser("context", help="Print a note about the page the user has open, for prompt hooks")
    context.add_argument("--backend", choices=("auto", "chrome", "hermes"), default=os.getenv("GHOST_BROWSER_BACKEND", "auto"))
    context.add_argument("--format", choices=("text", "hook"), default="text", help="hook prints Claude Code/Codex UserPromptSubmit JSON")

    token = sub.add_parser("bridge-token", help="Create and print the Chrome extension pairing token")
    token.add_argument("--path-only", action="store_true", help="Print only the token file path")

    serve = sub.add_parser("serve", help="Run the Chrome extension bridge")
    serve.add_argument("--port", type=int, default=9377)
    serve.add_argument("--allow-eval", action="store_true", help="Explicitly enable ghost_eval in the bridge")
    return parser


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
        if args.subcommand == "serve":
            import asyncio
            from bridge_server import BridgeServer

            asyncio.run(BridgeServer(port=args.port, allow_eval=args.allow_eval).run())
            return
        if args.subcommand == "context":
            # Prompt hooks must never block the user's message, so failures print nothing.
            print_context(page_context_note(BrowserClient(args.backend)), args.format)
            return

        client = BrowserClient(args.backend, allow_eval=getattr(args, "allow_eval", False))
        if args.subcommand == "status":
            result = client.connect()
        else:
            result = client.call(args.command, args.args)
        print(json.dumps({"backend": client.backend, "result": result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
