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
from ghost_room import RoomHub, page_key, serve_room
from room_link import RoomLink

KEY = "k" * 40
TOKEN = "t" * 64
SHEET = "https://docs.google.com/spreadsheets/d/abc/edit#gid=0"


class ScriptedExtension:
    """Stands in for the Chrome extension: answers commands, records them."""

    def __init__(self, bridge: BridgeServer, tab_id: int):
        self.bridge = bridge
        self.tab_id = tab_id
        self.tab_url = SHEET
        self.read_url_override = None
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
        meta = {"tab_id": self.tab_id, "url": self.tab_url, "title": "Q4 budget"}
        if command == "ghost_tab_list":
            result = {"tabs": [{"id": self.tab_id, "url": self.tab_url, "title": "Q4 budget", "active": True}]}
        elif command == "ghost_read":
            result = {"url": self.read_url_override or self.tab_url, "title": "Q4 budget", "content": "Visible cells"}
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


class TwoMachineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = RoomHub()
        self.hub.store.create_room("demo", KEY)
        self.server = await serve_room(self.hub, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.machines = {}
        for me, color, tab in (("luis", "#3b82f6", 11), ("ana", "#ef4444", 22)):
            link = RoomLink(url, "demo", KEY, {"id": me, "kind": "human", "name": me.title(), "color": color}, on_message=None)
            bridge = BridgeServer(token=TOKEN, room=link)
            link.on_message = bridge.on_room_message
            ext = ScriptedExtension(bridge, tab)
            bridge.extension_ws = ext
            bridge.connected = True
            link.start()
            self.machines[me] = (bridge, link, ext)
        for _bridge, link, _ext in self.machines.values():
            await self._until(lambda link=link: link.connected)

    async def asyncTearDown(self):
        for _bridge, link, _ext in self.machines.values():
            await link.stop()
        self.server.close()
        await self.server.wait_closed()

    async def _until(self, check, timeout=5):
        async def loop():
            while not check():
                await asyncio.sleep(0.02)
        await asyncio.wait_for(loop(), timeout)

    async def _share(self):
        luis, luis_link, luis_ext = self.machines["luis"]
        ana, ana_link, ana_ext = self.machines["ana"]
        await luis.execute("room_share", {"url": SHEET, "title": "Q4 budget"}, 10)
        room_url = luis_link.local_pages[page_key(SHEET)]
        await self._until(lambda: ana_link.is_shared(room_url) and luis_link.is_shared(SHEET))
        self.assertNotIn("/spreadsheets/", json.dumps(ana_link.shared[room_url]))
        self.assertEqual(ana_link.shared[room_url]["origin"], "https://docs.google.com/")
        self.assertTrue(ana_link.bind_local(SHEET, room_url))
        for bridge, ext in ((luis, luis_ext), (ana, ana_ext)):
            await bridge._extension_event({"type": "room_access", "tab_id": ext.tab_id, "url": SHEET,
                                           "room_url": room_url, "accepted": True})
        return room_url

    async def test_unaccepted_page_cannot_attach_to_matching_local_tab(self):
        luis, _, luis_ext = self.machines["luis"]
        ana, ana_link, _ = self.machines["ana"]
        await ana.execute("room_share", {"url": SHEET, "title": "Q4 budget"}, 10)
        room_url = ana_link.local_pages[page_key(SHEET)]
        await self._until(lambda: ana_link.is_shared(SHEET) and luis.room.is_shared(room_url))
        self.assertEqual(await luis._tabs_showing(SHEET), [])
        ok, approved = await luis.execute("room_approved_tabs", {}, 10)
        self.assertTrue(ok)
        self.assertEqual(approved["tabs"], [])
        self.assertFalse([c for c, _ in luis_ext.commands if c == "ghost_show"])

    async def test_room_read_rechecks_consent_and_actual_tab_address(self):
        room_url = await self._share()
        luis, _, ext = self.machines["luis"]
        args = {"tab_id": ext.tab_id, "url": room_url, "actor_id": "answer", "max_chars": 1000}
        ok, page = await luis.execute("room_read", args, 10)
        self.assertTrue(ok)
        self.assertEqual(page["content"], "Visible cells")
        ext.read_url_override = "https://bank.example/account"
        with self.assertRaisesRegex(Exception, "ROOM_ACCESS_DENIED"):
            await luis.execute("room_read", args, 10)
        ext.read_url_override = None
        ext.tab_url = "https://bank.example/account"
        with self.assertRaisesRegex(Exception, "ROOM_ACCESS_DENIED"):
            await luis.execute("room_read", args, 10)
        self.assertEqual(len([c for c, _ in ext.commands if c == "ghost_read"]), 2)

    async def test_query_change_requires_fresh_tab_acceptance(self):
        room_url = await self._share()
        luis, _, ext = self.machines["luis"]
        ext.tab_url = SHEET.replace("#gid=0", "?account=other#gid=0")
        ok, approved = await luis.execute("room_approved_tabs", {}, 10)
        self.assertTrue(ok)
        self.assertEqual(approved["tabs"], [])
        with self.assertRaisesRegex(Exception, "ROOM_ACCESS_DENIED"):
            await luis.execute("room_read", {"tab_id": ext.tab_id, "url": room_url}, 10)

    async def test_bot_on_one_machine_is_drawn_on_the_other(self):
        await self._share()
        ana, _, _ = self.machines["ana"]
        _, _, luis_ext = self.machines["luis"]
        ok, _ = await ana.execute("ghost_show", {"actor_id": "ledger", "tab_id": 22, "label": "Ledger · Q3 actuals", "color": "#3b82f6", "selector": "table"}, 10)
        self.assertTrue(ok)
        ok, _ = await ana.execute("ghost_fill", {"actor_id": "ledger", "tab_id": 22, "choice": 4, "value": "1,140"}, 10)
        self.assertTrue(ok)
        _, args = await luis_ext.wait_for(lambda c, a: c == "ghost_show" and a.get("anchor", {}).get("selector") == "#B2")
        self.assertEqual(args["actor_id"], "ledger")
        self.assertEqual(args["tab_id"], 11)
        self.assertEqual(args["label"], "Ledger · Q3 actuals")
        self.assertEqual(args["owner_color"], "#ef4444")  # ring = Ana's player color
        self.assertNotIn("value", json.dumps(args))  # what the bot typed never travels

    async def test_human_cursor_travels(self):
        await self._share()
        ana, _, _ = self.machines["ana"]
        _, _, luis_ext = self.machines["luis"]
        await ana._extension_event({"type": "human", "tab_id": 22, "url": SHEET,
                                    "focus": {"selector": "#D7"}, "pointer": {"anchor": {"selector": "#D7"}, "fx": 0.5, "fy": 0.5}})
        _, args = await luis_ext.wait_for(lambda c, a: c == "ghost_show" and a.get("actor_id") == "ana")
        self.assertEqual(args["kind"], "human")
        self.assertEqual(args["pointer"]["fx"], 0.5)

    async def test_unshared_pages_stay_private(self):
        ana, _, _ = self.machines["ana"]
        _, _, luis_ext = self.machines["luis"]
        await ana._extension_event({"type": "human", "tab_id": 22, "url": SHEET, "focus": {"selector": "#D7"}})
        await ana.execute("ghost_fill", {"actor_id": "ledger", "tab_id": 22, "choice": 4, "value": "x"}, 10)
        await asyncio.sleep(0.3)
        self.assertFalse([c for c, a in luis_ext.commands if c == "ghost_show"])

    async def test_suggestion_accepted_on_another_machine(self):
        await self._share()
        ana, ana_link, ana_ext = self.machines["ana"]
        luis, _, luis_ext = self.machines["luis"]
        ok, made = await ana.execute("ghost_suggest", {"actor_id": "ledger", "tab_id": 22, "id": "net45", "title": "Net 45",
                                                       "body": "Matches Acme's AP cycle", "text": "within 30 days"}, 10)
        self.assertTrue(ok)
        _, card = await luis_ext.wait_for(lambda c, a: c == "ghost_suggestion" and not a.get("clear"))
        self.assertEqual((card["id"], card["actor"]["owner"]), ("net45", "ana"))
        await luis._extension_event({"type": "resolve", "id": "net45", "decision": "accept"})
        await self._until(lambda: "net45" in ana_link.decisions)
        ok, room = await ana.execute("ghost_room", {}, 10)
        decision = room["decisions_on_your_suggestions"][0]
        self.assertEqual((decision["decision"], decision["by"]["id"]), ("accept", "luis"))
        await ana_ext.wait_for(lambda c, a: c == "ghost_suggestion" and a.get("clear"))

    async def test_cropped_picture_stays_on_the_askers_bridge(self):
        await self._share()
        luis, _, _ = self.machines["luis"]
        ana, ana_link, _ = self.machines["ana"]
        image = "data:image/jpeg;base64," + "A" * 20000  # bigger than a relay message
        await luis._extension_event({"type": "ask", "tab_id": 11, "url": SHEET, "question": "What is this chart?",
                                     "text": "Q3", "target": {"rect": {"x": 1, "y": 2, "w": 30, "h": 40}}, "image": image})
        await self._until(lambda: ana_link.asks)
        ok, room = await luis.execute("ghost_room", {}, 10)
        ask = room["asks"][0]
        self.assertTrue(ask["image"])
        ok, got = await luis.execute("room_ask_image", {"id": ask["id"]}, 10)
        self.assertEqual(got["image"], image)
        ok, room = await ana.execute("ghost_room", {}, 10)
        self.assertNotIn("image", room["asks"][0])  # never sent to the relay
        with self.assertRaises(Exception):
            await ana.execute("room_ask_image", {"id": ask["id"]}, 10)

    async def test_follow_up_gets_the_same_picture_and_the_threads_text(self):
        await self._share()
        luis, luis_link, _ = self.machines["luis"]
        image = "data:image/jpeg;base64," + "B" * 2000
        await luis._extension_event({"type": "ask", "tab_id": 11, "url": SHEET, "question": "What is this chart?",
                                     "text": "Q3 by region", "target": {"rect": {"x": 1, "y": 2, "w": 30, "h": 40}}, "image": image})
        await self._until(lambda: luis_link.asks)
        first = next(iter(luis_link.asks.values()))
        # A reply from a card rebuilt after a reload: no text, no picture, just the thread.
        await luis._extension_event({"type": "ask", "tab_id": 11, "url": SHEET, "question": "and the west?", "thread": first["id"]})
        await self._until(lambda: len(luis_link.asks) == 2)
        follow = next(a for a in luis_link.asks.values() if a["id"] != first["id"])
        self.assertEqual((follow["thread"], follow["text"]), (first["id"], "Q3 by region"))
        self.assertEqual(follow["target"], first["target"])
        self.assertEqual(follow["turns"], [{"question": "What is this chart?", "answer": ""}])
        ok, room = await luis.execute("ghost_room", {}, 10)
        self.assertTrue(all(a["image"] for a in room["asks"]))
        ok, got = await luis.execute("room_ask_image", {"id": follow["id"]}, 10)
        self.assertEqual(got["image"], image)


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
