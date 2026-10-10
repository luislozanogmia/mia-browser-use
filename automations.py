"""Play Automations: fixed scripts Mia Browser runs step by step, with no AI.

A builder bot writes them: it looks at the real pages in a tab of its own, tests
the script there (skipping anything that sends), and only a script that passed is
saved (see ghost_chat.ChatHub). Running one later needs no model: the bridge
replays the steps, when the person presses Play, on a schedule, or when a bot
uses it. AI Workflows are the other kind of work,
where Mia's bots decide each step as they go.

A script is a list of steps:
  {"do": "open", "url": "https://..."}                      always the first step
  {"do": "click", "css": "...", "text": "Export"}           css, text, or both
  {"do": "type", "css"/"text": ..., "value": "hi {{total}}"}
  {"do": "key", "key": "Enter", "css"/"text": optional}
  {"do": "copy", "css"/"text": ..., "as": "total"}          keeps the text for {{total}}
  {"do": "copy", "source": "url", "as": "source_url"}     keeps the current page's complete URL
  {"do": "wait", "ms": 2000} or {"do": "wait", "css": "..."}
  {"do": "scroll", "direction": "down" | "up" | "top" | "bottom"}
A copy step can keep only the first words: {"do": "copy", ..., "as": "first_name", "words": 1}.

Two optional parts make a script repeat and ask before it runs:
  "each": {"links": "linkedin.com/in/", "next": "Next"}   the first step opens a list page; the
      rest run once per link on it whose address contains "links" ({{link}}), page after page
      (the "next" button), until the list is used up or the person presses Stop. Links already
      done are remembered, so the next run carries on.
  "inputs": [{"name": "template", "label": "Message"}]     text boxes in the panel, filled before
      Play, used as {{template}}; an input may itself use copied values like {{first_name}}.

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
from urllib.parse import quote, urlsplit

MAX_AUTOMATIONS = 50
MAX_STEPS = 60
NAME_CHARS = 60
ABOUT_CHARS = 300
VALUE_CHARS = 2000
CATCH_UP = timedelta(minutes=30)  # a scheduled run missed by less than this still runs
ACTIONS = {"open", "click", "type", "key", "copy", "wait", "scroll", "append"}
SHEET_URL = re.compile(r"^https://docs\.google\.com/spreadsheets/")
MAX_COLUMNS = 26
TARGETED = {"click", "type", "copy"}
MAX_INPUTS = 5
MAX_CHOICES = 50  # saved values in one drop-down
MAX_CHOICE_CHARS = 100
MAX_DONE = 5000  # links remembered as done, per automation
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,30}")
VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,30})\s*\}\}")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
AUTOMATION_ID = re.compile(r"^auto-[a-z0-9]{1,20}$")


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _path_inputs(template: str, names: Any) -> set[str]:
    if names is None:
        return set()
    if (not isinstance(names, list) or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in names)
            or len(names) != len(set(names))):
        raise ValueError("path_inputs must be a list of distinct variable names")
    parts = urlsplit(template)
    path_names = set(VAR.findall(parts.path))
    elsewhere = set(VAR.findall(parts.netloc + parts.query + parts.fragment))
    if not set(names) <= path_names or set(names) & elsewhere or VAR.fullmatch(template):
        raise ValueError("path_inputs variables must occur only in the URL path")
    return set(names)


def expand_url(template: str, values: dict, path_inputs: list[str] | None = None) -> str:
    """A whole URL input stays intact; embedded values are URL components, expanded once."""
    try:
        paths = _path_inputs(template, path_inputs)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    def component(match):
        value = str(values.get(match[1]) or "")
        if match[1] not in paths:
            return quote(value, safe="")
        segments = value.split("/")
        if any(s in {"", ".", ".."} for s in segments) or "\\" in value:
            raise RuntimeError(f"{match[1]} needs nonempty path segments without traversal")
        return "/".join(quote(segment, safe="") for segment in segments)
    whole = VAR.fullmatch(template)
    if whole:
        url = str(values.get(whole[1]) or "").strip()
    else:
        url = VAR.sub(component, template)
    if not re.match(r"^https?://", url):
        raise RuntimeError(f"{template} needs a web address (https://…), got “{_text(url, 60)}”")
    return url


def clean_step(step: Any, loop: bool = False) -> dict | None:
    """One step as stored, or None when it's malformed. In a loop, open may go to {{link}}."""
    if not isinstance(step, dict) or step.get("do") not in ACTIONS:
        return None
    do = step["do"]
    out: dict = {"do": do}
    css, text = _text(step.get("css"), 300), _text(step.get("text"), 120)
    if css:
        out["css"] = css
    if text:
        out["text"] = text
    label = _text(step.get("label"), 60)
    if label and not text and css:  # what the panel calls an element found only by its css
        out["label"] = label
    if do == "open":
        url = _text(step.get("url"), 1000)
        try:
            paths = _path_inputs(url, step.get("path_inputs"))
        except ValueError:
            return None
        path_option = {"path_inputs": sorted(paths)} if paths else {}
        var = VAR.fullmatch(url)
        if var:  # {{link}} in a loop, or a page the person gives before Play (an input)
            return {"do": do, "url": f"{{{{{var[1]}}}}}"} if loop or var[1] != "link" else None
        return {"do": do, "url": url, **path_option} if re.match(r"^https?://", url) else None
    if do == "copy" and "source" in step:
        name = _text(step.get("as"), 31)
        if step["source"] != "url" or css or text or "words" in step or not NAME.fullmatch(name):
            return None
        out = {"do": "copy", "source": "url", "as": name}
        if "expected_url" in step:
            expected = _text(step["expected_url"], 1000)
            if not (re.match(r"^https?://", expected) or VAR.fullmatch(expected)):
                return None
            try:
                paths = _path_inputs(expected, step.get("path_inputs"))
            except ValueError:
                return None
            out["expected_url"] = expected
            if paths:
                out["path_inputs"] = sorted(paths)
        elif "path_inputs" in step:
            return None
        return out
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
        if not NAME.fullmatch(name):
            return None
        out["as"] = name
        words = step.get("words")
        if isinstance(words, int) and not isinstance(words, bool) and 1 <= words <= 20:
            out["words"] = words
    if do == "wait":
        if not css:
            out.pop("text", None)
            out["ms"] = max(100, min(int(step.get("ms") or 1000) if str(step.get("ms") or "").isdigit() else 1000, 10000))
    if do == "append":
        # A new row at the bottom of a Google Sheet tab. sheet and tab are usually the person's boxes.
        sheet, tab = _text(step.get("sheet"), 1000), _text(step.get("tab"), 100)
        if not (VAR.fullmatch(sheet) or SHEET_URL.match(sheet)) or not tab:
            return None
        row = step.get("row") if isinstance(step.get("row"), list) else []
        out = {"do": do, "sheet": sheet, "tab": tab,
               "row": [str(v if v is not None else "")[:VALUE_CHARS] for v in row[:MAX_COLUMNS]]}
        unique = _text(step.get("unique"), 200)
        if unique:  # a value that's in the tab already means the row is there: skip it
            out["unique"] = unique
        return out
    if do == "scroll":
        direction = step.get("direction")
        out = {"do": do, "direction": direction if direction in {"down", "up", "top", "bottom"} else "down"}
    return out


def clean_choices(choices: Any) -> dict:
    """A box with a drop-down: the values the person saved for it (a list of searches, say)."""
    if not isinstance(choices, list):
        return {}
    kept = list(dict.fromkeys(c for c in (_text(c, MAX_CHOICE_CHARS) for c in choices[:MAX_CHOICES]) if c))
    return {"choices": kept}


def clean_column(item: dict) -> dict:
    """A drop-down can also list the values of one column of the automation's spreadsheet (its header)."""
    column = _text(item.get("column"), 60) if isinstance(item.get("choices"), list) else ""
    return {"column": column} if column else {}


def clean_schedule(schedule: Any) -> dict:
    """{"kind": "manual"} or {"kind": "daily" | "weekdays", "at": "HH:MM"}."""
    if isinstance(schedule, dict) and schedule.get("kind") in {"daily", "weekdays"}:
        match = HHMM.match(str(schedule.get("at") or "").strip())
        if match:
            return {"kind": schedule["kind"], "at": f"{int(match[1]):02d}:{match[2]}"}
    return {"kind": "manual"}


def validate_variables(item: dict, given: dict | None = None) -> None:
    """Check variables where they are consumed, before later copy steps can define them.

    With run inputs, also check the second expansion used by type steps: a message
    template can use a copied name, but only after that name has been copied.
    Unused input templates impose no dependencies on the script.
    """
    input_names = {i["name"] for i in item.get("inputs", [])}
    known = input_names | {"page_url"}
    if item.get("each"):
        known.add("link")
    templates = given if isinstance(given, dict) else {}
    overwritten = set()
    for n, step in enumerate(item["steps"], 1):
        do = step["do"]
        if do == "type":
            text = step["value"]
        elif do == "open":
            text = step["url"]
        elif do == "append":
            text = " ".join([step["sheet"], step["tab"], step.get("unique", ""), *step["row"]])
        elif do == "copy" and step.get("source") == "url":
            text = step.get("expected_url", "")
        else:
            text = ""
        used = set(VAR.findall(text)) | set(VAR.findall(step.get("text", "")))
        if do == "type":
            # Play expands type values twice; open and append expand only once.
            for name in set(VAR.findall(text)) & input_names - overwritten:
                used.update(VAR.findall(str(templates.get(name) or "")))
        missing = sorted(used - known)
        if missing:
            raise ValueError(f"step {n} uses {{{{{missing[0]}}}}} without copying it first or asking for it")
        if do == "copy":
            known.add(step["as"])
            overwritten.add(step["as"])


def clean(data: Any) -> dict:
    """A whole automation from Mia's JSON: raises ValueError with a reason she can fix."""
    if not isinstance(data, dict):
        raise ValueError("an automation is a JSON object")
    name = _text(data.get("name"), NAME_CHARS)
    if not name:
        raise ValueError("it needs a name")
    each = data.get("each") if isinstance(data.get("each"), dict) else None
    links = _text(each.get("links"), 200) if each else ""
    raw = data.get("steps") if isinstance(data.get("steps"), list) else []
    steps = [s for s in (clean_step(s, loop=bool(links)) for s in raw[:MAX_STEPS]) if s]
    if not steps:
        raise ValueError("it has no steps it can run")
    # Without an open step it runs on the page the person has open (a profile they're looking at).
    if steps[0]["do"] == "open" and steps[0]["url"] == "{{link}}":
        raise ValueError("the first step must open a page")
    if links and steps[0]["do"] != "open":
        raise ValueError("a repeating automation's first step must open the list page")
    # A page the person gives before Play is an input; {{link}} is the loop's item.
    if each and not links:
        raise ValueError("a repeating automation needs the text its links contain")
    if links and len(steps) < 2:
        raise ValueError("a repeating automation needs steps to run for each link")
    inputs = []
    for item in (data.get("inputs") if isinstance(data.get("inputs"), list) else [])[:MAX_INPUTS]:
        key = _text(item.get("name"), 31) if isinstance(item, dict) else ""
        if NAME.fullmatch(key) and key not in {i["name"] for i in inputs} and key != "link":
            inputs.append({"name": key, "label": _text(item.get("label"), 60) or key.replace("_", " ").capitalize(),
                           **clean_choices(item.get("choices")), **clean_column(item)})
    out = {"name": name, "about": _text(data.get("about"), ABOUT_CHARS), "steps": steps,
           "schedule": clean_schedule(data.get("schedule")), "inputs": inputs}
    if links:
        out["each"] = {"links": links, "next": _text(each.get("next"), 60)}
        within = _text(each.get("within"), 200)
        if within:  # only links inside this part of the page (the results, not a menu)
            out["each"]["within"] = within
    validate_variables(out)
    return out


def describe_step(step: dict) -> str:
    """A step in plain words, for the panel."""
    named = step.get("text") or step.get("label")
    target = f"“{named}”" if named else "the element at " + step.get("css", "")
    do = step["do"]
    if do == "open":
        if step["url"] == "{{link}}":
            return "Open the link"
        return f"Open the page in {step['url']}" if VAR.fullmatch(step["url"]) else f"Open {step['url']}"
    if do == "click":
        return f"Click {target}"
    if do == "type":
        return f"Type “{_text(step['value'], 60)}” into {target}"
    if do == "key":
        return f"Press {step['key']}" + (f" in {target}" if step.get("css") or step.get("text") else "")
    if do == "copy":
        if step.get("source") == "url":
            return (f"Copy the current page URL as {{{{{step['as']}}}}}"
                    + (f" after it reaches {step['expected_url']}" if step.get("expected_url") else ""))
        if step["as"] == "first_name" and step.get("words") == 1:
            return "Copy the person's first name as {{first_name}}"
        if step["as"] == "full_name" and not step.get("text"):
            return "Copy the person's full name as {{full_name}}"
        part = f"the first word{'s' if step['words'] > 1 else ''} of " if step.get("words") else ""
        return f"Copy {part}{target} as {{{{{step['as']}}}}}"
    if do == "wait":
        return f"Wait for {target}" if step.get("css") else f"Wait {step['ms'] / 1000:g} s"
    if do == "append":
        tab = f"the tab {step['tab']}" if VAR.fullmatch(step["tab"]) else f"the “{step['tab']}” tab"
        cols = f": {', '.join(c or '(blank)' for c in step['row'])}" if step["row"] else ""
        return f"Add a row at the bottom of {tab} of the spreadsheet{cols}"
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
        item = {"id": old["id"] if old else f"auto-{secrets.token_hex(4)}", **data, "done": [],
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

    def delete_step(self, automation_id: Any, index: Any) -> str:
        """Take one step out; the reason when the rest wouldn't make a working automation."""
        item = self.get(automation_id)
        if item is None or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(item["steps"]):
            return "that step isn't there anymore"
        data = {**item, "steps": item["steps"][:index] + item["steps"][index + 1:]}
        try:
            cleaned = clean(data)
        except ValueError as exc:
            return str(exc)
        item.update(steps=cleaned["steps"], inputs=cleaned["inputs"])
        self._save()
        return ""

    def set_choices(self, automation_id: Any, name: Any, choices: Any) -> bool:
        """The saved values of one of the automation's drop-downs, added or deleted in the panel."""
        item = self.get(automation_id)
        box = next((i for i in (item or {}).get("inputs") or [] if i["name"] == name), None)
        if box is None:
            return False
        box.update(clean_choices(choices if isinstance(choices, list) else []))
        self._save()
        return True

    def rename(self, automation_id: Any, name: Any) -> str:
        """A new name from the panel; the reason it was kept as it was, or "" when renamed."""
        item = self.get(automation_id)
        clean = _text(name, NAME_CHARS)
        if item is None:
            return "that automation is gone"
        if not clean:
            return "the name can't be empty"
        other = self.find(clean)
        if other is not None and other is not item:
            return f"another automation is already called “{other['name']}”"
        item["name"] = clean
        self._save()
        return ""

    def set_column(self, automation_id: Any, name: Any, column: Any) -> bool:
        """The spreadsheet column a drop-down lists the values of, picked in the panel ("" for none)."""
        item = self.get(automation_id)
        box = next((i for i in (item or {}).get("inputs") or [] if i["name"] == name and "choices" in i), None)
        if box is None:
            return False
        box.pop("column", None)
        box.update(clean_column({"choices": [], "column": column}))
        self._save()
        return True

    def mark_done(self, automation_id: Any, link: str) -> None:
        """A link the automation finished: the next run skips it."""
        item = self.get(automation_id)
        if item is not None:
            item["done"] = (item.get("done") or [])[-(MAX_DONE - 1):] + [link]
            self._save()

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
            "paused": bool(item.get("paused")), "running": running, "full_access": item.get("full_access") is True,
            "steps": [describe_step(s) for s in item["steps"]], "last_run": last,
            # Which boxes each step uses, so the panel shows a box under the step that types it.
            "uses": [sorted(set(VAR.findall(" ".join([s.get("value", ""), s.get("sheet", ""), s.get("tab", ""),
                                                      *s.get("row", [])]))))
                     for s in item["steps"]],
            "inputs": [dict(i) for i in item.get("inputs") or []],
            # The box holding the spreadsheet link, so a drop-down can list one of its columns.
            "sheet_input": next((m for s in item["steps"] if s["do"] == "append"
                                 for m in VAR.findall(s.get("sheet", ""))), ""),
            "each": (f"Repeats for each link with “{item['each']['links']}” in its address"
                     + (f" inside {item['each']['within']}" if item["each"].get("within") else "")
                     + (f", page after page (“{item['each']['next']}”)" if item["each"].get("next") else "")
                     if item.get("each") else ""),
            "done": len(item.get("done") or [])}
