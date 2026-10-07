import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ghost_up
import native_host


class FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


ROOM = {"port": 9377, "room": "home-luis", "me": "luis", "room_url": "ws://127.0.0.1:9390", "name": "Luis"}


class SupervisorTests(unittest.TestCase):
    def make(self, config=ROOM, held=()):
        self.now = 0.0
        self.spawned = []

        def spawn(args):
            proc = FakeProc()
            self.spawned.append((args, proc))
            return proc
        sup = ghost_up.Supervisor(config, spawn=spawn, listening=lambda host, port: port in held,
                                  clock=lambda: self.now)
        return sup

    def test_starts_room_then_bridge(self):
        with mock.patch.object(native_host, "wait_listening", return_value=True):
            self.make().tick()
        names = [args[:2] for args, _ in self.spawned]
        self.assertEqual(names, [["room", "serve"], ["serve", "--port"]])
        bridge = self.spawned[1][0]
        self.assertIn("--room", bridge)
        self.assertNotIn("--allow-eval", bridge)

    def test_restarts_a_child_that_stops_with_growing_waits(self):
        with mock.patch.object(native_host, "wait_listening", return_value=True):
            sup = self.make()
            sup.tick()
            bridge = self.spawned[1][1]
            bridge.returncode = 1
            self.now = 1.0
            sup.tick()  # noticed; waits before trying again
            self.assertEqual(len(self.spawned), 2)
            self.now = 3.0
            sup.tick()
            self.assertEqual(len(self.spawned), 3)
            self.spawned[2][1].returncode = 1
            self.now = 4.0
            sup.tick()
            self.assertGreater(sup.backoff["bridge"], 1.0)

    def test_leaves_ports_held_by_someone_else_alone(self):
        with mock.patch.object(native_host, "wait_listening", return_value=True):
            self.make(held={9377, 9390}).tick()
        self.assertEqual(self.spawned, [])

    def test_the_relay_always_runs_here(self):
        # There is no remote room: a saved remote address still gets the local relay.
        config = {**ROOM, "room_url": "wss://rooms.example.com"}
        with mock.patch.object(native_host, "wait_listening", return_value=True):
            self.make(config).tick()
        self.assertIn(["room", "serve"], [args[:2] for args, _ in self.spawned])

    def test_stop_terminates_children(self):
        with mock.patch.object(native_host, "wait_listening", return_value=True):
            sup = self.make()
            sup.tick()
        sup.stop()
        self.assertTrue(all(proc.returncode == -15 for _, proc in self.spawned))


class FirstRunTests(unittest.TestCase):
    def test_fresh_install_gets_a_personal_room(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(native_host, "GHOST_DIR", Path(tmp)), \
             mock.patch.object(native_host, "CONFIG_PATH", Path(tmp) / "bridge.json"), \
             mock.patch("getpass.getuser", return_value="Ana María"):
            config = ghost_up.load_or_create_config()
            self.assertEqual(config["me"], "AnaMara")
            self.assertEqual(config["room"], "home-AnaMara")
            self.assertEqual(ghost_up.load_or_create_config(), config)  # saved and reused

    def test_saved_room_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(native_host, "GHOST_DIR", Path(tmp)), \
             mock.patch.object(native_host, "CONFIG_PATH", Path(tmp) / "bridge.json"):
            native_host.save_bridge_config(9377, "demo", "ws://127.0.0.1:9390", "luis", "Luis", "#3b82f6")
            self.assertEqual(ghost_up.load_or_create_config()["room"], "demo")


if __name__ == "__main__":
    unittest.main()
