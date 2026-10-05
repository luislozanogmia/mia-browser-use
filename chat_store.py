"""Mia's past conversations, kept on this Mac so closing Chrome loses nothing.

One JSON file per conversation in ~/.ghost/chats, readable only by the person.
Only the messages are kept: bots and their tabs belong to the browser session.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

MAX_CHATS = 50
TITLE_CHARS = 60
CHAT_ID = re.compile(r"^[a-z0-9-]{1,40}$")


def new_id() -> str:
    return f"{int(time.time())}-{secrets.token_hex(3)}"


class ChatStore:
    def __init__(self, folder: Path | None = None):
        self.folder = folder or Path.home() / ".ghost" / "chats"

    def _path(self, chat_id: str) -> Path | None:
        return self.folder / f"{chat_id}.json" if CHAT_ID.match(chat_id or "") else None

    def save(self, chat_id: str, messages: list[dict]) -> None:
        path = self._path(chat_id)
        if not path or not messages:
            return
        first = next((m["text"] for m in messages if m.get("who") == "you"), messages[0].get("text", ""))
        title = " ".join(str(first).split())[:TITLE_CHARS] or "Chat"
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"id": chat_id, "title": title, "ts": int(time.time() * 1000), "messages": messages}, f)
        os.replace(tmp, path)
        self._prune()

    def load(self, chat_id: str) -> dict | None:
        path = self._path(chat_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path else None
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        data["messages"] = [m for m in data["messages"] if isinstance(m, dict) and isinstance(m.get("text"), str)]
        return data

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if path:
            path.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        """Newest first: {"id", "title", "ts"} for each saved conversation."""
        chats = []
        for path in self._files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                chats.append({"id": path.stem, "title": str(data.get("title", ""))[:TITLE_CHARS],
                              "ts": int(data.get("ts", 0))})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return sorted(chats, key=lambda c: c["ts"], reverse=True)

    def _files(self) -> list[Path]:
        try:
            return [p for p in self.folder.glob("*.json") if CHAT_ID.match(p.stem)]
        except OSError:
            return []

    def _prune(self) -> None:
        files = sorted(self._files(), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[MAX_CHATS:]:
            old.unlink(missing_ok=True)
