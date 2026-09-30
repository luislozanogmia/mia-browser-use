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

TOOLS = (
    ToolDef("ghost_status", "Check the active browser connection and page.", _schema()),
    ToolDef("ghost_tab_list", "List browser tabs.", _schema()),
    ToolDef("ghost_tab_open", "Open a browser tab.", _schema({"url": {"type": "string"}})),
    ToolDef("ghost_tab_switch", "Switch tabs by id or list index.", _schema({"tab_id": {"type": "integer"}, "tab_index": {"type": "integer"}})),
    ToolDef("ghost_tab_close", "Close a browser tab by id or list index.", _schema({"tab_id": {"type": "integer"}, "tab_index": {"type": "integer"}})),
    ToolDef("ghost_navigate", "Navigate the active browser tab.", _schema({"url": {"type": "string"}, "tab_id": {"type": "integer"}}, ["url"])),
    ToolDef("ghost_vacuum", "Navigate and return numbered interactive elements.", _schema({"url": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "selector": {"type": "string"}}, ["url"])),
    ToolDef("ghost_read", "Read bounded text from the current page.", _schema({"max_chars": {"type": "integer", "minimum": 1, "maximum": 100000}, "selector": {"type": "string"}})),
    ToolDef("ghost_pdf_read", "Read bounded, page-indexed text from the PDF open in Chrome.", _schema({"page_start": {"type": "integer", "minimum": 1, "maximum": 300}, "page_end": {"type": "integer", "minimum": 1, "maximum": 300}, "mode": {"type": "string", "enum": ["auto", "text", "ocr"]}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 1000000}, "password": {"type": "string"}})),
    ToolDef("ghost_click", "Click a numbered element or CSS selector.", _schema({"choice": {"type": "integer"}, "selector": {"type": "string"}, "wait": {"type": "string"}})),
    ToolDef("ghost_fill", "Fill a numbered input or CSS selector.", _schema({"choice": {"type": "integer"}, "selector": {"type": "string"}, "value": {"type": "string"}}, ["value"])),
    ToolDef("ghost_key", "Press a key or type text in the focused element.", _schema({"key": {"type": "string"}, "text": {"type": "string"}})),
    ToolDef("ghost_eval", "Run a JavaScript function in the current page and return its value.", _schema({"script": {"type": "string"}}, ["script"])),
    ToolDef("ghost_screenshot", "Capture the visible browser page.", _schema({"format": {"type": "string", "enum": ["png", "jpeg"]}, "quality": {"type": "integer", "minimum": 1, "maximum": 100}})),
    ToolDef("ghost_scroll", "Scroll the current page.", _schema({"direction": {"type": "string", "enum": ["up", "down", "top", "bottom"]}, "amount": {"type": "integer"}})),
    ToolDef("ghost_show", "Show what you are working on: ring a numbered element, selector, text, or page rect with your mote and label. Shows only; it never edits the page.", _SHOW_SCHEMA),
    ToolDef("ghost_wait", "Wait for a selector or a bounded number of milliseconds.", _schema({"selector": {"type": "string"}, "ms": {"type": "integer", "minimum": 0, "maximum": 30000}, "timeout": {"type": "integer", "minimum": 1, "maximum": 30000}})),
)

TOOL_NAMES = frozenset(tool.name for tool in TOOLS)


def get_ghost_tools() -> list[ToolDef]:
    return list(TOOLS)
