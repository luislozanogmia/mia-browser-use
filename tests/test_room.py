"""
Tests for the Ghost room relay (who is working on what, across machines).

Run:
    python -m pytest tests/test_room.py -v
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

from ghost_room import MAX_MESSAGE_BYTES, RoomHub, page_key, serve_room

KEY = "k" * 40
SHEET = "https://docs.google.com/spreadsheets/d/abc/edit#gid=0"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def of_type(outbound, kind):
    return [(conn, msg) for conn, msg in outbound if msg.get("type") == kind]


class RoomHubTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hub = RoomHub(clock=self.clock)
        self.hub.store.create_room("demo", KEY)

    def join(self, conn, actor_id, kind="human", key=KEY, **extra):
        return self.hub.handle(conn, {"action": "join", "room": "demo", "key": key, "actor": {"id": actor_id, "kind": kind, **extra}})

    def share(self, conn, url=SHEET):
        return self.hub.handle(conn, {"action": "share", "page": {"url": url, "title": "Q4 budget"}})

    def test_page_key_ignores_query_and_fragment(self):
        self.assertEqual(page_key(SHEET), "https://docs.google.com/spreadsheets/d/abc/edit")
        self.assertEqual(page_key("https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing"), page_key(SHEET))
        self.assertIsNone(page_key("chrome://settings"))
        self.assertIsNone(page_key("file:///etc/passwd"))

    def test_wrong_key_and_unknown_room_fail_the_same_way(self):
        wrong = self.join("a", "luis", key="x" * 40)
        self.assertEqual(wrong[0][1]["code"], "AUTH_FAILED")
        unknown = self.hub.handle("b", {"action": "join", "room": "nope", "key": KEY, "actor": {"id": "luis"}})
        self.assertEqual(unknown[0][1]["code"], "AUTH_FAILED")
        self.assertEqual(self.hub.handle("a", {"action": "share", "page": {"url": SHEET}})[0][1]["code"], "NOT_JOINED")

    def test_join_returns_snapshot_and_announces_member(self):
        self.join("a", "luis")
        self.share("a")
        out = self.join("b", "ana", color="#ef4444")
        snapshot = out[0][1]
        self.assertEqual(snapshot["type"], "joined")
        self.assertEqual([p["url"] for p in snapshot["pages"]], [page_key(SHEET)])
        self.assertEqual(of_type(out, "member")[0][0], "a")

    def test_bot_presence_reaches_everyone_else_with_its_owner(self):
        self.join("a", "luis")
        self.join("b", "ana", color="#ef4444")
        self.share("a")
        self.hub.handle("b", {"action": "actor", "actor": {"id": "ledger", "kind": "bot", "owner": "ana", "color": "#3b82f6", "owner_color": "#ef4444"}})
        out = self.hub.handle("b", {"action": "presence", "actor_id": "ledger", "url": SHEET, "label": "Ledger · Q3 actuals",
                                    "target": {"range": "B2:B6", "rect": {"x": 1, "y": 2, "w": 3, "h": 4}}})
        self.assertEqual([conn for conn, _ in out], ["a"])
        presence = out[0][1]
        self.assertEqual(presence["actor"]["owner"], "ana")
        self.assertEqual(presence["target"]["range"], "B2:B6")
        # Someone joining later sees the bot where it is.
        late = self.join("c", "diego")
        self.assertEqual(late[0][1]["presence"][0]["actor"]["id"], "ledger")

    def test_presence_only_for_shared_pages(self):
        self.join("a", "luis")
        out = self.hub.handle("a", {"action": "presence", "actor_id": "luis", "url": "https://mail.google.com/mail/u/0/"})
        self.assertEqual(out[0][1]["code"], "PAGE_NOT_SHARED")

    def test_cannot_speak_for_someone_elses_actor(self):
        self.join("a", "luis")
        self.join("b", "ana")
        self.share("a")
        out = self.hub.handle("b", {"action": "presence", "actor_id": "luis", "url": SHEET})
        self.assertEqual(out[0][1]["code"], "NOT_YOUR_ACTOR")
        taken = self.hub.handle("b", {"action": "actor", "actor": {"id": "luis", "kind": "bot"}})
        self.assertEqual(taken[0][1]["code"], "ACTOR_TAKEN")

    def test_presence_expires_and_leaving_clears_it(self):
        self.join("a", "luis")
        self.share("a")
        self.hub.handle("a", {"action": "presence", "actor_id": "luis", "url": SHEET, "ttl_ms": 2000})
        self.clock.now += 3
        self.assertEqual(self.join("b", "ana")[0][1]["presence"], [])
        self.hub.handle("a", {"action": "presence", "actor_id": "luis", "url": SHEET})
        left = self.hub.disconnect("a")
        self.assertEqual(left[0], ("b", {"type": "member", "event": "left", "actor": {"id": "luis", "kind": "human", "name": "luis"}}))
        self.assertEqual(self.join("c", "diego")[0][1]["presence"], [])

    def test_untrusted_fields_are_cleaned(self):
        self.join("a", "luis")
        self.join("b", "ana")
        self.share("a")
        out = self.hub.handle("a", {"action": "presence", "actor_id": "luis", "url": SHEET, "label": "x\n" * 200,
                                    "target": {"rect": {"x": "1", "y": 2, "w": 3, "h": 4}, "evil": True},
                                    "pointer": {"anchor": {"selector": "#c3"}, "fx": 7, "fy": -1}})
        presence = out[0][1]
        self.assertNotIn("\n", presence["label"])
        self.assertLessEqual(len(presence["label"]), 80)
        self.assertIsNone(presence["target"])
        self.assertEqual(presence["pointer"], {"anchor": {"selector": "#c3"}, "fx": 1.0, "fy": 0.0})

    def test_activity_events_carry_actor_and_status(self):
        self.join("a", "luis")
        self.join("b", "ana")
        self.share("a")
        out = self.hub.handle("a", {"action": "activity", "actor_id": "luis", "url": SHEET, "call_id": "c1",
                                    "op": "fill", "status": "failed", "error": "TAB_CLOSED: gone"})
        event = out[0][1]
        self.assertEqual((event["type"], event["op"], event["status"], event["error"]), ("activity", "fill", "failed", "TAB_CLOSED: gone"))
        bad = self.hub.handle("a", {"action": "activity", "actor_id": "luis", "url": SHEET, "status": "exploded"})
        self.assertEqual(bad[0][1]["code"], "INVALID")

    def test_only_humans_resolve_suggestions(self):
        self.join("a", "luis")
        self.join("b", "ana")
        self.share("a")
        self.hub.handle("b", {"action": "actor", "actor": {"id": "ledger", "kind": "bot", "owner": "ana"}})
        made = self.hub.handle("b", {"action": "suggest", "actor_id": "ledger", "url": SHEET, "id": "s1",
                                     "title": "Net 45", "body": "Matches Acme's AP cycle", "target": {"text": "within 30 days"}})
        self.assertEqual(len(of_type(made, "suggestion")), 2)
        resolved = self.hub.handle("a", {"action": "resolve", "id": "s1", "decision": "accept"})
        self.assertEqual(resolved[0][1]["by"]["id"], "luis")
        again = self.hub.handle("a", {"action": "resolve", "id": "s1", "decision": "reject"})
        self.assertEqual(again[0][1]["code"], "NOT_FOUND")

    def test_rate_limit(self):
        self.join("a", "luis")
        self.share("a")
        codes = [self.hub.handle("a", {"action": "presence", "actor_id": "luis", "url": SHEET}) for _ in range(60)]
        self.assertTrue(any(out and out[0][1].get("code") == "RATE_LIMITED" for out in codes))

    def test_oversized_and_malformed_messages(self):
        self.assertEqual(self.hub.handle_raw("a", "x" * (MAX_MESSAGE_BYTES + 1))[0][1]["code"], "TOO_LARGE")
        self.assertEqual(self.hub.handle_raw("a", "{nope")[0][1]["code"], "INVALID")
        self.assertEqual(self.hub.handle("a", {"action": "__init__"})[0][1]["code"], "UNKNOWN_ACTION")


class RoomServerTests(unittest.TestCase):
    """Two machines talk through the real WebSocket server."""

    def test_presence_crosses_connections(self):
        async def scenario():
            from websockets.asyncio.client import connect

            hub = RoomHub()
            hub.store.create_room("demo", KEY)
            server = await serve_room(hub, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                async with connect(f"ws://127.0.0.1:{port}") as luis, connect(f"ws://127.0.0.1:{port}") as ana:
                    await luis.send(json.dumps({"action": "join", "room": "demo", "key": KEY, "actor": {"id": "luis", "kind": "human"}}))
                    self.assertEqual(json.loads(await luis.recv())["type"], "joined")
                    await luis.send(json.dumps({"action": "share", "page": {"url": SHEET}}))
                    await luis.recv()
                    await ana.send(json.dumps({"action": "join", "room": "demo", "key": KEY, "actor": {"id": "ana", "kind": "human"}}))
                    self.assertEqual(json.loads(await ana.recv())["type"], "joined")
                    self.assertEqual(json.loads(await luis.recv())["type"], "member")
                    await ana.send(json.dumps({"action": "presence", "actor_id": "ana", "url": SHEET, "target": {"selector": "#C3"}}))
                    got = json.loads(await asyncio.wait_for(luis.recv(), 5))
                    self.assertEqual((got["type"], got["actor"]["id"], got["target"]["selector"]), ("presence", "ana", "#C3"))
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
