"""Recorded selectors retain their actual root role, without targeting a namesake elsewhere."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import ghost_chat
from tests.test_automations import play_hub


class TargetBrowser:
    def __init__(self, line="button: Search", metadata=True):
        self.calls = []
        self.line, self.metadata = line, metadata

    async def __call__(self, command, args):
        self.calls.append((command, args))
        if command == "ghost_read":
            if args.get("selector"):
                value = {"content": f"[0] {self.line}"}
                if self.metadata:
                    value["target"] = {"choice": 0, "line": self.line}
                return True, value
            return True, {"content": "[0] link: Search (/wiki/Special:Search)\n[1] button: Search"}
        return True, {"ok": True}


def task_for(hub):
    return ghost_chat.Task(1, "Lookup", "Look up an article", "", "do", "parallel", "m",
                           hub.agent_for(7, "https://example.test/"))


def test_recorded_button_selector_beats_same_named_sidebar_link(tmp_path):
    async def main():
        browser = TargetBrowser()
        hub = play_hub(browser, tmp_path)
        task = task_for(hub)
        await hub.play_step(task, {"do": "click", "text": "Search", "css": "#searchform button"}, {}, dry=True)
        clicks = [a for c, a in browser.calls if c == "ghost_click"]
        assert clicks and clicks[0]["selector"] == "#searchform button"
        assert "choice" not in clicks[0]
        assert not any(c == "ghost_read" and not a.get("selector") for c, a in browser.calls)
    asyncio.run(main())


def test_css_only_search_field_enter_executes_in_rehearsal(tmp_path):
    async def main():
        browser = TargetBrowser("input(search): Search Wikipedia [REDACTED]")
        hub = play_hub(browser, tmp_path)
        out = await hub.play_step(task_for(hub), {"do": "key", "css": "#searchInput", "key": "Enter"}, {}, dry=True)
        assert out == ""
        assert any(c == "ghost_key" and a["selector"] == "#searchInput" for c, a in browser.calls)
    asyncio.run(main())


@pytest.mark.parametrize("line,metadata", [("input(password): Password", True), ("input(password): Password", False)])
def test_selector_type_cannot_bypass_password_guard(tmp_path, line, metadata):
    async def main():
        browser = TargetBrowser(line, metadata)
        hub = play_hub(browser, tmp_path)
        with pytest.raises(RuntimeError, match="passwords|confirm the selector's field type"):
            await hub.play_step(task_for(hub), {"do": "type", "css": "#secret", "value": "never-type"}, {}, dry=True)
        assert not any(c == "ghost_fill" for c, _ in browser.calls)
    asyncio.run(main())


def test_older_worker_without_root_metadata_does_not_infer_search_role(tmp_path):
    async def main():
        browser = TargetBrowser("input(search): Search", metadata=False)
        hub = play_hub(browser, tmp_path)
        out = await hub.play_step(task_for(hub), {"do": "key", "css": "#searchInput", "text": "Search", "key": "Enter"}, {}, dry=True)
        assert "skipped" in out
        assert not any(c == "ghost_key" for c, _ in browser.calls)
    asyncio.run(main())


def test_older_worker_named_type_fallback_preserves_observed_password_role(tmp_path):
    async def main():
        browser = TargetBrowser("input(password): Account password", metadata=False)
        hub = play_hub(browser, tmp_path)
        original = hub.call
        async def read_named(command, args):
            if command == "ghost_read" and not args.get("selector"):
                return True, {"content": "[5] input(password): Account password"}
            return await original(command, args)
        hub.call = read_named
        with pytest.raises(RuntimeError, match="never type passwords"):
            await hub.play_step(task_for(hub), {"do": "type", "css": "#password", "text": "Account password", "value": "never-type"}, {}, dry=True)
        assert not any(c == "ghost_fill" for c, _ in browser.calls)
    asyncio.run(main())


def test_selector_copy_keeps_full_container_and_never_selects_its_link(tmp_path):
    async def main():
        browser = TargetBrowser("link: First topic (/topic)")
        hub = play_hub(browser, tmp_path)
        original = browser.__call__
        async def call(command, args):
            if command == "ghost_read":
                browser.calls.append((command, args))
                return True, {"content": "Leading prose\n[0] link: First topic (/topic)\nTrailing prose", "target": None}
            return await original(command, args)
        hub.call = call
        values = {}
        await hub.play_step(task_for(hub), {"do": "copy", "css": "#article", "text": "First topic", "as": "intro"}, values)
        assert values["intro"] == "Leading prose\nFirst topic\nTrailing prose"
        assert [a["selector"] for c, a in browser.calls if c == "ghost_read"] == ["#article"]
    asyncio.run(main())


def run_js(program):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required to execute production page helpers")
    return json.loads(subprocess.run([node, "-e", program], check=True, text=True, capture_output=True).stdout)


def test_production_enumerator_reports_only_interactive_root_metadata():
    source = (Path(__file__).resolve().parents[1] / "extension/ghost_page.js").read_text()
    enumerate_source = source[source.index("  function enumerate("):source.index("  /** Find the element an actor means")]
    result = run_js("""
const Node = {ELEMENT_NODE: 1, TEXT_NODE: 3};
const lists = new Map(); let snapshotCounter = 0;
let root;
const deepQuery = () => root, checkActor = () => {}, fail = () => {throw Error('missing');};
const isVisible = () => true, isContainer = () => false;
const isInteractive = (n, tag) => ['input', 'button'].includes(tag);
const renderedChildren = n => n.children || [];
const selectorText = () => null;
const describe = (n, tag, number) => `[${number}] ${n.line}`;
""" + enumerate_source + """
root = {nodeType: 1, tagName: 'INPUT', line: 'input(search): Search', children: []};
const field = enumerate('a', 4000, '#field');
root = {nodeType: 1, tagName: 'DIV', children: [root]};
const container = enumerate('a', 4000, '#container');
process.stdout.write(JSON.stringify({field, container}));
""")
    assert result["field"]["target"] == {"choice": 0, "line": "input(search): Search"}
    assert result["container"]["target"] is None
    assert "input(search): Search" in result["container"]["text"]


def test_production_read_page_forwards_root_metadata():
    source = (Path(__file__).resolve().parents[1] / "extension/background.js").read_text()
    function = source[source.index("async function readPage("):source.index("\n}", source.index("async function readPage(")) + 2]
    result = run_js("""
const getActiveTabId = async () => 7, actorOf = () => 'a';
const readTabContent = async () => ({text: '[0] input(search): Search', snapshot: 'a-1', target: {choice: 0, line: 'input(search): Search'}});
const withSheetCells = async (tab, text) => text;
const chrome = {tabs: {get: async () => ({url: 'https://example.test/', title: 'Search'})}};
""" + function + "\nreadPage({selector:'#field'}).then(r => process.stdout.write(JSON.stringify(r)));" )
    assert result["target"] == {"choice": 0, "line": "input(search): Search"}
