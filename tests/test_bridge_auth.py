from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import hashlib
import hmac
import secrets
import time
import ast
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridge_auth import (
    AUTH_NONCE_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    BridgeAuthError,
    challenge_init_signature,
    challenge_signature,
    load_bridge_token,
    signed_request_headers,
    verify_response_signature,
)
from bridge_server import BridgeServer
from bridge_transport import BridgeError, BridgeTransport


TOKEN = "t" * 48


class FakeSocket:
    def __init__(self, token, origin="chrome-extension://unit-test"):
        self.request = type("Request", (), {"headers": {"Origin": origin}})()
        self.remote_address = ("127.0.0.1", 1)
        self.token = token
        self.client_nonce = "a" * 64
        self.messages = [json.dumps({"type": "auth_init", "client_nonce": self.client_nonce})]
        self.sent = []
        self.closed = None

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        challenge = self.sent[-1]
        message = f"ghost-ws-client-v1:{self.client_nonce}:{challenge['server_nonce']}".encode()
        proof = hmac.new(self.token.encode(), message, hashlib.sha256).hexdigest()
        return json.dumps({"type": "auth_response", "client_proof": proof})

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self, code, reason):
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class BridgeAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = BridgeServer(token=TOKEN)
        app = web.Application()
        app.router.add_get("/challenge", self.bridge.handle_challenge)
        app.router.add_get("/status", self.bridge.handle_status)
        app.router.add_post("/call", self.bridge.handle_call)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def headers(self, method, path, body=b"", token=TOKEN, **kwargs):
        challenge = secrets.token_hex(32)
        self.bridge.http_challenges[challenge] = (int(time.time()) + 30, "test-client")
        return signed_request_headers(
            token,
            method,
            path,
            body,
            instance=self.bridge.instance_id,
            challenge=challenge,
            **kwargs,
        )

    async def test_http_rejects_missing_and_wrong_tokens(self):
        response = await self.client.get("/status")
        self.assertEqual(response.status, 401)

    async def test_challenge_init_is_authenticated_fresh_and_single_use(self):
        client_nonce = "b" * 32
        timestamp = str(int(time.time()))
        headers = {
            "X-Ghost-Timestamp": timestamp,
            "X-Ghost-Nonce": client_nonce,
            "X-Ghost-Signature": challenge_init_signature(
                TOKEN, client_nonce, timestamp
            ),
        }
        response = await self.client.get("/challenge", headers=headers)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["client_nonce"], client_nonce)
        self.assertEqual(
            body["proof"],
            challenge_signature(
                TOKEN,
                body["instance"],
                client_nonce,
                body["challenge"],
                body["expires"],
            ),
        )
        self.assertEqual((await self.client.get("/challenge", headers=headers)).status, 401)

        old_timestamp = str(int(time.time()) - 60)
        expired_headers = {
            "X-Ghost-Timestamp": old_timestamp,
            "X-Ghost-Nonce": "c" * 32,
            "X-Ghost-Signature": challenge_init_signature(
                TOKEN, "c" * 32, old_timestamp
            ),
        }
        self.assertEqual(
            (await self.client.get("/challenge", headers=expired_headers)).status,
            401,
        )
        response = await self.client.get("/status", headers=self.headers("GET", "/status", token="wrong"))
        self.assertEqual(response.status, 401)

    async def test_http_accepts_valid_token(self):
        headers = self.headers("GET", "/status")
        response = await self.client.get("/status", headers=headers)
        self.assertEqual(response.status, 200)
        body = await response.read()
        self.assertTrue(verify_response_signature(
            TOKEN,
            headers[AUTH_NONCE_HEADER],
            response.status,
            body,
            response.headers[RESPONSE_SIGNATURE_HEADER],
        ))
        self.assertFalse(json.loads(body)["connected"])

    async def test_http_rejects_replay_and_expired_signature(self):
        headers = self.headers("GET", "/status")
        self.assertEqual((await self.client.get("/status", headers=headers)).status, 200)
        self.assertEqual((await self.client.get("/status", headers=headers)).status, 401)
        expired = self.headers("GET", "/status", timestamp=int(time.time()) - 60)
        self.assertEqual((await self.client.get("/status", headers=expired)).status, 401)

    async def test_call_rejects_invalid_argument_and_timeout_shapes(self):
        invalid_args = json.dumps({"command": "ghost_read", "args": [], "timeout": 10}).encode()
        response = await self.client.post(
            "/call",
            headers={**self.headers("POST", "/call", invalid_args), "Content-Type": "application/json"},
            data=invalid_args,
        )
        self.assertEqual(response.status, 400)
        invalid_timeout = json.dumps({"command": "ghost_read", "args": {}, "timeout": True}).encode()
        response = await self.client.post(
            "/call",
            headers={**self.headers("POST", "/call", invalid_timeout), "Content-Type": "application/json"},
            data=invalid_timeout,
        )
        self.assertEqual(response.status, 400)

    async def test_eval_requires_explicit_opt_in(self):
        body = json.dumps({"command": "ghost_eval", "args": {"script": "() => 1"}}).encode()
        response = await self.client.post(
            "/call",
            headers={**self.headers("POST", "/call", body), "Content-Type": "application/json"},
            data=body,
        )
        self.assertEqual(response.status, 403)

    async def test_authenticated_non_object_json_returns_signed_400(self):
        raw = b"[]"
        headers = self.headers("POST", "/call", raw)
        response = await self.client.post(
            "/call",
            headers={**headers, "Content-Type": "application/json"},
            data=raw,
        )
        self.assertEqual(response.status, 400)
        body = await response.read()
        self.assertTrue(verify_response_signature(
            TOKEN,
            headers[AUTH_NONCE_HEADER],
            response.status,
            body,
            response.headers[RESPONSE_SIGNATURE_HEADER],
        ))

    async def test_websocket_rejects_untrusted_origin(self):
        socket = FakeSocket(TOKEN, origin="https://example.com")
        await self.bridge.ws_handler(socket)
        self.assertEqual(socket.closed[0], 4003)

    async def test_websocket_rejects_wrong_token(self):
        socket = FakeSocket("wrong")
        await self.bridge.ws_handler(socket)
        self.assertEqual(socket.closed[0], 4003)

    async def test_websocket_accepts_extension_with_valid_token(self):
        socket = FakeSocket(TOKEN)
        await self.bridge.ws_handler(socket)
        self.assertEqual(socket.sent[0]["type"], "auth_challenge")
        self.assertEqual(socket.sent[1], {"type": "authenticated"})

    async def test_websocket_never_transmits_raw_token(self):
        socket = FakeSocket(TOKEN)
        await self.bridge.ws_handler(socket)
        self.assertNotIn(TOKEN, json.dumps(socket.sent))


class TokenFileTests(unittest.TestCase):
    def test_generated_token_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            with mock.patch.dict(os.environ, {"GHOST_BRIDGE_TOKEN_FILE": str(path)}, clear=False):
                token = load_bridge_token(create=True)
                self.assertGreaterEqual(len(token.encode()), 32)
                if os.name != "nt":  # Windows has no POSIX mode bits
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits and O_NOFOLLOW; Windows relies on NTFS ACLs")
    def test_rejects_permissive_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(TOKEN)
            path.chmod(0o644)
            with mock.patch.dict(os.environ, {"GHOST_BRIDGE_TOKEN_FILE": str(path)}, clear=False):
                with self.assertRaises(BridgeAuthError):
                    load_bridge_token()


class BridgeClientTrustTests(unittest.TestCase):
    def test_rejects_oversized_response_before_authentication(self):
        class LargeResponse:
            def read(self, limit=-1):
                return b"x" * limit

        with self.assertRaisesRegex(BridgeError, "16 MiB"):
            BridgeTransport._read_bounded(LargeResponse())

    def test_rejects_unsigned_fake_bridge_response(self):
        class FakeResponse:
            status = 200
            headers = {}

            def read(self, _limit=-1):
                return b'{"result":{"controlled":true}}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        transport = BridgeTransport(token=TOKEN)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(BridgeError, "UNTRUSTED_BRIDGE"):
                transport.call("ghost_read", {})

    def test_websocket_cleanup_has_no_return_in_finally(self):
        source = (Path(__file__).resolve().parent.parent / "bridge_server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        returns_in_finally = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for statement in node.finalbody:
                returns_in_finally.extend(
                    child for child in ast.walk(statement) if isinstance(child, ast.Return)
                )
        self.assertEqual(returns_in_finally, [])

    def test_rejects_expired_replayed_challenge(self):
        client_nonce = "d" * 32
        instance = "e" * 32
        challenge = "f" * 64
        expires = int(time.time()) - 3600
        body = json.dumps({
            "instance": instance,
            "client_nonce": client_nonce,
            "challenge": challenge,
            "expires": expires,
            "proof": challenge_signature(
                TOKEN, instance, client_nonce, challenge, expires
            ),
        }).encode()

        class ReplayResponse:
            status = 200
            headers = {}

            def read(self, _limit=-1):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        transport = BridgeTransport(token=TOKEN)
        with (
            mock.patch("bridge_transport.secrets.token_hex", return_value=client_nonce),
            mock.patch("urllib.request.urlopen", return_value=ReplayResponse()),
        ):
            with self.assertRaisesRegex(BridgeError, "stale or mismatched"):
                transport.call("ghost_read", {})

    @unittest.skipIf(os.name == "nt", "POSIX mode bits and O_NOFOLLOW; Windows relies on NTFS ACLs")
    def test_rejects_token_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real-token"
            target.write_text(TOKEN)
            target.chmod(0o600)
            link = root / "token"
            link.symlink_to(target)
            with mock.patch.dict(os.environ, {"GHOST_BRIDGE_TOKEN_FILE": str(link)}, clear=False):
                with self.assertRaises(BridgeAuthError):
                    load_bridge_token()
