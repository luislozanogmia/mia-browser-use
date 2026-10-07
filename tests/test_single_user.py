"""Mia Browser is for one person on one computer: no multi-user surface anywhere.

The local relay stays as the internal path that carries questions and answers to
the page; everything that existed for other people or other machines is gone.
"""

import asyncio
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bridge_server import BridgeServer, ROOM_COMMANDS
from ghost_chat import Agent
from room_link import RoomLink

EXT = REPO_ROOT / "extension"


class FakeRoom:
    def __init__(self):
        self.sent = []
        self.connected = True
        self.shared = {}

    def is_shared(self, url):
        return True

    async def send(self, message):
        self.sent.append(message)

    async def ensure_bot(self, actor, look=None):
        pass


class ExtensionSourceTests(unittest.TestCase):
    def test_every_open_tab_is_shared_by_itself_and_unshared_when_it_closes(self):
        source = (EXT / "background.js").read_text()
        self.assertIn("function shareTab(tab)", source)
        self.assertIn("function leavePage(tabId, key)", source)
        self.assertIn("chrome.tabs.onRemoved.addListener(tabId => {", source)
        for gone in ("followActiveTab", "setFollow", "manuallyShared", "stoppedByHuman", '"share-tab"', 'type === "follow"',
                     "human_presence\"", "roomMe"):
            self.assertNotIn(gone, source, gone)

    def test_side_panel_has_no_room_sharing_or_follow_me(self):
        html = (EXT / "sidepanel.html").read_text()
        js = (EXT / "sidepanel.js").read_text()
        for gone in ("shareBtn", "unshareBtn", "followMe", "roomLabel", "room-pill", "faces"):
            self.assertNotIn(gone, html, gone)
            self.assertNotIn(gone, js, gone)
        for kept in ("reelMode", "immersive", "skipPrompt", "openReel", "language"):
            self.assertIn(kept, html, kept)

    def test_page_script_only_wires_the_ask_box(self):
        source = (EXT / "human_presence.js").read_text()
        self.assertIn('type: "ask"', source)
        self.assertNotIn("pointermove", source)
        self.assertNotIn("human_presence", source.split("\n", 8)[-1])  # no presence messages sent

    def test_bots_are_not_named_after_an_owner(self):
        source = (EXT / "overlay.js").read_text()
        self.assertNotIn("${owner}'s", source)
        self.assertNotIn("owner_color", source.split("function makeNodes")[1].split("const list = layer()")[0])


class BridgeTests(unittest.TestCase):
    def test_the_bridge_sends_no_presence_and_knows_no_other_people(self):
        bridge = BridgeServer(token="t" * 64)
        bridge.room = FakeRoom()
        asyncio.run(bridge._extension_event({"type": "human", "tab_id": 1, "url": "https://a.example/",
                                             "status": "working", "focus": None, "pointer": None}))
        asyncio.run(bridge._extension_event({"type": "resolve", "id": "s1", "decision": "accept"}))
        asyncio.run(bridge._announce("ghost_click", {"actor_id": "mia-1", "tab_id": 1}, "c1", "done",
                                     {"anchor": {"selector": "#x"}}))
        self.assertEqual(bridge.room.sent, [])
        self.assertEqual(bridge._chat_room(), {"connected": True})
        self.assertNotIn("room_share", ROOM_COMMANDS)
        self.assertNotIn("room_resolve", ROOM_COMMANDS)

    def test_a_tab_share_still_reaches_the_relay(self):
        bridge = BridgeServer(token="t" * 64)
        bridge.room = FakeRoom()
        asyncio.run(bridge._extension_event({"type": "share", "tab_id": 1, "url": "https://a.example/p", "title": "A"}))
        asyncio.run(bridge._extension_event({"type": "unshare", "url": "https://a.example/p"}))
        self.assertEqual([m["action"] for m in bridge.room.sent], ["share", "unshare"])

    def test_bot_ids_carry_no_person(self):
        self.assertEqual(Agent(1, 5).id, "mia-1")

    def test_the_relay_is_local_only(self):
        me = {"id": "luis", "kind": "human"}
        RoomLink("ws://127.0.0.1:9390", "home", "k" * 40, me, on_message=None)
        for remote in ("wss://rooms.example.com", "ws://10.0.0.5:9390"):
            with self.assertRaises(ValueError):
                RoomLink(remote, "home", "k" * 40, me, on_message=None)


if __name__ == "__main__":
    unittest.main()
