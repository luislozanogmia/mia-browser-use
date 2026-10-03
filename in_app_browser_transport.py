"""
InAppBrowserTransport — Ghost CLI adapter for an in-app browser.

Communicates with In-App Browser over a private local Unix socket, using a JSON-RPC-style
request/response protocol. It controls the browser pane already owned by the app.

The host Electron app already has a `WebContentsView`-based browser
(`browser.cjs`). This transport sends commands that map 1:1 onto the host
app's existing IPC actions; the host app only needs to expose them over the socket.

Security model:
  - Unix socket with filesystem permissions (only the owning user can connect)
  - Every request carries a unique `id` and an auth `token`
  - Every response echoes the `id` for correlation
  - Page text/content is untrusted and bounded
  - Local-only: private Unix socket; unauthenticated peer discovery over TCP is not used

Usage:
    from in_app_browser_transport import InAppBrowserTransport, InAppBrowserTransportError

    transport = InAppBrowserTransport()              # defaults
    transport = InAppBrowserTransport(token="secret", timeout=30)

    status = transport.status()
    result = transport.call("navigate", {"url": "https://example.com"})
    result = transport.call("read", {"max_chars": 4000})
    result = transport.call("tab_list")
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "ghost"
DEFAULT_SOCKET_PATH = Path(
    os.environ.get("GHOST_IN_APP_BROWSER_SOCKET", DEFAULT_RUNTIME_DIR / "in-app-browser.sock")
)
DEFAULT_TOKEN_PATH = Path(
    os.environ.get("GHOST_IN_APP_BROWSER_TOKEN_FILE", DEFAULT_RUNTIME_DIR / "in-app-browser.token")
)
DEFAULT_TIMEOUT = 30  # seconds
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # 16 MB hard cap on any single response
MAX_PAGE_TEXT_CHARS = 100_000  # bound on returned page text

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InAppBrowserTransportError(Exception):
    """Base error for In-App Browser transport failures."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class InAppBrowserConnectionError(InAppBrowserTransportError):
    """Cannot reach In-App Browser."""

    def __init__(self, message: str):
        super().__init__("CONNECTION_FAILED", message)


class InAppBrowserTimeoutError(InAppBrowserTransportError):
    """Request timed out."""

    def __init__(self, command: str, timeout: float):
        super().__init__("TIMEOUT", f"'{command}' timed out after {timeout}s")


class InAppBrowserAuthError(InAppBrowserTransportError):
    """Authentication rejected."""

    def __init__(self):
        super().__init__("AUTH_FAILED", "In-App Browser rejected the auth token")


class InAppBrowserCommandError(InAppBrowserTransportError):
    """In-App Browser returned an error for a command."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

# Wire format: 4-byte big-endian length prefix + UTF-8 JSON payload.
# This avoids delimiter issues with newline-based protocols when page
# content contains newlines.

def _pack_message(obj: dict) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


def _recv_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    """Receive exactly n bytes, respecting timeout."""
    sock.settimeout(timeout)
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("recv timed out")
        sock.settimeout(min(remaining, timeout))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(sock: socket.socket, timeout: float) -> dict:
    """Receive one length-prefixed JSON message."""
    header = _recv_exact(sock, 4, timeout)
    length = struct.unpack(">I", header)[0]
    if length > MAX_RESPONSE_BYTES:
        raise InAppBrowserTransportError(
            "RESPONSE_TOO_LARGE",
            f"Response {length} bytes exceeds {MAX_RESPONSE_BYTES} limit",
        )
    payload = _recv_exact(sock, length, timeout)
    return json.loads(payload.decode("utf-8"))


def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _sanitize_action_result(command: str, args: dict, result: Any) -> Any:
    if command not in {"fill", "key"} or not isinstance(result, dict):
        return result
    clean = dict(result)
    for key in ("value", "text", "input", "password", "token"):
        clean.pop(key, None)
    if command == "key" and "text" in args:
        clean["typed"] = True
        clean["characters"] = len(str(args["text"]))
    elif isinstance(clean.get("typed"), str):
        clean["characters"] = len(clean["typed"])
        clean["typed"] = True
    return clean


def _read_private_token(path: Path) -> str:
    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
            raise InAppBrowserAuthError()
        if hasattr(os, "getuid") and parent.st_uid != os.getuid():
            raise InAppBrowserAuthError()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except (OSError, InAppBrowserAuthError) as exc:
        raise InAppBrowserAuthError() from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InAppBrowserAuthError()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise InAppBrowserAuthError()
        if info.st_mode & 0o077:
            raise InAppBrowserAuthError()
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            token = handle.read().strip()
        if len(token.encode()) < 32:
            raise InAppBrowserAuthError()
        return token
    finally:
        if fd >= 0:
            os.close(fd)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

@dataclass
class InAppBrowserTransport:
    """Client adapter for the host app's in-app browser.

    Connects over an owner-only Unix socket.
    Each call sends a JSON-RPC-style request and waits for the correlated
    response.
    """

    socket_path: Optional[Path] = None
    token: Optional[str] = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT
    _last_status: Optional[dict] = field(default=None, repr=False)

    def __post_init__(self):
        if self.socket_path is None:
            self.socket_path = DEFAULT_SOCKET_PATH
        # Try to read token from env or token file
        if self.token is None:
            self.token = os.environ.get("GHOST_IN_APP_BROWSER_TOKEN")
        if self.token is None:
            try:
                self.token = _read_private_token(DEFAULT_TOKEN_PATH)
            except InAppBrowserAuthError:
                pass
        if self.token is not None and len(self.token.encode()) < 32:
            raise InAppBrowserAuthError()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> socket.socket:
        """Open the owner-only Unix socket exposed by Hermes Desktop."""
        if not self.socket_path:
            raise InAppBrowserConnectionError("Hermes Desktop Unix socket path is not configured")
        try:
            parent = self.socket_path.parent.lstat()
            info = self.socket_path.lstat()
        except OSError as exc:
            raise InAppBrowserConnectionError("Hermes Desktop private Unix socket is unavailable") from exc
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
            raise InAppBrowserConnectionError("Unix socket directory must not be group/world writable")
        if hasattr(os, "getuid") and parent.st_uid != os.getuid():
            raise InAppBrowserConnectionError("Unix socket directory is not owned by the current user")
        if not stat.S_ISSOCK(info.st_mode):
            raise InAppBrowserConnectionError("Configured Unix path is not a socket")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise InAppBrowserConnectionError("Unix socket is not owned by the current user")
        if info.st_mode & 0o077:
            raise InAppBrowserConnectionError("Unix socket permissions must be 0600")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5)
            sock.connect(str(self.socket_path))
            return sock
        except (OSError, ConnectionError) as exc:
            sock.close()
            raise InAppBrowserConnectionError(
                f"Unix socket exists but connection failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Request/response
    # ------------------------------------------------------------------

    def _send_request(
        self,
        command: str,
        args: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Send a command and wait for the correlated response."""
        timeout = timeout or self.timeout
        if not self.token:
            raise InAppBrowserAuthError()
        request_id = _new_request_id()

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": command,
            "params": args or {},
        }
        if self.token:
            request["token"] = self.token

        sock = self._connect()
        try:
            sock.sendall(_pack_message(request))
            response = _recv_message(sock, timeout)
        except TimeoutError:
            raise InAppBrowserTimeoutError(command, timeout)
        except ConnectionError as exc:
            raise InAppBrowserConnectionError(str(exc))
        finally:
            sock.close()

        # Validate correlation
        resp_id = response.get("id")
        if resp_id != request_id:
            raise InAppBrowserTransportError(
                "ID_MISMATCH",
                f"Expected response id={request_id}, got id={resp_id}",
            )

        # Check for auth error
        error = response.get("error")
        if error:
            code = error.get("code", "UNKNOWN") if isinstance(error, dict) else "UNKNOWN"
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            if code in ("AUTH_FAILED", "UNAUTHORIZED", -32001):
                raise InAppBrowserAuthError()
            raise InAppBrowserCommandError(str(code), message)

        return response.get("result", {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(
        self,
        command: str,
        args: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Send a command to the in-app browser and return the result.

        Commands map to the host app's browser.cjs IPC actions:
            status, navigate, read, vacuum, click, fill, key, eval,
            tab_list, tab_open, tab_switch, tab_close,
            back, forward, reload, stop, screenshot, scroll, wait
        """
        call_args = args or {}
        result = self._send_request(command, call_args, timeout)
        result = _sanitize_action_result(command, call_args, result)

        # Bound text fields to prevent unbounded memory usage
        if isinstance(result, dict):
            for text_key in ("text", "content", "page_text"):
                if text_key in result and isinstance(result[text_key], str):
                    if len(result[text_key]) > MAX_PAGE_TEXT_CHARS:
                        result[text_key] = result[text_key][:MAX_PAGE_TEXT_CHARS]
                        result[f"{text_key}_truncated"] = True

        return result

    def status(self) -> dict:
        """Check in-app browser status."""
        try:
            result = self._send_request("status", timeout=5)
            self._last_status = result
            return result
        except InAppBrowserTransportError:
            self._last_status = None
            return {"connected": False, "error": "In-App Browser not reachable"}

    def ping(self) -> bool:
        """Quick connectivity check."""
        try:
            result = self.status()
            return result.get("connected", False)
        except Exception:
            return False

    @property
    def connected(self) -> bool:
        if self._last_status is None:
            self.status()
        return bool(self._last_status and self._last_status.get("connected"))

    @property
    def transport_kind(self) -> str:
        return "in-app-browser-transport"

    # ------------------------------------------------------------------
    # Convenience methods (mirror mia-browser-use tool names)
    # ------------------------------------------------------------------

    def navigate(self, url: str, tab_id: Optional[int] = None) -> dict:
        args: dict[str, Any] = {"url": url}
        if tab_id is not None:
            args["tab_id"] = tab_id
        return self.call("navigate", args)

    def read(self, max_chars: int = 4000, selector: Optional[str] = None) -> dict:
        args: dict[str, Any] = {"max_chars": min(max_chars, MAX_PAGE_TEXT_CHARS)}
        if selector:
            args["selector"] = selector
        return self.call("read", args)

    def vacuum(self, url: str, limit: int = 30) -> dict:
        return self.call("vacuum", {"url": url, "limit": limit})

    def click(self, choice: Optional[int] = None, selector: Optional[str] = None) -> dict:
        args: dict[str, Any] = {}
        if choice is not None:
            args["choice"] = choice
        if selector:
            args["selector"] = selector
        return self.call("click", args)

    def fill(self, value: str, choice: Optional[int] = None, selector: Optional[str] = None) -> dict:
        args: dict[str, Any] = {"value": value}
        if choice is not None:
            args["choice"] = choice
        if selector:
            args["selector"] = selector
        return self.call("fill", args)

    def key(self, key: Optional[str] = None, text: Optional[str] = None) -> dict:
        args: dict[str, Any] = {}
        if key:
            args["key"] = key
        if text:
            args["text"] = text
        return self.call("key", args)

    def eval(self, script: str) -> dict:
        return self.call("eval", {"script": script})

    def tab_list(self) -> dict:
        return self.call("tab_list")

    def tab_open(self, url: Optional[str] = None) -> dict:
        return self.call("tab_open", {"url": url or "about:blank"})

    def tab_switch(self, tab_id: int) -> dict:
        return self.call("tab_switch", {"tab_id": tab_id})

    def tab_close(self, tab_id: int) -> dict:
        return self.call("tab_close", {"tab_id": tab_id})

    def back(self) -> dict:
        return self.call("back")

    def forward(self) -> dict:
        return self.call("forward")

    def reload(self) -> dict:
        return self.call("reload")

    def stop(self) -> dict:
        return self.call("stop")

    def screenshot(self, format: str = "png", quality: int = 80) -> dict:
        return self.call("screenshot", {"format": format, "quality": quality})

    def scroll(self, direction: str = "down", amount: int = 500) -> dict:
        return self.call("scroll", {"direction": direction, "amount": amount})

    def wait(self, selector: Optional[str] = None, ms: Optional[int] = None) -> dict:
        args: dict[str, Any] = {}
        if selector:
            args["selector"] = selector
        if ms:
            args["ms"] = ms
        return self.call("wait", args)
