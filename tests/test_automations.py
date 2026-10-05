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


# -- going through a list, with the person's own text --------------------------------

RESULTS = ("People\n[0] link: Ana Silva (https://www.linkedin.com/in/ana-silva?mini=1)\n"
           "[1] link: Bo Chen (/in/bo-chen/)\n[2] link: Jobs (https://www.linkedin.com/jobs/)\n"
           "[3] link: Ana Silva (https://www.linkedin.com/in/ana-silva?mini=2)\n[4] button: Next")
PROFILE = "Profile\n[0] h1: {name}\n[1] button: Message\n[2] div: Write a message…\n[3] button: Send"
LOOP = {"name": "1st connection message", "schedule": {"kind": "manual"},
        "inputs": [{"name": "template", "label": "Message"}],
        "each": {"links": "linkedin.com/in/", "next": "Next"},
        "steps": [
            {"do": "open", "url": "https://www.linkedin.com/search/results/people/?network=F"},
            {"do": "open", "url": "{{link}}"},
            {"do": "copy", "text": "Ana Silva", "css": "h1", "as": "first_name", "words": 1},
            {"do": "click", "text": "Message"},
            {"do": "type", "text": "Write a message", "value": "{{template}}"},
        ]}


class ListBrowser(PlayBrowser):
    """A results page with two people on page 1, one more on page 2, then no more."""

    def __init__(self):
        super().__init__()
        self.url, self.page = "", 1

    async def __call__(self, command, args):
        self.calls.append((command, args))
        if command in {"ghost_tab_open", "ghost_navigate"}:
            self.url = args["url"]
            if "search" in self.url and command == "ghost_navigate":
                pass  # back to the list: same page as before
            return True, {"id": 91, "url": args["url"]}
        if command == "ghost_click" and args.get("choice") == 4 and "search" in self.url:
            self.page += 1
            return True, {"clicked": True}
        if command == "ghost_read" and args.get("selector") == "h1":
            return True, {"content": f"[0] h1: {self.name()}"}
        if command == "ghost_read":
            if "search" in self.url:
                content = RESULTS if self.page == 1 else (
                    "People\n[0] link: Cy Diaz (https://www.linkedin.com/in/cy-diaz)\n[4] button: Next" if self.page == 2
                    else "People\nNo more results")
                return True, {"url": self.url, "content": content}
            return True, {"url": self.url, "content": PROFILE.format(name=self.name())}
        return True, {"ok": True}

    def name(self):
        return {"ana-silva": "Ana Silva", "bo-chen": "Bo Chen", "cy-diaz": "Cy Diaz"}[self.url.rstrip("/").rsplit("/", 1)[-1]]


def test_a_repeating_automation_goes_through_every_link_page_after_page(tmp_path):
    async def main():
        browser = ListBrowser()
        hub = play_hub(browser, tmp_path)
        item = hub.scripts.add(clean(LOOP))
        task = await hub.play(item, {"template": "Hi {{first_name}}, how is research going?", "other": "ignored"})
        await finish(task)
        assert task.status == "done", task.result
        typed = [a["value"] for c, a in browser.calls if c == "ghost_fill"]
        assert typed == ["Hi Ana, how is research going?", "Hi Bo, how is research going?", "Hi Cy, how is research going?"]
        opened = [a["url"] for c, a in browser.calls if c == "ghost_navigate" and "/in/" in a["url"]]
        assert opened == ["https://www.linkedin.com/in/ana-silva", "https://www.linkedin.com/in/bo-chen",
                          "https://www.linkedin.com/in/cy-diaz"]
        assert task.result == "Done for 3 links."
        # The next run carries on where this one ended: nothing left, until Start over.
        assert hub.states[-1]["automations"][0]["done"] == 3
        browser.page = 1
        task = await hub.play(item, {"template": "Hi"})
        await finish(task)
        assert task.result == "Done for 0 links."
        await hub.handle({"action": "automation_reset", "automation": item["id"]})
        assert hub.scripts.get(item["id"])["done"] == []
    asyncio.run(main())


def test_a_repeating_automation_skips_a_link_that_fails_and_stops_after_three_in_a_row():
    async def main():
        browser = ListBrowser()
        hub = play_hub(browser)
        script = {**LOOP, "each": {"links": "linkedin.com/in/"}, "steps": LOOP["steps"][:2] + [{"do": "click", "text": "Follow"}]}
        task = await hub.play(hub.scripts.add(clean(script)), {"template": "x"})
        await finish(task)
        assert task.status == "done" and task.result.startswith("Done for 0 links. Skipped 2:")

        browser = ListBrowser()
        hub = play_hub(browser)
        many = "People\n" + "\n".join(f"[{i}] link: P{i} (https://www.linkedin.com/in/ana-silva-{i})" for i in range(5))
        browser.name = lambda: "Ana Silva"
        orig = browser.__call__

        async def call(command, args):
            if command == "ghost_read" and "search" in browser.url and not args.get("selector"):
                browser.calls.append((command, args))
                return True, {"url": browser.url, "content": many}
            return await orig(command, args)
        hub.call = call
        task = await hub.play(hub.scripts.add(clean(script)), {"template": "x"})
        await finish(task)
        assert task.status == "failed" and "3 links in a row didn't work" in task.result
    asyncio.run(main())


def test_play_asks_for_its_inputs_in_the_panel():
    async def main():
        hub = play_hub(ListBrowser())
        item = hub.scripts.add(clean(LOOP))
        await hub.handle({"action": "sync"})
        shown = hub.states[-1]["automations"][0]
        assert shown["inputs"] == [{"name": "template", "label": "Message"}]
        assert shown["each"] == "Repeats for each link with “linkedin.com/in/” in its address, page after page (“Next”)"
        assert shown["steps"][1:3] == ["Open the link", "Copy the first word of “Ana Silva” as {{first_name}}"]
        assert shown["task"] == ""
    asyncio.run(main())


def test_clean_checks_loops_and_inputs():
    with pytest.raises(ValueError, match="first step must open a page"):
        clean({**LOOP, "steps": LOOP["steps"][1:]})
    with pytest.raises(ValueError, match="without copying it first or asking for it"):
        clean({**LOOP, "inputs": []})
    assert "{{link}}" not in str(clean({**LOOP, "each": None})["steps"])  # without a loop, {{link}} can't be opened


def test_a_recording_becomes_a_script_with_values_and_a_loop():
    async def main():
        hub, browser, states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {}},
            {"tool": "ghost_fill", "args": {"choice": 0, "value": "Hi Ana"}},
            {"done": "Typed the message."},
        ]], plan={"reply": "Recording it.", "tasks": [{
            "title": "Record", "goal": "Do it once", "kind": "do", "save_as": {"name": "1st connection message"}}]})
        asked = []

        async def compose(model, prompt):
            asked.append(prompt)
            return json.dumps({**LOOP, "name": "Something else"})
        hub.compose = compose
        await hub.handle(send("make a play automation that messages my connections"))
        task = next(iter(hub.tasks.values()))
        for _ in range(100):
            if task.status == "needs_you":
                await hub.handle({"action": "approve", "task": task.id})
            if task.job.done():
                break
            await asyncio.sleep(0.01)
        saved = hub.scripts.find("1st connection message")
        assert saved and saved["each"]["links"] == "linkedin.com/in/" and saved["inputs"][0]["name"] == "template"
        assert '"value": "Hi Ana"' in asked[0] and "messages my connections" in asked[0]
    asyncio.run(main())


def test_a_recording_mia_cannot_turn_into_a_script_is_saved_as_recorded():
    async def main():
        hub, *_ = make_hub([[{"tool": "ghost_read", "args": {}}, {"done": "Read it."}]], plan={
            "reply": "Recording.", "tasks": [{"title": "R", "goal": "g", "kind": "do", "save_as": {"name": "Plain"}}]})

        async def compose(model, prompt):
            return "not json"
        hub.compose = compose
        await hub.handle(send("make an automation"))
        task = next(iter(hub.tasks.values()))
        await task.job
        assert hub.scripts.find("Plain")["steps"] == [{"do": "open", "url": "https://mail.example/"}]
    asyncio.run(main())


def test_an_empty_plan_gets_a_reply_instead_of_silence():
    async def main():
        hub, *_ = make_hub([], plan={"reply": "", "tasks": []})
        await hub.handle(send("make a play automation"))
        assert hub.messages[-1]["who"] == "mia" and "send it again" in hub.messages[-1]["text"]
    asyncio.run(main())


def test_an_automation_mia_writes_herself_is_made_reusable_too():
    async def main():
        hub, *_ = make_hub([], plan={"reply": "Saved.", "tasks": [], "automation": {
            "name": "1st connection message",
            "steps": [{"do": "open", "url": "https://www.linkedin.com/in/alejandro-lozano/"},
                      {"do": "type", "text": "Write a message", "value": "Hola Alejandro"}]}})
        asked = []

        async def compose(model, prompt):
            asked.append(prompt)
            return json.dumps(LOOP)
        hub.compose = compose
        await hub.handle(send("save that as an automation for all my connections"))
        saved = hub.scripts.find("1st connection message")
        assert "Hola Alejandro" in asked[0] and "all my connections" in asked[0]
        assert saved["each"]["links"] == "linkedin.com/in/" and saved["inputs"][0]["name"] == "template"
    asyncio.run(main())


def test_the_list_page_can_be_a_link_the_person_gives_before_play(tmp_path):
    script = {**LOOP, "inputs": LOOP["inputs"] + [{"name": "list_url", "label": "List page (link)"}],
              "steps": [{"do": "open", "url": "{{list_url}}"}] + LOOP["steps"][1:]}
    item = clean(script)
    assert item["steps"][0] == {"do": "open", "url": "{{list_url}}"}
    assert automations.describe_step(item["steps"][0]) == "Open the page in {{list_url}}"
    with pytest.raises(ValueError, match="without copying it first or asking for it"):
        clean({**script, "inputs": LOOP["inputs"]})
    with pytest.raises(ValueError, match="first step must open a page"):
        clean({**script, "steps": [{"do": "open", "url": "{{link}}"}] + LOOP["steps"][2:]})

    async def main():
        browser = ListBrowser()
        hub = play_hub(browser, tmp_path)
        saved = hub.scripts.add(item)
        task = await hub.play(saved, {"template": "Hi {{first_name}}",
                                      "list_url": "https://www.linkedin.com/search/results/people/?keywords=sales"})
        await finish(task)
        assert task.status == "done" and task.result == "Done for 3 links."
        first = next(a for c, a in browser.calls if c == "ghost_tab_open")
        assert first["url"] == "https://www.linkedin.com/search/results/people/?keywords=sales"

        task = await hub.play(saved, {"template": "Hi", "list_url": "not a link"})
        await finish(task)
        assert task.status == "failed" and "needs a web address" in task.result
    asyncio.run(main())
