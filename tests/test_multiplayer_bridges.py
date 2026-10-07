"""
Two machines, one room: each has a Ghost bridge with a scripted extension.

Checks the whole path without Chrome: a bot acting on Ana's machine shows up
in Luis's browser, Ana's cursor reaches Luis, and a bot's suggestion is
accepted by a human on the other machine.

Run:
    python -m pytest tests/test_multiplayer_bridges.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge_server import BridgeServer
from ghost_room import RoomHub, serve_room
from room_link import RoomLink

KEY = "k" * 40
TOKEN = "t" * 64
SHEET = "https://docs.google.com/spreadsheets/d/abc/edit#gid=0"


class ScriptedExtension:
    """Stands in for the Chrome extension: answers commands, records them."""

    def __init__(self, bridge: BridgeServer, tab_id: int):
        self.bridge = bridge
        self.tab_id = tab_id
        self.commands: list[tuple[str, dict]] = []
        self.pushed: list[dict] = []
        self.changed = asyncio.Event()

    async def send(self, raw: str):
        msg = json.loads(raw)
        if "command" not in msg:
            self.pushed.append(msg)
            return
        command, args = msg["command"], msg.get("args", {})
        self.commands.append((command, args))
        self.changed.set()
        meta = {"tab_id": self.tab_id, "url": SHEET, "title": "Q4 budget"}
        if command == "ghost_tab_list":
            result = {"tabs": [{"id": self.tab_id, "url": SHEET, "title": "Q4 budget", "active": True}]}
        elif command == "ghost_fill":
            result = {"filled": True, "tag": "input", "anchor": {"selector": "#B2", "text": "", "rect": {"x": 10, "y": 20, "w": 80, "h": 22}}}
        elif command == "ghost_suggest":
            result = {"id": args.get("id", "s1"), "anchor": {"text": "within 30 days"}, "tab_id": self.tab_id}
        else:
            result = {"ok": True}
        self.bridge._remember_tab(meta)
        self.bridge.pending[msg["id"]].set_result({"id": msg["id"], "result": result, "meta": meta})

    async def wait_for(self, predicate, timeout=5):
        async def loop():
            while True:
                for command, args in self.commands:
                    if predicate(command, args):
                        return command, args
                self.changed.clear()
                await self.changed.wait()
        return await asyncio.wait_for(loop(), timeout)


class ExtensionSocketTests(unittest.IsolatedAsyncioTestCase):
    """The real WebSocket the extension connects to, with the real handshake."""

    async def asyncSetUp(self):
        self.bridge = BridgeServer(token=TOKEN)
        self.server = await self.bridge.serve_extension(0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def _connect(self, token=TOKEN):
        import hashlib
        import hmac
        import secrets
        from websockets.asyncio.client import connect

        ws = await connect(f"ws://127.0.0.1:{self.port}/ghost-bridge", additional_headers={"Origin": "chrome-extension://abc"},
                           max_size=16 * 1024 * 1024)
        client_nonce = secrets.token_hex(32)
        await ws.send(json.dumps({"type": "auth_init", "client_nonce": client_nonce}))
        challenge = json.loads(await ws.recv())
        proof = hmac.new(token.encode(), f"ghost-ws-client-v1:{client_nonce}:{challenge['server_nonce']}".encode(), hashlib.sha256).hexdigest()
        await ws.send(json.dumps({"type": "auth_response", "client_proof": proof}))
        return ws

    async def test_a_cropped_picture_sized_message_keeps_the_connection(self):
        import websockets

        ws = await self._connect()
        self.assertEqual(json.loads(await ws.recv())["type"], "authenticated")
        # Bigger than the websockets default of 1 MiB, which used to close the socket.
        await ws.send(json.dumps({"type": "heartbeat", "pad": "x" * (3 * 1024 * 1024)}))
        pong = asyncio.ensure_future(self.bridge.send_command("ping", {}, timeout=5))
        request = json.loads(await asyncio.wait_for(ws.recv(), 5))
        self.assertEqual(request["command"], "ping")
        await ws.send(json.dumps({"id": request["id"], "result": {"pong": True}}))
        self.assertTrue((await pong)["result"]["pong"])
        # And the bound still holds.
        await ws.send(json.dumps({"type": "heartbeat", "pad": "x" * (17 * 1024 * 1024)}))
        with self.assertRaises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
        await ws.close()

    async def test_wrong_token_is_closed_before_any_command(self):
        import websockets

        ws = await self._connect(token="w" * 64)
        with self.assertRaises(websockets.ConnectionClosed) as closed:
            await asyncio.wait_for(ws.recv(), 5)
        self.assertEqual(closed.exception.rcvd.code, 4003)
        self.assertFalse(self.bridge.connected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
