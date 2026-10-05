import asyncio
import json
import os
from datetime import datetime

import pytest

import automations
import ghost_chat
from automations import AutomationStore, clean
from ghost_chat import ChatHub, element_label, match_element
from tests.test_chat import FakeSession, make_hub, send

PAGE = ("Report\n[0] link: Home (/)\n[1] input(text): Search reports\n[2] button: Export\n"
        "[3] button: Send report\n[4] input(password): Password\nTotal: 42")
SCRIPT = {"name": "Daily report", "about": "Exports the report", "schedule": {"kind": "manual"}, "steps": [
    {"do": "open", "url": "https://reports.example/"},
    {"do": "copy", "css": "#total", "as": "total"},
    {"do": "type", "text": "Search reports", "value": "total {{total}}"},
    {"do": "click", "css": "#export", "text": "Export"},
]}


class PlayBrowser:
    """A browser for replays: a fixed page, a #total element, and every call recorded."""

    def __init__(self, missing=()):
        self.calls = []
        self.missing = set(missing)

    async def __call__(self, command, args):
        self.calls.append((command, args))
        if command == "ghost_tab_open":
            return True, {"id": 91, "url": args["url"]}
        if command == "ghost_read" and args.get("selector") == "#total":
            return True, {"url": "https://reports.example/", "content": "[0] span: 42 open items"}
        if command == "ghost_read":
            return True, {"url": "https://reports.example/", "title": "Report", "content": PAGE}
        if command == "ghost_wait" and args.get("selector") in self.missing:
            return False, "Timeout"
        if command == "ghost_click":
            return True, {"clicked": True, "anchor": {"selector": "#export", "text": "Export"}}
        return True, {"ok": True}

    def actions(self):
        return [(c, a) for c, a in self.calls if c not in {"ghost_show", "ghost_wait", "ghost_read"}]


def play_hub(browser, tmp_path=None):
    async def push(state):
        hub.states.append(state)

    hub = ChatHub(browser, push, plan=lambda model, prompt: None,
                  scripts=AutomationStore(tmp_path / "automations.json" if tmp_path else None))
    hub.states = []
    hub.claude_status = lambda fresh=False: {"installed": True, "signed_in": True}
    return hub


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(ghost_chat, "PLAY_RETRIES", (0, 0, 0))


async def finish(task, stop_for_questions=True):
    for _ in range(300):
        if task.job.done() or (stop_for_questions and task.status == "needs_you"):
            return
        await asyncio.sleep(0.01)


# -- the script format --------------------------------------------------------------

def test_clean_keeps_a_valid_script_and_describes_it():
    item = clean(SCRIPT)
    assert [s["do"] for s in item["steps"]] == ["open", "copy", "type", "click"]
    words = [automations.describe_step(s) for s in item["steps"]]
    assert words == ["Open https://reports.example/", "Copy the element at #total as {{total}}",
                     "Type “total {{total}}” into “Search reports”", "Click “Export”"]
    assert automations.describe_schedule(item["schedule"]) == "Runs when you press Play"


@pytest.mark.parametrize("change, reason", [
    ({"steps": [{"do": "click", "text": "Export"}]}, "first step must open"),
    ({"steps": [{"do": "open", "url": "https://a.example/"}, {"do": "type", "text": "x", "value": "{{nope}}"}]}, "{{nope}}"),
    ({"steps": [{"do": "open", "url": "javascript:alert(1)"}]}, "no steps"),
    ({"name": ""}, "name"),
])
def test_clean_refuses_scripts_it_cannot_run(change, reason):
    with pytest.raises(ValueError, match=reason.replace("{", r"\{").replace("}", r"\}")):
        clean({**SCRIPT, **change})


def test_clean_drops_unknown_steps_and_bounds_waits():
    item = clean({**SCRIPT, "steps": [SCRIPT["steps"][0], {"do": "eval", "script": "x"}, {"do": "wait", "ms": 999999},
                                      {"do": "click"}]})
    assert item["steps"][1:] == [{"do": "wait", "ms": 10000}]


def test_store_is_private_and_replaces_by_name(tmp_path):
    store = AutomationStore(tmp_path / "automations.json")
    first = store.add(clean(SCRIPT))
    second = store.add(clean({**SCRIPT, "about": "Better"}))
    assert first["id"] == second["id"] and len(store.list()) == 1
    assert oct(os.stat(tmp_path / "automations.json").st_mode & 0o777) == "0o600"
    assert AutomationStore(tmp_path / "automations.json").get(first["id"])["about"] == "Better"
    assert store.delete(first["id"]) and not store.list()


def test_schedule_runs_once_a_day_within_its_window():
    store = AutomationStore()
    item = store.add(clean({**SCRIPT, "schedule": {"kind": "daily", "at": "09:00"}}), now=datetime(2026, 10, 5, 8, 0))
    assert store.due(datetime(2026, 10, 5, 8, 59)) == []
    assert store.due(datetime(2026, 10, 5, 9, 0)) == [item]
    assert store.due(datetime(2026, 10, 5, 9, 5)) == []  # already ran today
    assert store.due(datetime(2026, 10, 6, 10, 0)) == []  # missed by over half an hour
    assert store.due(datetime(2026, 10, 7, 9, 10)) == [item]
    store.update(item["id"], paused=True)
    assert store.due(datetime(2026, 10, 8, 9, 0)) == []


def test_a_new_schedule_does_not_fire_for_a_time_already_past_today():
    store = AutomationStore()
    store.add(clean({**SCRIPT, "schedule": {"kind": "weekdays", "at": "9:00"}}), now=datetime(2026, 10, 5, 9, 10))
    assert store.due(datetime(2026, 10, 5, 9, 11)) == []
    assert store.due(datetime(2026, 10, 10, 9, 0)) == []  # Saturday
    assert len(store.due(datetime(2026, 10, 12, 9, 0))) == 1  # Monday


def test_elements_are_matched_by_their_visible_name():
    elements = {0: "link: Home (/)", 2: "button: Export", 3: "button: Export all"}
    assert element_label("link: Home (/)") == "Home"
    assert match_element(elements, "export") == 2
    assert match_element(elements, "Export a") == 3
    assert match_element(elements, "Import") is None


# -- replaying ---------------------------------------------------------------------

def test_play_replays_steps_in_its_own_tab_with_copied_values(tmp_path):
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser, tmp_path)
        item = hub.scripts.add(clean(SCRIPT))
        task = await hub.play(item)
        await finish(task)
        assert task.status == "done", task.result
        assert browser.actions() == [
            ("ghost_tab_open", {"url": "https://reports.example/", "actor_id": task.agent.id}),
            ("ghost_fill", {"choice": 1, "value": "total 42 open items", "tab_id": 91, "actor_id": task.agent.id,
                            "human_ok": True, "expected_url": ""}),
            ("ghost_click", {"choice": 2, "tab_id": 91, "actor_id": task.agent.id, "human_ok": True, "expected_url": ""}),
        ]
        assert hub.scripts.get(item["id"])["last_run"]["status"] == "done"
        assert hub.messages[-1]["text"].startswith("▶ Daily report · Done.")
    asyncio.run(main())


def test_play_falls_back_to_the_recorded_selector():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        task = await hub.play(hub.scripts.add(clean({**SCRIPT, "steps": [
            SCRIPT["steps"][0], {"do": "click", "css": "#new-button", "text": "Renamed button"}]})))
        await finish(task)
        assert task.status == "done", task.result
        assert ("ghost_click", {"selector": "#new-button", "tab_id": 91, "actor_id": task.agent.id,
                                "human_ok": True, "expected_url": ""}) in browser.actions()
    asyncio.run(main())


def test_play_stops_with_the_step_that_failed():
    async def main():
        browser = PlayBrowser(missing={"#gone"})
        hub = play_hub(browser)
        task = await hub.play(hub.scripts.add(clean({**SCRIPT, "steps": [
            SCRIPT["steps"][0], {"do": "click", "css": "#gone", "text": "Nowhere"}]})))
        await finish(task)
        assert task.status == "failed"
        assert task.result.startswith("Step 2 (Click “Nowhere”) didn't work")
        assert "ghost_click" not in [c for c, _ in browser.actions()]
    asyncio.run(main())


def test_play_asks_before_a_risky_click_and_never_types_passwords():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        task = await hub.play(hub.scripts.add(clean({**SCRIPT, "steps": [
            SCRIPT["steps"][0], {"do": "click", "text": "Send report"}]})))
        await finish(task)
        assert task.status == "needs_you" and "Send report" in task.question
        assert "ghost_click" not in [c for c, _ in browser.actions()]
        await hub.handle({"action": "reject", "task": task.id})
        await finish(task, stop_for_questions=False)
        assert task.status == "failed" and "rejected" in task.result

        task = await hub.play(hub.scripts.add(clean({**SCRIPT, "name": "Sign in", "steps": [
            SCRIPT["steps"][0], {"do": "type", "text": "Password", "value": "hunter2"}]})))
        await finish(task)
        assert task.status == "failed" and "passwords" in task.result
        assert "ghost_fill" not in [c for c, _ in browser.actions()]
    asyncio.run(main())


def test_panel_runs_pauses_and_deletes_automations():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        item = hub.scripts.add(clean({**SCRIPT, "schedule": {"kind": "daily", "at": "09:00"}}))
        await hub.handle({"action": "automation_pause", "automation": item["id"]})
        shown = hub.states[-1]["automations"][0]
        assert shown["paused"] and shown["scheduled"] and shown["steps"][0] == "Open https://reports.example/"
        await hub.handle({"action": "play", "automation": item["id"]})
        task = next(iter(hub.tasks.values()))
        await finish(task)
        assert task.kind == "play" and task.status == "done"
        await hub.handle({"action": "automation_delete", "automation": item["id"]})
        assert hub.states[-1]["automations"] == []
    asyncio.run(main())


# -- teaching: Mia does it once, and what she did becomes the script ------------------

def test_a_save_as_task_records_its_steps_and_saves_them():
    async def main():
        hub, browser, states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {}},
            {"tool": "ghost_fill", "args": {"choice": 0, "value": "weekly"}},
            {"tool": "ghost_click", "args": {"choice": 3}},
            {"done": "Opened the next page."},
        ]], plan={"reply": "I'll do it once and save it.", "tasks": [{
            "title": "Next page", "goal": "Search weekly, then go to the next page", "url": "", "kind": "ask",
            "save_as": {"name": "Weekly next", "schedule": {"kind": "daily", "at": "08:30"}}}]})
        await hub.handle(send("make an automation that searches weekly and opens the next page"))
        task = next(iter(hub.tasks.values()))
        for _ in range(100):
            if task.status == "needs_you":
                await hub.handle({"action": "approve", "task": task.id})
            if task.job.done():
                break
            await asyncio.sleep(0.01)
        assert task.kind == "do" and task.status == "done", task.result
        saved = hub.scripts.find("Weekly next")
        assert saved["schedule"] == {"kind": "daily", "at": "08:30"}
        assert saved["steps"] == [
            {"do": "open", "url": "https://mail.example/"},
            {"do": "type", "text": "Home", "value": "weekly"},
            {"do": "click", "text": "Next page"},
        ]
        assert any(m["text"].startswith("Saved “Weekly next” as a Play Automation: 3 steps, every day at 08:30")
                   for m in hub.messages)
    asyncio.run(main())


def test_mia_can_save_and_run_an_automation_from_the_chat():
    async def main():
        hub, browser, states, sessions = make_hub([], plan={"reply": "Saved.", "tasks": [], "automation": SCRIPT})
        await hub.handle(send("save that as an automation"))
        assert hub.scripts.find("Daily report")
        assert "Saved “Daily report”" in hub.messages[-1]["text"]

        async def planner(model, prompt):
            assert "Saved Play Automations:\n- Daily report: Exports the report" in prompt
            return json.dumps({"reply": "Running it.", "tasks": [], "run_automation": "daily report"})
        hub.plan_run = planner
        await hub.handle(send("run my daily report"))
        task = next(t for t in hub.tasks.values() if t.kind == "play")
        assert task.automation == hub.scripts.find("Daily report")["id"]
        task.job.cancel()
    asyncio.run(main())


def test_a_bad_script_from_mia_is_explained_not_saved():
    async def main():
        hub, *_ = make_hub([], plan={"reply": "", "tasks": [], "automation": {"name": "X", "steps": [
            {"do": "click", "text": "Go"}]}})
        await hub.handle(send("save it"))
        assert not hub.scripts.list()
        assert "first step must open a page" in hub.messages[-1]["text"]
    asyncio.run(main())
