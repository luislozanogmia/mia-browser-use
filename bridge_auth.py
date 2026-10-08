"""Authentication helpers for Ghost's loopback Chrome bridge."""

from __future__ import annotations

import os
import secrets
import stat
import hashlib
import hmac
import time
from pathlib import Path


TOKEN_ENV = "GHOST_BRIDGE_TOKEN"
TOKEN_FILE_ENV = "GHOST_BRIDGE_TOKEN_FILE"
DEFAULT_TOKEN_PATH = Path.home() / ".ghost" / "bridge.token"
MIN_TOKEN_BYTES = 32
AUTH_WINDOW_SECONDS = 30
AUTH_TIMESTAMP_HEADER = "X-Ghost-Timestamp"
AUTH_NONCE_HEADER = "X-Ghost-Nonce"
AUTH_SIGNATURE_HEADER = "X-Ghost-Signature"
RESPONSE_SIGNATURE_HEADER = "X-Ghost-Response-Signature"
AUTH_INSTANCE_HEADER = "X-Ghost-Instance"
AUTH_CHALLENGE_HEADER = "X-Ghost-Challenge"


class BridgeAuthError(RuntimeError):
    """The local bridge token is missing or stored unsafely."""


def token_path() -> Path:
    return Path(os.environ.get(TOKEN_FILE_ENV, DEFAULT_TOKEN_PATH)).expanduser()


def _validate_token(token: str) -> str:
    token = token.strip()
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:
        raise BridgeAuthError(
            f"{TOKEN_ENV} must contain at least {MIN_TOKEN_BYTES} bytes"
        )
    return token


def _validate_private_parent(path: Path) -> None:
    try:
        info = path.parent.lstat()
    except OSError as exc:
        raise BridgeAuthError(f"Bridge token directory is unavailable: {path.parent}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise BridgeAuthError(f"Bridge token parent must be a directory: {path.parent}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise BridgeAuthError(f"Bridge token directory is not owned by the current user: {path.parent}")
    if os.name != "nt" and info.st_mode & 0o022:  # Windows: NTFS ACLs, no POSIX mode bits
        raise BridgeAuthError(f"Bridge token directory must not be group/world writable: {path.parent}")


def _read_private_file(path: Path) -> str:
    _validate_private_parent(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BridgeAuthError(f"Bridge token path must be a private regular file: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BridgeAuthError(f"Bridge token path must be a regular file: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise BridgeAuthError(f"Bridge token file is not owned by the current user: {path}")
        if os.name != "nt" and info.st_mode & 0o077:
            raise BridgeAuthError(f"Bridge token file permissions must be 0600: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return _validate_token(handle.read())
    finally:
        if fd >= 0:
            os.close(fd)


def challenge_init_signature(token: str, client_nonce: str, timestamp: str) -> str:
    message = f"ghost-http-init-v1\n{client_nonce}\n{timestamp}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def challenge_signature(
    token: str,
    instance: str,
    client_nonce: str,
    challenge: str,
    expires: int,
) -> str:
    message = (
        f"ghost-http-challenge-v1\n{instance}\n{client_nonce}\n{challenge}\n{expires}"
    ).encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def verify_challenge_signature(
    token: str,
    instance: str,
    client_nonce: str,
    challenge: str,
    expires: int,
    supplied: str,
) -> bool:
    expected = challenge_signature(
        token, instance, client_nonce, challenge, expires
    )
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _request_message(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    instance: str,
    challenge: str,
    body: bytes,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"ghost-http-v1\n{method.upper()}\n{path}\n{timestamp}\n{nonce}\n"
        f"{instance}\n{challenge}\n{digest}"
    ).encode()


def signed_request_headers(
    token: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    instance: str,
    challenge: str,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Create replay-bounded request authentication without transmitting the token."""
    timestamp_text = str(int(time.time() if timestamp is None else timestamp))
    nonce = nonce or secrets.token_hex(16)
    signature = hmac.new(
        token.encode(),
        _request_message(
            method, path, timestamp_text, nonce, instance, challenge, body
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        AUTH_TIMESTAMP_HEADER: timestamp_text,
        AUTH_NONCE_HEADER: nonce,
        AUTH_SIGNATURE_HEADER: signature,
        AUTH_INSTANCE_HEADER: instance,
        AUTH_CHALLENGE_HEADER: challenge,
    }


def response_signature(token: str, nonce: str, status: int, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    message = f"ghost-http-response-v1\n{nonce}\n{status}\n{digest}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def verify_response_signature(
    token: str, nonce: str, status: int, body: bytes, supplied: str
) -> bool:
    expected = response_signature(token, nonce, status, body)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def load_bridge_token(*, create: bool = False) -> str:
    """Load the bridge token from the environment or a private token file."""
    configured = os.environ.get(TOKEN_ENV)
    if configured:
        return _validate_token(configured)

    path = token_path()
    try:
        return _read_private_file(path)
    except BridgeAuthError:
        if path.exists() or path.is_symlink():
            raise
    if not create:
        raise BridgeAuthError(
            f"Bridge token not found at {path}. Open Chrome with the Mia extension, or run `mia-browser-use up`, to create it."
        )

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    _validate_private_parent(path)
    value = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (value + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return value
