"""Play Automations: fixed scripts Mia Browser runs step by step, with no AI.

Mia writes them, usually by doing the job once in a tab while every click and
typed value is recorded (see ghost_chat.ChatHub). Running one later needs no
model: the bridge replays the steps. AI Workflows are the other kind of work,
where Mia's bots decide each step as they go.

A script is a list of steps:
  {"do": "open", "url": "https://..."}                      always the first step
  {"do": "click", "css": "...", "text": "Export"}           css, text, or both
  {"do": "type", "css"/"text": ..., "value": "hi {{total}}"}
  {"do": "key", "key": "Enter", "css"/"text": optional}
  {"do": "copy", "css"/"text": ..., "as": "total"}          keeps the text for {{total}}
  {"do": "wait", "ms": 2000} or {"do": "wait", "css": "..."}
  {"do": "scroll", "direction": "down" | "up" | "top" | "bottom"}

Kept in ~/.ghost/automations.json, readable only by the person.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

MAX_AUTOMATIONS = 50
MAX_STEPS = 60
NAME_CHARS = 60
ABOUT_CHARS = 300
VALUE_CHARS = 2000
CATCH_UP = timedelta(minutes=30)  # a scheduled run missed by less than this still runs
ACTIONS = {"open", "click", "type", "key", "copy", "wait", "scroll"}
TARGETED = {"click", "type", "copy"}
VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,30})\s*\}\}")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
AUTOMATION_ID = re.compile(r"^auto-[a-z0-9]{1,20}$")


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def clean_step(step: Any) -> dict | None:
    """One step as stored, or None when it's malformed."""
    if not isinstance(step, dict) or step.get("do") not in ACTIONS:
        return None
    do = step["do"]
    out: dict = {"do": do}
    css, text = _text(step.get("css"), 300), _text(step.get("text"), 120)
    if css:
        out["css"] = css
    if text:
        out["text"] = text
    if do == "open":
        url = _text(step.get("url"), 1000)
        return {"do": do, "url": url} if re.match(r"^https?://", url) else None
    if do in TARGETED and not (css or text):
        return None
    if do == "type":
        out["value"] = str(step.get("value") or "")[:VALUE_CHARS]
    if do == "key":
        key = _text(step.get("key"), 30)
        if not key:
            return None
        out["key"] = key
    if do == "copy":
        name = _text(step.get("as"), 31)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,30}", name):
            return None
        out["as"] = name
    if do == "wait":
        if not css:
            out.pop("text", None)
            out["ms"] = max(100, min(int(step.get("ms") or 1000) if str(step.get("ms") or "").isdigit() else 1000, 10000))
    if do == "scroll":
        direction = step.get("direction")
        out = {"do": do, "direction": direction if direction in {"down", "up", "top", "bottom"} else "down"}
    return out


def clean_schedule(schedule: Any) -> dict:
    """{"kind": "manual"} or {"kind": "daily" | "weekdays", "at": "HH:MM"}."""
    if isinstance(schedule, dict) and schedule.get("kind") in {"daily", "weekdays"}:
        match = HHMM.match(str(schedule.get("at") or "").strip())
        if match:
            return {"kind": schedule["kind"], "at": f"{int(match[1]):02d}:{match[2]}"}
    return {"kind": "manual"}


def clean(data: Any) -> dict:
    """A whole automation from Mia's JSON: raises ValueError with a reason she can fix."""
    if not isinstance(data, dict):
        raise ValueError("an automation is a JSON object")
    name = _text(data.get("name"), NAME_CHARS)
    if not name:
        raise ValueError("it needs a name")
    raw = data.get("steps") if isinstance(data.get("steps"), list) else []
    steps = [s for s in (clean_step(s) for s in raw[:MAX_STEPS]) if s]
    if not steps:
        raise ValueError("it has no steps it can run")
    if steps[0]["do"] != "open":
        raise ValueError("the first step must open a page")
    copied = {s["as"] for s in steps if s["do"] == "copy"}
    missing = sorted({m for s in steps if s["do"] == "type" for m in VAR.findall(s["value"])} - copied)
    if missing:
        raise ValueError(f"it uses {{{{{missing[0]}}}}} without copying it first")
    return {"name": name, "about": _text(data.get("about"), ABOUT_CHARS), "steps": steps,
            "schedule": clean_schedule(data.get("schedule"))}


def describe_step(step: dict) -> str:
    """A step in plain words, for the panel."""
    target = f"“{step['text']}”" if step.get("text") else "the element at " + step.get("css", "")
    do = step["do"]
    if do == "open":
        return f"Open {step['url']}"
    if do == "click":
        return f"Click {target}"
    if do == "type":
        return f"Type “{_text(step['value'], 60)}” into {target}"
    if do == "key":
        return f"Press {step['key']}" + (f" in {target}" if step.get("css") or step.get("text") else "")
    if do == "copy":
        return f"Copy {target} as {{{{{step['as']}}}}}"
    if do == "wait":
        return f"Wait for {target}" if step.get("css") else f"Wait {step['ms'] / 1000:g} s"
    return f"Scroll {step['direction']}"


def describe_schedule(schedule: dict) -> str:
    kind = schedule.get("kind")
    if kind not in {"daily", "weekdays"}:
        return "Runs when you press Play"
    offset = time.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
    when = "Every day" if kind == "daily" else "Weekdays"
    return f"{when} at {schedule['at']}" + (f" · {zone}" if zone else "")


def slot(schedule: dict, now: datetime) -> str:
    """Today's scheduled run, as "YYYY-MM-DD", once its time has come (within the catch-up window)."""
    kind = schedule.get("kind")
    if kind not in {"daily", "weekdays"} or (kind == "weekdays" and now.weekday() >= 5):
        return ""
    hour, minute = map(int, schedule["at"].split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now.strftime("%Y-%m-%d") if at <= now < at + CATCH_UP else ""


def past_slot(schedule: dict, now: datetime) -> str:
    """Today's date when today's time has already passed: a new schedule doesn't fire for it."""
    kind = schedule.get("kind")
    if kind not in {"daily", "weekdays"}:
        return ""
    hour, minute = map(int, schedule["at"].split(":"))
    return now.strftime("%Y-%m-%d") if now.replace(hour=hour, minute=minute, second=0, microsecond=0) <= now else ""


class AutomationStore:
    def __init__(self, path: Path | None = None):
        self.path = path  # None keeps them in memory only
        self.items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.path:
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [a for a in (data if isinstance(data, list) else [])
                if isinstance(a, dict) and AUTOMATION_ID.match(str(a.get("id"))) and isinstance(a.get("steps"), list)]

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.items, f)
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        return list(self.items)

    def get(self, automation_id: Any) -> dict | None:
        return next((a for a in self.items if a["id"] == automation_id), None)

    def find(self, name: Any) -> dict | None:
        wanted = _text(name, NAME_CHARS).casefold()
        return next((a for a in self.items if a["name"].casefold() == wanted), None) if wanted else None

    def add(self, data: dict, now: datetime | None = None) -> dict:
        """Save a cleaned automation; one with the same name is replaced (Mia improving it)."""
        now = now or datetime.now()
        old = self.find(data["name"])
        item = {"id": old["id"] if old else f"auto-{secrets.token_hex(4)}", **data,
                "paused": False, "created": int(time.time() * 1000),
                "last_slot": past_slot(data["schedule"], now), "last_run": old.get("last_run") if old else None}
        self.items = [a for a in self.items if a is not old][-(MAX_AUTOMATIONS - 1):] + [item]
        self._save()
        return item

    def update(self, automation_id: Any, **fields) -> dict | None:
        item = self.get(automation_id)
        if item:
            item.update(fields)
            self._save()
        return item

    def delete(self, automation_id: Any) -> bool:
        before = len(self.items)
        self.items = [a for a in self.items if a["id"] != automation_id]
        if len(self.items) != before:
            self._save()
        return len(self.items) != before

    def due(self, now: datetime) -> list[dict]:
        """Scheduled automations whose time has come and that haven't run for it yet; marks them run."""
        ready = []
        for item in self.items:
            today = slot(item.get("schedule") or {}, now)
            if today and not item.get("paused") and item.get("last_slot") != today:
                item["last_slot"] = today
                ready.append(item)
        if ready:
            self._save()
        return ready


def view(item: dict, running: bool = False) -> dict:
    """What the panel shows for one automation."""
    last = item.get("last_run") if isinstance(item.get("last_run"), dict) else None
    return {"id": item["id"], "name": item["name"], "about": item.get("about", ""),
            "schedule": describe_schedule(item.get("schedule") or {}),
            "scheduled": (item.get("schedule") or {}).get("kind") in {"daily", "weekdays"},
            "paused": bool(item.get("paused")), "running": running,
            "steps": [describe_step(s) for s in item["steps"]], "last_run": last}
