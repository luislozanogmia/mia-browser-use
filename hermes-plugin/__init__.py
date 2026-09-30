"""Hermes Agent adapter for Ghost browser tools."""

from __future__ import annotations

import json

from .client import BrowserClient
from .page_context import find_active_page, page_note


def _schema(properties=None, required=None):
    value = {"type": "object", "properties": properties or {}}
    if required:
        value["required"] = required
    return value


LOCAL_PLATFORMS = frozenset({"cli", "tui", "desktop"})

_SHOW_SCHEMA = _schema({
    "actor_id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,64}$"},
    "label": {"type": "string", "maxLength": 80},
    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{3,8}$"},
    "owner_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{3,8}$"},
    "kind": {"type": "string", "enum": ["bot", "human"]},
    "status": {"type": "string", "enum": ["working", "done", "failed"]},
    "choice": {"type": "integer"},
    "selector": {"type": "string"},
    "text": {"type": "string", "maxLength": 500},
    "rect": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}}, "required": ["x", "y", "w", "h"]},
    "ttl_ms": {"type": "integer", "minimum": 1000, "maximum": 3600000},
    "clear": {"type": "boolean"},
    "tab_id": {"type": "integer"},
}, ["actor_id"])

TOOL_SPECS = (
    ("ghost_status", "Check the selected browser connection and page.", _schema()),
    ("ghost_tab_list", "List browser tabs.", _schema()),
    ("ghost_tab_open", "Open a browser tab.", _schema({"url": {"type": "string"}})),
    ("ghost_tab_switch", "Switch tabs by id or list index.", _schema({"tab_id": {"type": "integer"}, "tab_index": {"type": "integer"}})),
    ("ghost_tab_close", "Close a browser tab by id or list index.", _schema({"tab_id": {"type": "integer"}, "tab_index": {"type": "integer"}})),
    ("ghost_navigate", "Navigate the active browser tab.", _schema({"url": {"type": "string"}, "tab_id": {"type": "integer"}}, ["url"])),
    ("ghost_vacuum", "Navigate and return numbered interactive elements.", _schema({"url": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "selector": {"type": "string"}}, ["url"])),
    ("ghost_read", "Read bounded text from the current page.", _schema({"max_chars": {"type": "integer", "minimum": 1, "maximum": 100000}, "selector": {"type": "string"}})),
    ("ghost_pdf_read", "Read bounded, page-indexed text from the PDF open in Chrome.", _schema({"page_start": {"type": "integer", "minimum": 1, "maximum": 300}, "page_end": {"type": "integer", "minimum": 1, "maximum": 300}, "mode": {"type": "string", "enum": ["auto", "text", "ocr"]}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 1000000}, "password": {"type": "string"}})),
    ("ghost_click", "Click a numbered element or CSS selector.", _schema({"choice": {"type": "integer"}, "selector": {"type": "string"}, "wait": {"type": "string"}})),
    ("ghost_fill", "Fill a numbered input or CSS selector.", _schema({"choice": {"type": "integer"}, "selector": {"type": "string"}, "value": {"type": "string"}}, ["value"])),
    ("ghost_key", "Press a key or type text in the focused element.", _schema({"key": {"type": "string"}, "text": {"type": "string"}})),
    ("ghost_eval", "Run a JavaScript function in the current page and return its value.", _schema({"script": {"type": "string"}}, ["script"])),
    ("ghost_screenshot", "Capture the visible browser page.", _schema({"format": {"type": "string", "enum": ["png", "jpeg"]}, "quality": {"type": "integer", "minimum": 1, "maximum": 100}})),
    ("ghost_scroll", "Scroll the current page.", _schema({"direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]}, "amount": {"type": "integer"}})),
    ("ghost_show", "Show what you are working on: ring a numbered element, selector, text, or page rect with your mote and label. Shows only; it never edits the page.", _SHOW_SCHEMA),
    ("ghost_wait", "Wait for a selector or a bounded delay.", _schema({"selector": {"type": "string"}, "ms": {"type": "integer", "minimum": 0, "maximum": 30000}, "timeout": {"type": "integer", "minimum": 1, "maximum": 30000}})),
)


def register(ctx) -> None:
    backend = str(ctx.get_config("backend", "auto"))
    chrome_port = int(ctx.get_config("chrome_port", 9378))
    allow_eval = bool(ctx.get_config("allow_eval", False))

    def handler(name):
        def run(arguments, **_kwargs):
            try:
                client = BrowserClient(backend, chrome_port, allow_eval)
                result = client.call(name, arguments or {})
                return json.dumps({"ok": True, "backend": client.active_backend, "result": result}, ensure_ascii=False)
            except Exception as exc:
                return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return run

    def page_context(platform="", **_kwargs):
        # Messaging gateways can carry other people's messages; share the page only locally.
        if platform not in LOCAL_PLATFORMS:
            return None
        try:
            client = BrowserClient(backend, chrome_port)
            status = client.connect()
            page = find_active_page(status, client.transport.call)
        except Exception:
            return None
        return {"context": page_note(page)} if page else None

    if ctx.get_config("page_context", True) and hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", page_context)

    for name, description, parameters in TOOL_SPECS:
        ctx.register_tool(
            name=name,
            toolset="ghost",
            schema={"name": name, "description": description, "parameters": parameters},
            handler=handler(name),
            emoji="👻",
        )
