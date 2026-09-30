"""Self-contained local clients used by the Hermes Ghost plugin."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import struct
import tempfile
import urllib.error
import urllib.request
import uuid
import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Any


MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MIN_TOKEN_BYTES = 32
AUTH_WINDOW_SECONDS = 30
GHOST_TO_HERMES = {
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


class GhostClientError(RuntimeError):
    pass


def _sanitize_action_result(command: str, args: dict[str, Any], result: Any) -> Any:
    if command not in {"ghost_fill", "ghost_key"} or not isinstance(result, dict):
        return result
    clean = dict(result)
    for key in ("value", "text", "input", "password", "token"):
        clean.pop(key, None)
    if command == "ghost_key" and "text" in args:
        clean["typed"] = True
        clean["characters"] = len(str(args["text"]))
    elif isinstance(clean.get("typed"), str):
        clean["characters"] = len(clean["typed"])
        clean["typed"] = True
    return clean


def _validated_token(token: str) -> str:
    token = token.strip()
    if len(token.encode()) < MIN_TOKEN_BYTES:
        raise GhostClientError(f"Authentication token must contain at least {MIN_TOKEN_BYTES} bytes")
    return token


def _private_token(env_name: str, file_env: str, default_path: Path) -> str:
    token = os.environ.get(env_name, "").strip()
    if token:
        return _validated_token(token)
    path = Path(os.environ.get(file_env, default_path)).expanduser()
    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
            raise GhostClientError(f"Token directory must not be group/world writable: {path.parent}")
        if hasattr(os, "getuid") and parent.st_uid != os.getuid():
            raise GhostClientError(f"Token directory is not owned by the current user: {path.parent}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except (OSError, GhostClientError) as exc:
        if isinstance(exc, GhostClientError):
            raise
        raise GhostClientError(f"Token file is missing or invalid: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GhostClientError(f"Token file is not regular: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise GhostClientError(f"Token file is not owned by the current user: {path}")
        if info.st_mode & 0o077:
            raise GhostClientError(f"Token file permissions must be 0600: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return _validated_token(handle.read())
    finally:
        if fd >= 0:
            os.close(fd)


def _challenge_init_proof(token: str, client_nonce: str, timestamp: str) -> str:
    message = f"ghost-http-init-v1\n{client_nonce}\n{timestamp}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def _challenge_proof(
    token: str, instance: str, client_nonce: str, challenge: str, expires: int
) -> str:
    message = (
        f"ghost-http-challenge-v1\n{instance}\n{client_nonce}\n{challenge}\n{expires}"
    ).encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def _signed_headers(
    token: str,
    method: str,
    path: str,
    body: bytes,
    instance: str,
    challenge: str,
) -> tuple[dict[str, str], str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    message = (
        f"ghost-http-v1\n{method}\n{path}\n{timestamp}\n{nonce}\n"
        f"{instance}\n{challenge}\n{digest}"
    ).encode()
    signature = hmac.new(token.encode(), message, hashlib.sha256).hexdigest()
    return {
        "X-Ghost-Timestamp": timestamp,
        "X-Ghost-Nonce": nonce,
        "X-Ghost-Signature": signature,
        "X-Ghost-Instance": instance,
        "X-Ghost-Challenge": challenge,
    }, nonce


def _verify_response(token: str, nonce: str, status: int, body: bytes, supplied: str) -> bool:
    digest = hashlib.sha256(body).hexdigest()
    message = f"ghost-http-response-v1\n{nonce}\n{status}\n{digest}".encode()
    expected = hmac.new(token.encode(), message, hashlib.sha256).hexdigest()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


class ChromeClient:
    def __init__(self, port: int, timeout: float = 60):
        self.base_url = f"http://127.0.0.1:{port}"
        self.timeout = timeout
        self.token = _private_token(
            "GHOST_BRIDGE_TOKEN",
            "GHOST_BRIDGE_TOKEN_FILE",
            Path.home() / ".ghost" / "bridge.token",
        )

    @staticmethod
    def _read_bounded(response) -> bytes:
        raw = response.read(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise GhostClientError("Chrome bridge response exceeded the 16 MiB limit")
        return raw

    def _challenge(self) -> tuple[str, str]:
        client_nonce = secrets.token_hex(16)
        timestamp = str(int(time.time()))
        request = urllib.request.Request(
            self.base_url + "/challenge",
            headers={
                "X-Ghost-Timestamp": timestamp,
                "X-Ghost-Nonce": client_nonce,
                "X-Ghost-Signature": _challenge_init_proof(
                    self.token, client_nonce, timestamp
                ),
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 5)) as response:
                raw = self._read_bounded(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GhostClientError(f"Chrome bridge challenge is unavailable: {exc}") from exc
        try:
            data = json.loads(raw)
            instance = data["instance"]
            echoed_nonce = data["client_nonce"]
            challenge = data["challenge"]
            expires = int(data["expires"])
            supplied = data["proof"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GhostClientError("Chrome bridge returned an invalid challenge") from exc
        now = int(time.time())
        if echoed_nonce != client_nonce or not now <= expires <= now + AUTH_WINDOW_SECONDS:
            raise GhostClientError("Chrome bridge returned a stale or mismatched challenge")
        expected = _challenge_proof(
            self.token, instance, client_nonce, challenge, expires
        )
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise GhostClientError("Chrome bridge returned an invalid challenge signature")
        return instance, challenge

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        method = "POST" if data is not None else "GET"
        instance, challenge = self._challenge()
        headers, nonce = _signed_headers(
            self.token, method, path, data or b"", instance, challenge
        )
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = self._read_bounded(response)
                if not _verify_response(
                    self.token,
                    nonce,
                    response.status,
                    raw,
                    response.headers.get("X-Ghost-Response-Signature", ""),
                ):
                    raise GhostClientError("Chrome bridge returned an invalid response signature")
        except urllib.error.HTTPError as exc:
            raw = self._read_bounded(exc)
            if not _verify_response(
                self.token,
                nonce,
                exc.code,
                raw,
                exc.headers.get("X-Ghost-Response-Signature", ""),
            ):
                raise GhostClientError("Chrome bridge returned an invalid response signature") from exc
            raise GhostClientError(f"Chrome bridge rejected the request (HTTP {exc.code}): {raw.decode('utf-8', 'replace')}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GhostClientError(f"Chrome bridge is unavailable: {exc}") from exc
        return json.loads(raw)

    def status(self) -> dict[str, Any]:
        return self._request("/status")

    def call(self, command: str, args: dict[str, Any]) -> Any:
        response = self._request("/call", {"command": command, "args": args})
        if "error" in response:
            raise GhostClientError(str(response["error"]))
        return _sanitize_action_result(command, args, response.get("result", response))


class HermesDesktopClient:
    def __init__(self, timeout: float = 60):
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "ghost"
        self.socket_path = Path(os.environ.get("GHOST_IN_APP_BROWSER_SOCKET", runtime / "in-app-browser.sock"))
        self.timeout = timeout
        self.token = _private_token(
            "GHOST_IN_APP_BROWSER_TOKEN",
            "GHOST_IN_APP_BROWSER_TOKEN_FILE",
            runtime / "in-app-browser.token",
        )

    def _connect(self) -> socket.socket:
        try:
            parent = self.socket_path.parent.lstat()
            info = self.socket_path.lstat()
        except OSError as exc:
            raise GhostClientError("Hermes Desktop private Unix socket is unavailable") from exc
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
            raise GhostClientError("Hermes Desktop socket directory must be private")
        if hasattr(os, "getuid") and parent.st_uid != os.getuid():
            raise GhostClientError("Hermes Desktop socket directory is not owned by the current user")
        if not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o077:
            raise GhostClientError("Hermes Desktop socket must be a private Unix socket")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise GhostClientError("Hermes Desktop socket is not owned by the current user")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        return sock

    def _receive(self, sock: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise GhostClientError("Hermes Desktop closed the browser connection")
            data.extend(chunk)
        return bytes(data)

    def call(self, command: str, args: dict[str, Any]) -> Any:
        method = GHOST_TO_HERMES.get(command)
        if method is None:
            raise GhostClientError(f"Unsupported Hermes Desktop command: {command}")
        request_id = uuid.uuid4().hex
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": args,
            "token": self.token,
        }, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            raise GhostClientError("Browser request exceeded the 16 MiB limit")
        try:
            sock = self._connect()
            with sock:
                sock.sendall(struct.pack(">I", len(payload)) + payload)
                length = struct.unpack(">I", self._receive(sock, 4))[0]
                if length > MAX_MESSAGE_BYTES:
                    raise GhostClientError("Hermes Desktop response exceeded the 16 MiB limit")
                response = json.loads(self._receive(sock, length))
        except (OSError, TimeoutError) as exc:
            raise GhostClientError(f"Hermes Desktop browser is unavailable: {exc}") from exc
        if response.get("id") != request_id:
            raise GhostClientError("Hermes Desktop returned a mismatched request id")
        if response.get("error"):
            raise GhostClientError(str(response["error"]))
        return _sanitize_action_result(command, args, response.get("result", {}))

    def status(self) -> dict[str, Any]:
        result = self.call("ghost_status", {})
        return result if isinstance(result, dict) else {"connected": True, "result": result}


class BrowserClient:
    def __init__(self, backend: str = "auto", chrome_port: int = 9378, allow_eval: bool = False, actor: str | None = None):
        if backend not in {"auto", "chrome", "hermes"}:
            raise GhostClientError("backend must be auto, chrome, or hermes")
        if actor and not re.match(r"^[A-Za-z0-9_.:-]{1,64}$", actor):
            raise GhostClientError("actor_id must be 1-64 of A-Z a-z 0-9 _ . : -")
        # Who is acting. Set in the plugin config, never chosen by the model.
        self.actor = actor or None
        self.backend = backend
        self.chrome_port = chrome_port
        self.allow_eval = allow_eval
        self.active_backend = ""
        self.transport: ChromeClient | HermesDesktopClient | None = None

    def connect(self):
        failures = []
        if self.backend in {"auto", "hermes"}:
            try:
                candidate = HermesDesktopClient()
                status = candidate.status()
                self.transport = candidate
                self.active_backend = "hermes"
                return status
            except Exception as exc:
                failures.append(str(exc))
                if self.backend == "hermes":
                    raise GhostClientError(failures[-1]) from exc
        try:
            candidate = ChromeClient(self.chrome_port)
            status = candidate.status()
            if not status.get("connected"):
                raise GhostClientError("Chrome extension is not connected")
            self.transport = candidate
            self.active_backend = "chrome"
            return status
        except Exception as exc:
            failures.append(str(exc))
            raise GhostClientError("No browser connection is available: " + " | ".join(failures)) from exc

    def call(self, command: str, args: dict[str, Any]):
        if command == "ghost_eval" and not self.allow_eval:
            raise GhostClientError("ghost_eval is disabled; set allow_eval: true to opt in")
        if command == "ghost_pdf_read" and self.backend == "hermes":
            raise GhostClientError("ghost_pdf_read is available through the Chrome extension only")
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
        return self.transport.call(command, args)
