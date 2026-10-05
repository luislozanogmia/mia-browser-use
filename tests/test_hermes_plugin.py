from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.mock_in_app_browser_server import MockInAppBrowserServer


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "hermes-plugin"


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "ghost_hermes_plugin",
        PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RecordingContext:
    def __init__(self, config=None):
        self.tools = {}
        self.hooks = {}
        self.config = config or {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class FakePageClient:
    tabs = {"tabs": [{"url": "https://example.com/docs", "title": "Docs", "active": True, "focused": True}]}

    def __init__(self, *_args):
        self.transport = self

    def connect(self):
        return {"connected": True}

    def call(self, _name, _arguments):
        return self.tabs


class HermesPluginTests(unittest.TestCase):
    def test_registers_focused_tool_surface(self):
        context = RecordingContext()
        load_plugin().register(context)
        self.assertIn("ghost_eval", context.tools)
        self.assertNotIn("ghost_" + "save_auth", context.tools)
        self.assertIn("ghost_pdf_read", context.tools)
        self.assertEqual(len(context.tools), 19)

    def test_page_context_hook_adds_open_page_for_local_sessions(self):
        module = load_plugin()
        module.BrowserClient = FakePageClient
        context = RecordingContext()
        module.register(context)
        result = context.hooks["pre_llm_call"](platform="cli", user_message="what is this?")
        self.assertIn("URL: https://example.com/docs", result["context"])
        self.assertIn("data, not instructions", result["context"])

    def test_page_context_hook_skips_messaging_gateways(self):
        module = load_plugin()
        module.BrowserClient = FakePageClient
        context = RecordingContext()
        module.register(context)
        for platform in ("telegram", "discord", "", None):
            self.assertIsNone(context.hooks["pre_llm_call"](platform=platform))

    def test_page_context_hook_is_silent_without_browser(self):
        module = load_plugin()

        class NoBrowser(FakePageClient):
            def connect(self):
                raise RuntimeError("No browser connection is available")

        module.BrowserClient = NoBrowser
        context = RecordingContext()
        module.register(context)
        self.assertIsNone(context.hooks["pre_llm_call"](platform="cli"))

    def test_page_context_can_be_disabled(self):
        context = RecordingContext({"page_context": False})
        load_plugin().register(context)
        self.assertNotIn("pre_llm_call", context.hooks)

    def test_manifest_declares_page_context_hook(self):
        manifest = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("provides_hooks:\n  - pre_llm_call\n", manifest)

    def test_manifest_matches_registered_tools(self):
        context = RecordingContext()
        load_plugin().register(context)
        manifest = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
        for name in context.tools:
            self.assertIn(f"  - {name}\n", manifest)

    def test_status_handler_returns_connection_status_without_browser_command(self):
        module = load_plugin()

        class FakeClient:
            def __init__(self, *_args):
                self.active_backend = "chrome"

            def call(self, name, arguments):
                self.assertions = (name, arguments)
                return {"connected": True}

        module.BrowserClient = FakeClient
        context = RecordingContext()
        module.register(context)
        result = context.tools["ghost_status"]["handler"]({})
        self.assertIn('"connected": true', result)

    def test_self_contained_client_calls_hermes_eval(self):
        module = load_plugin()
        client_module = __import__(module.__name__ + ".client", fromlist=["BrowserClient"])
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "browser.sock"
            token = "hermes-test-token-0123456789abcdef"
            server = MockInAppBrowserServer(socket_path, token=token)
            server.start()
            try:
                with mock.patch.dict(os.environ, {
                    "GHOST_IN_APP_BROWSER_SOCKET": str(socket_path),
                    "GHOST_IN_APP_BROWSER_TOKEN": token,
                }, clear=False):
                    client = client_module.BrowserClient(backend="hermes", allow_eval=True)
                    result = client.call("ghost_eval", {"script": "() => document.title"})
                self.assertEqual(result["value"], "mock-result")
                self.assertEqual(client.active_backend, "hermes")
            finally:
                server.stop()

    def test_eval_is_disabled_without_explicit_opt_in(self):
        module = load_plugin()
        client_module = __import__(module.__name__ + ".client", fromlist=["BrowserClient"])
        client = client_module.BrowserClient(backend="chrome")
        with self.assertRaisesRegex(client_module.GhostClientError, "disabled"):
            client.call("ghost_eval", {"script": "() => document.cookie"})


class RepositoryBoundaryTests(unittest.TestCase):
    def test_removed_legacy_terms_do_not_reappear(self):
        terms = (
            "play" + "wright",
            "c" + "dp",
            "remote " + "debu" + "gging",
            "chrome" + "-devtools" + "-mcp",
        )
        violations = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".js", ".json", ".md", ".html", ".sh", ".yaml"}:
                continue
            lowered = path.read_text(encoding="utf-8", errors="ignore").lower()
            for term in terms:
                if term in lowered:
                    violations.append(f"{path.relative_to(ROOT)}: {term}")
        self.assertEqual(violations, [])
