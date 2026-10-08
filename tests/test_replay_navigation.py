"""Explicit replay opens reset same-URL document state and still honor the viewing guard."""
import asyncio
from pathlib import Path

from ghost_chat import Agent, Task
from tests.test_automations import PlayBrowser, play_hub
from tests.test_selector_target import run_js


def test_production_navigation_only_reloads_when_explicit_and_guarded():
    source = (Path(__file__).resolve().parents[1] / "extension/background.js").read_text()
    start = source.index("async function navigate(")
    function = source[start:source.index("\n}", start) + 2]
    result = run_js("""
let events = [], deny = false;
const url = 'https://example.test/list';
const isSheetExport = () => false, isActorCall = () => true;
const refuseIfHumanViewing = async () => { events.push('guard'); if (deny) throw Error('HUMAN_VIEWING'); };
const waitForTabLoad = async id => { events.push('load:' + id); };
const chrome = {tabs: {
 get: async id => ({id, url, title:'List'}),
 reload: async id => {events.push('reload:' + id);},
 update: async (id, args) => {events.push('update:' + args.url);}
}};
""" + function + """
(async () => {
 const results = [];
 for (const [args, blocked] of [
   [{url, tab_id:7}, false],
   [{url, tab_id:7, reload:'true'}, false],
   [{url, tab_id:7, reload:true}, false],
   [{url, tab_id:7, reload:true}, true],
   [{url:url+'?page=2', tab_id:7}, false],
   [{url:url+'?page=2', tab_id:7, reload:true}, true]
 ]) {
   events = []; deny = blocked;
   let error = '';
   try { await navigate(args); } catch (e) { error = e.message; }
   results.push({events, error});
 }
 process.stdout.write(JSON.stringify(results));
})();
""")
    assert result[0] == result[1] == {"events": [], "error": ""}
    assert result[2] == {"events": ["guard", "reload:7", "load:7"], "error": ""}
    assert result[3] == {"events": ["guard"], "error": "HUMAN_VIEWING"}
    assert result[4] == {"events": ["guard", "update:https://example.test/list?page=2", "load:7"], "error": ""}
    assert result[5] == {"events": ["guard"], "error": "HUMAN_VIEWING"}


def test_replay_open_passes_reload_to_existing_tab_without_changing_other_navigation(tmp_path):
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser, tmp_path)
        task = Task(1, "Replay", "Replay the list", "", "play", "parallel", "", Agent(1, 91))
        url = "https://example.test/list"
        await hub.play_step(task, {"do": "open", "url": url}, {})
        replay = next(args for command, args in browser.calls if command == "ghost_navigate")
        assert replay == {"url": url, "reload": True, "tab_id": 91, "actor_id": task.agent.id,
                          "human_ok": True, "expected_url": ""}
        browser.calls.clear()
        await hub.play_call(task, "ghost_navigate", {"url": url})
        ordinary = next(args for command, args in browser.calls if command == "ghost_navigate")
        assert "reload" not in ordinary
    asyncio.run(main())
