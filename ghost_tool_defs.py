"""Canonical tools shared by the CLI and Hermes plugin adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


def _schema(properties=None, required=None):
    value = {"type": "object", "properties": properties or {}}
    if required:
        value["required"] = required
    return value


_SHOW_SCHEMA = _schema({
    "actor_id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,64}$", "description": "Who is shown; defaults to the configured actor"},
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
})

_SUGGEST_SCHEMA = _schema({
    "tab_id": {"type": "integer"},
    "choice": {"type": "integer"},
    "selector": {"type": "string"},
    "text": {"type": "string", "maxLength": 500},
    "rect": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}}, "required": ["x", "y", "w", "h"]},
    "title": {"type": "string", "maxLength": 120},
    "body": {"type": "string", "maxLength": 600},
    "id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,64}$"},
    "kind": {"type": "string", "enum": ["edit", "note"]},
    "reply_to": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,64}$"},
}, ["title"])

TOOLS = (
    ToolDef("ghost_status", "Check the active browser connection and page.", _schema()),
    ToolDef("ghost_tab_list", "List browser tabs.", _schema()),
    ToolDef("ghost_tab_open", "Open a browser tab.", _schema({"url": {"type": "string"}})),
    ToolDef("ghost_tab_switch", "Switch tabs by id or list index.", _schema({"tab_id": {"type": "integer"}, "tab_index": {"type": "integer"}})),
    ToolDef("ghost_tab_close", "Close a browser tab by id or list index.", _schema({"tab_id": {"type": "integer"}, "tab_index": {"type": "integer"}})),
    ToolDef("ghost_navigate", "Navigate the active browser tab. Set reload=true to load a fresh document when the URL is already current but its content is stale; normal human-viewing guards still apply.", _schema({"url": {"type": "string"}, "tab_id": {"type": "integer"}, "reload": {"type": "boolean"}}, ["url"])),
    ToolDef("ghost_vacuum", "Navigate and return numbered interactive elements. Set reload=true for a fresh same-URL document when a site's client-side navigation left stale content.", _schema({"tab_id": {"type": "integer"}, "url": {"type": "string"}, "reload": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "selector": {"type": "string"}}, ["url"])),
    ToolDef("ghost_read", "Read bounded text from the current page.", _schema({"tab_id": {"type": "integer"}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 100000}, "selector": {"type": "string"}})),
    ToolDef("ghost_records", "Read a complete bounded collection of visible noneditable records for destination verification. Holds on incomplete or ambiguous evidence.", _schema({
        "tab_id": {"type": "integer"}, "actor_id": {"type": "string"},
        "spec": {"type": "object", "additionalProperties": False,
                 "required": ["collection", "row", "id", "identity", "fields", "total_count"],
                 "properties": {**{key: {"type": "string", "minLength": 1, "maxLength": 500}
                                    for key in ("collection", "row", "id", "identity", "total_count")},
                                "fields": {"type": "object", "minProperties": 1, "maxProperties": 32,
                                           "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 500}}}}}, ["spec"])),
    ToolDef("ghost_pdf_read", "Read bounded, page-indexed text from the PDF open in Chrome.", _schema({"tab_id": {"type": "integer"}, "page_start": {"type": "integer", "minimum": 1, "maximum": 300}, "page_end": {"type": "integer", "minimum": 1, "maximum": 300}, "mode": {"type": "string", "enum": ["auto", "text", "ocr"]}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 1000000}, "password": {"type": "string"}})),
    ToolDef("ghost_click", "Click a numbered element or CSS selector.", _schema({"tab_id": {"type": "integer"}, "choice": {"type": "integer"}, "selector": {"type": "string"}, "wait": {"type": "string"}})),
    ToolDef("ghost_fill", "Fill a numbered input or CSS selector.", _schema({"tab_id": {"type": "integer"}, "choice": {"type": "integer"}, "selector": {"type": "string"}, "value": {"type": "string"}}, ["value"])),
    ToolDef("ghost_key", "Press a key or type text. With choice or selector the text goes into that element without taking focus.", _schema({"tab_id": {"type": "integer"}, "key": {"type": "string"}, "text": {"type": "string"}, "choice": {"type": "integer"}, "selector": {"type": "string"}})),
    ToolDef("ghost_eval", "Run a JavaScript function in the current page and return its value.", _schema({"tab_id": {"type": "integer"}, "script": {"type": "string"}}, ["script"])),
    ToolDef("ghost_screenshot", "Capture the visible browser page.", _schema({"tab_id": {"type": "integer"}, "format": {"type": "string", "enum": ["png", "jpeg"]}, "quality": {"type": "integer", "minimum": 1, "maximum": 100}})),
    ToolDef("ghost_sheet_append", "Add one row at the bottom of a Google Sheet tab, with the person's own Google session: it pastes the row after the last filled row, skips it when unique is already in the tab, and reads the row back.", _schema({"tab_id": {"type": "integer"}, "sheet": {"type": "string"}, "row": {"type": "array", "items": {"type": "string"}, "maxItems": 26}, "unique": {"type": "string"}, "tab_name": {"type": "string"}}, ["sheet", "row"])),
    ToolDef("ghost_scroll", "Scroll the current page.", _schema({"tab_id": {"type": "integer"}, "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]}, "amount": {"type": "integer"}})),
    ToolDef("ghost_show", "Show what you are working on: ring a numbered element, selector, text, or page rect with your mote and label. Shows only; it never edits the page.", _SHOW_SCHEMA),
    ToolDef("ghost_suggest", "Put a card next to a numbered element, selector, text or page rect. kind=edit (default) proposes a change people accept or reject; apply it only after ghost_room shows it accepted. kind=note just explains. reply_to answers a question from ghost_room's asks, on the text it was asked about.", _SUGGEST_SCHEMA),
    ToolDef("ghost_room", "See the multiplayer room: who is here, which pages are shared, where everyone is working, questions people asked about selected text (asks), and decisions on your suggestions. wait_ms waits up to 50 s for a question or a change in shared pages.", _schema({"wait_ms": {"type": "integer", "minimum": 0, "maximum": 50000}})),
    ToolDef("ghost_wait", "Wait for a selector or a bounded number of milliseconds.", _schema({"tab_id": {"type": "integer"}, "selector": {"type": "string"}, "ms": {"type": "integer", "minimum": 0, "maximum": 30000}, "timeout": {"type": "integer", "minimum": 1, "maximum": 30000}})),
)

TOOL_NAMES = frozenset(tool.name for tool in TOOLS)


def get_ghost_tools() -> list[ToolDef]:
    return list(TOOLS)
