import io
import os
import json
import struct
import unittest
from pathlib import Path
from unittest import mock

import native_host


def framed(message):
    data = json.dumps(message).encode()
    return io.BytesIO(struct.pack("<I", len(data)) + data)


class NativeHostTests(unittest.TestCase):
    def test_pair_returns_token_over_framed_stdio(self):
        out = io.BytesIO()
        with mock.patch.object(native_host, "load_bridge_token", return_value="t" * 64), \
             mock.patch.object(native_host, "load_bridge_config", return_value={"port": 9377}), \
             mock.patch("ghost_up.load_or_create_config", return_value={"port": 9377}), \
             mock.patch.object(native_host, "ensure_up", return_value="running"):
            native_host.write_message(out, native_host.answer(native_host.read_message(framed({"type": "pair"}))))
        raw = out.getvalue()
        (length,) = struct.unpack("<I", raw[:4])
        reply = json.loads(raw[4:4 + length])
        self.assertEqual((reply["ok"], reply["token"], reply["port"]), (True, "t" * 64, 9377))

    def test_other_requests_get_no_token(self):
        with mock.patch.object(native_host, "load_bridge_token", return_value="t" * 64):
            self.assertNotIn("token", native_host.answer({"type": "dump"}))
            self.assertNotIn("token", native_host.answer(None))

    def test_oversized_and_garbage_input_is_ignored(self):
        self.assertIsNone(native_host.read_message(io.BytesIO(struct.pack("<I", 10**6))))
        self.assertIsNone(native_host.read_message(io.BytesIO(struct.pack("<I", 3) + b"abc")))
        self.assertIsNone(native_host.read_message(io.BytesIO(b"")))

    def test_extension_id_matches_chromes_unpacked_rule(self):
        # Chrome: first 32 hex chars of sha256(path), each mapped 0-f -> a-p.
        ext_id = native_host.extension_id(Path("/tmp"))
        self.assertEqual(len(ext_id), 32)
        self.assertTrue(set(ext_id) <= set("abcdefghijklmnop"))

    def test_manifest_only_allows_this_extension(self):
        with mock.patch.object(native_host.Path, "home", return_value=Path(self.tmp)), \
             mock.patch.object(native_host, "manifest_dirs", return_value=[Path(self.tmp) / "hosts"]):
            info = native_host.install_native_host(Path(self.tmp), python="/usr/bin/python3")
        manifest = json.loads(Path(info["manifests"][0]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{info['extension_id']}/"])
        self.assertEqual(manifest["name"], "com.ghost.bridge")
        if os.name != "nt":  # Windows has no POSIX mode bits
            self.assertEqual(Path(info["launcher"]).stat().st_mode & 0o777, 0o700)

    def test_pair_starts_the_supervisor_once(self):
        with mock.patch.object(native_host, "GHOST_DIR", Path(self.tmp)), \
             mock.patch.object(native_host, "LOCK_PATH", Path(self.tmp) / "lock"), \
             mock.patch.object(native_host, "is_listening", return_value=False), \
             mock.patch.object(native_host, "wait_listening", return_value=True), \
             mock.patch("ghost_up.up_running", return_value=False), \
             mock.patch.object(native_host, "spawn") as spawn:
            self.assertEqual(native_host.ensure_up({"port": 9377}), "started")
        spawn.assert_called_once_with(["up"])
        with mock.patch.object(native_host, "LOCK_PATH", Path(self.tmp) / "lock"), \
             mock.patch("ghost_up.up_running", return_value=True), \
             mock.patch.object(native_host, "spawn") as spawn:
            self.assertEqual(native_host.ensure_up({"port": 9377}), "running")
        spawn.assert_not_called()

    def test_saved_config_drops_bad_values(self):
        config = native_host.clean_bridge_config({
            "port": 80, "room": "demo; rm -rf /", "me": "luis", "allow_eval": True,
        })
        self.assertEqual(config, {"port": 9377})
        config = native_host.clean_bridge_config({
            "port": 9377, "room": "demo", "me": "luis", "color": "red", "name": "Luis\n--allow-eval",
            "room_url": "http://evil",
        })
        self.assertEqual(config, {"port": 9377, "room": "demo", "me": "luis"})

    def test_saved_config_round_trips(self):
        with mock.patch.object(native_host, "GHOST_DIR", Path(self.tmp)), \
             mock.patch.object(native_host, "CONFIG_PATH", Path(self.tmp) / "bridge.json"):
            native_host.save_bridge_config(9377, "demo", "ws://127.0.0.1:9390", "luis", "Luis", "#3b82f6")
            self.assertEqual(native_host.load_bridge_config()["room"], "demo")
            if os.name != "nt":
                self.assertEqual((Path(self.tmp) / "bridge.json").stat().st_mode & 0o777, 0o600)

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = self._dir.name

    def tearDown(self):
        self._dir.cleanup()


if __name__ == "__main__":
    unittest.main()
