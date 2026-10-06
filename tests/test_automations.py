import asyncio
import json
import os
from datetime import datetime

import pytest

import automations
import ghost_chat
from automations import AutomationStore, clean
from ghost_chat import ChatHub, element_label, match_element
from tests.test_chat import FakeSession, MiaAnswers, make_hub, send

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
    ({"each": {"links": "x"}, "steps": [{"do": "click", "text": "Export"}, {"do": "click", "text": "Go"}]}, "first step must open"),
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
            ("ghost_tab_list", {}),  # afterwards: is its tab still open, so its bot stays?
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
        assert shown["steps"][1:3] == ["Open the link", "Copy the person's first name as {{first_name}}"]
        assert shown["task"] == ""
    asyncio.run(main())


def test_clean_checks_loops_and_inputs():
    with pytest.raises(ValueError, match="first step must open a page"):
        clean({**LOOP, "steps": LOOP["steps"][1:]})
    with pytest.raises(ValueError, match="without copying it first or asking for it"):
        clean({**LOOP, "inputs": []})
    assert "{{link}}" not in str(clean({**LOOP, "each": None})["steps"])  # without a loop, {{link}} can't be opened


def test_an_empty_plan_gets_a_reply_instead_of_silence():
    async def main():
        hub, *_ = make_hub([], plan={"reply": "", "tasks": []})
        await hub.handle(send("make a play automation"))
        assert hub.messages[-1]["who"] == "mia" and "send it again" in hub.messages[-1]["text"]
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


# -- builder bots: they write the script, test it on the real pages, then save it -------

def builder_hub(browser, scripts, plan, tmp_path=None):
    made = []

    def session(model, system, effort):
        if system == ghost_chat.ANSWER_PROMPT:
            return MiaAnswers()
        made.append({"model": model, "system": system, "effort": effort, "session": FakeSession(scripts.pop(0))})
        return made[-1]["session"]

    async def planner(model, prompt):
        return json.dumps(plan)

    async def push(state):
        pass

    hub = ChatHub(browser, push, session=session, plan=planner,
                  scripts=AutomationStore(tmp_path / "automations.json" if tmp_path else None))
    hub.claude_status = lambda fresh=False: {"installed": True, "signed_in": True}
    hub.made = made
    return hub


async def run_task(hub, task):
    """Run it to the end, approving what it asks (the questions are kept in hub.asked)."""
    hub.asked = getattr(hub, "asked", [])
    for _ in range(300):
        if task.job.done():
            return
        if task.status == "needs_you" and task.approval and not task.approval.done():
            hub.asked.append(task.question)
            await hub.handle({"action": "approve", "task": task.id})
        await asyncio.sleep(0.01)
    raise AssertionError(f"{task.id} still {task.status}: {task.note}")


LOOP_SEND = {**LOOP, "steps": LOOP["steps"] + [{"do": "click", "text": "Send"}]}
SEARCH = "https://www.linkedin.com/search/results/people/?network=F"


def test_a_builder_bot_tests_its_script_on_the_real_pages_and_saves_only_what_passed(tmp_path):
    async def main():
        browser = ListBrowser()
        hub = builder_hub(browser, [[
            {"tool": "ghost_read", "args": {}},
            {"done": "Messages each connection.", "automation": LOOP_SEND},  # not tested yet
            {"tool": "test_automation", "args": {"automation": LOOP_SEND, "inputs": {"template": "Hi {{first_name}}"}}},
            {"done": "Messages each connection; fill Message before Play.", "automation": LOOP_SEND},
        ]], {"reply": "A builder bot is on it.", "tasks": [{
            "title": "Build it", "kind": "build", "url": SEARCH,
            "goal": "Message each 1st connection with the person's text, starting with their first name.",
            "build": {"name": "1st connection message", "schedule": {"kind": "manual"}}}]}, tmp_path)
        await hub.handle({**send("make an automation that messages my 1st connections"), "model": "claude-opus-5-5"})
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        assert task.kind == "build" and task.status == "done", task.result
        builder = hub.made[0]
        assert builder["model"] == "claude-opus-5-5" and builder["effort"] == ghost_chat.BUILD_EFFORT
        assert ghost_chat.BUILD_PROMPT in builder["system"] and "# Building Play Automations" in builder["system"]
        prompts = builder["session"].prompts
        assert "hasn't passed test_automation" in prompts[2]
        test = prompts[3]
        assert test.startswith("Result of test_automation:") and "Test passed: every step worked." in test
        assert "The list page has 2 items with “linkedin.com/in/”" in test and "Next-page button “Next”: found" in test
        assert "copied “Ana” as {{first_name}}" in test and "typed “Hi Ana”" in test
        assert "skipped in this test: on a real run it asks the person first (Click “Send”?)" in test
        # The test never pressed Send, nor asked about it: the only click was Message.
        assert hub.asked == [f"Open {SEARCH}? This sends the address to that site."]
        assert [a.get("choice") for c, a in browser.calls if c == "ghost_click"] == [1]
        saved = hub.scripts.find("1st connection message")
        assert saved["steps"][-1] == {"do": "click", "text": "Send"} and saved["inputs"][0]["name"] == "template"
        assert any(m["text"].startswith("Saved “1st connection message” as a Play Automation") for m in hub.messages)
    asyncio.run(main())


def test_a_failed_test_shows_the_builder_what_is_on_the_page():
    async def main():
        broken = {**LOOP, "steps": LOOP["steps"][:3] + [{"do": "click", "text": "Send InMail"}]}
        hub = builder_hub(ListBrowser(), [[
            {"tool": "test_automation", "args": {"automation": broken, "inputs": {"template": "x"},
                                                 "link": "https://www.linkedin.com/in/bo-chen"}},
            {"fail": "The page has no such button."},
        ]], {"reply": "", "tasks": [{"title": "B", "kind": "build", "url": SEARCH, "goal": "Build it",
                                     "build": {"name": "Broken"}}]})
        await hub.handle(send("make it"))
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        test = hub.made[0]["session"].prompts[1]
        assert "Testing the steps for one item: https://www.linkedin.com/in/bo-chen" in test
        assert "copied “Bo” as {{first_name}}" in test
        assert "4. Click “Send InMail”: FAILED, couldn't find it on the page" in test
        assert "- button: Message" in test and "- h1: Bo Chen" in test
        assert task.status == "failed" and not hub.scripts.list()
    asyncio.run(main())


def test_a_script_mia_drafts_goes_to_a_builder_instead_of_being_saved_untested():
    async def main():
        hub = builder_hub(PlayBrowser(), [[{"fail": "stop here"}]], {"reply": "", "tasks": [], "automation": SCRIPT})
        await hub.handle(send("save that as an automation"))
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        assert task.kind == "build" and task.build["name"] == "Daily report" and task.url == "https://reports.example/"
        assert "Build this Play Automation, starting from this draft" in task.goal and "#export" in task.goal
        assert not hub.scripts.list()
    asyncio.run(main())


def test_plans_from_before_builder_bots_still_build():
    plan = ghost_chat.parse_plan(json.dumps({"tasks": [
        {"title": "R", "goal": "Do it once", "kind": "do", "save_as": {"name": "Old"}},
        {"title": "New one", "goal": "Build it", "kind": "build"}]}))
    assert [(t["kind"], t["build"]) for t in plan["tasks"]] == [("build", {"name": "Old"}), ("build", {"name": "New one"})]


def test_a_bot_uses_a_saved_automation_for_one_item_in_its_own_tab(tmp_path):
    async def main():
        browser = ListBrowser()
        hub = builder_hub(browser, [[
            {"tool": "use_automation", "args": {"name": "1st connection message", "inputs": {"template": "Hola {{first_name}}"},
                                                "link": "https://www.linkedin.com/in/bo-chen"}},
            {"done": "Typed the message for Bo."},
        ]], {"reply": "", "tasks": [{"title": "Message Bo", "kind": "do",
                                     "goal": "Use the Play Automation “1st connection message” for Bo Chen."}]}, tmp_path)
        hub.scripts.add(clean(LOOP))
        await hub.handle(send("message Bo with my automation", mode="do"))
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        assert task.status == "done", task.result
        used = hub.made[0]["session"].prompts[1]
        assert used.startswith("Result of use_automation:") and "Ran “1st connection message” for https://www.linkedin.com/in/bo-chen." in used
        assert 'Copied: {"first_name": "Bo"}' in used
        assert [a["value"] for c, a in browser.calls if c == "ghost_fill"] == ["Hola Bo"]
        # It ran in a tab of its own: the person's tab (5) was never touched.
        assert not [c for c, a in browser.calls if a.get("tab_id") == 5 and c in {"ghost_navigate", "ghost_click", "ghost_fill"}]
        assert "use_automation" in hub.made[0]["system"]
        assert "inputs: template; repeats for each link with “linkedin.com/in/”" in hub.made[0]["session"].prompts[0]
    asyncio.run(main())


def test_a_loop_whose_items_differ_by_their_query_keeps_it():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        page = ("HN\n[0] link: 12 comments (item?id=1)\n[1] link: 3 comments (item?id=2#c)\n"
                "[2] link: 12 comments (https://news.ycombinator.com/item?id=1)")

        async def call(command, args):
            if command == "ghost_read" and not args.get("selector"):
                return True, {"url": "https://news.ycombinator.com/", "content": page}
            return await browser(command, args)
        hub.call = call
        task = await hub.play(hub.scripts.add(clean({"name": "HN", "each": {"links": "item?id="}, "steps": [
            {"do": "open", "url": "https://news.ycombinator.com/"}, {"do": "open", "url": "{{link}}"}]})))
        await finish(task)
        assert task.result == "Done for 2 links."
        opened = [a["url"] for c, a in browser.calls if c == "ghost_navigate" and "item" in a["url"]]
        assert opened == ["https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=2"]
    asyncio.run(main())


def test_a_bot_that_only_looks_things_up_may_use_an_automation_that_does_not_type(tmp_path):
    async def main():
        browser = ListBrowser()
        hub = builder_hub(browser, [[
            {"tool": "use_automation", "args": {"name": "Names", "link": "https://www.linkedin.com/in/ana-silva"}},
            {"tool": "use_automation", "args": {"name": "1st connection message", "link": "https://www.linkedin.com/in/ana-silva"}},
            {"done": "Ana."},
        ]], {"reply": "", "tasks": [{"title": "Name", "kind": "ask", "goal": "Get Ana's first name with Names."}]}, tmp_path)
        hub.scripts.add(clean(LOOP))
        hub.scripts.add(clean({"name": "Names", "each": {"links": "linkedin.com/in/"}, "steps": [
            LOOP["steps"][0], LOOP["steps"][1], {"do": "copy", "css": "h1", "as": "first_name", "words": 1},
            {"do": "click", "text": "Send"}]}))
        await hub.handle(send("what's Ana's first name?"))
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        prompts = hub.made[0]["session"].prompts
        assert 'Copied: {"first_name": "Ana"}' in prompts[1] and "skipped in this test" in prompts[1]
        assert "types into the page, and your task only looks things up" in prompts[2]
        assert not [c for c, a in browser.calls if c in {"ghost_click", "ghost_fill"}]
    asyncio.run(main())


def test_a_loop_can_take_its_items_from_one_part_of_the_page_only():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        page = "[0] link: Recent: Ismael (https://www.linkedin.com/in/ismael)\n[1] link: Ana (https://www.linkedin.com/in/ana)"
        results = "[1] link: Ana (https://www.linkedin.com/in/ana)"

        async def call(command, args):
            if command == "ghost_read" and not args.get("selector"):
                return True, {"url": SEARCH, "content": page}
            if command == "ghost_read" and args.get("selector") == "main":
                return True, {"url": SEARCH, "content": results}
            return await browser(command, args)
        hub.call = call
        item = clean({"name": "In main", "each": {"links": "linkedin.com/in/", "within": "main"},
                      "steps": [{"do": "open", "url": SEARCH}, {"do": "open", "url": "{{link}}"}]})
        assert item["each"]["within"] == "main"
        saved = hub.scripts.add(item)
        assert automations.view(saved)["each"] == "Repeats for each link with “linkedin.com/in/” in its address inside main"
        task = await hub.play(saved)
        await finish(task)
        assert task.result == "Done for 1 link."
        assert [a["url"] for c, a in browser.calls if c == "ghost_navigate" and "/in/" in a["url"]] == ["https://www.linkedin.com/in/ana"]
    asyncio.run(main())


def test_an_automation_without_an_open_step_runs_on_the_page_the_person_has_open():
    item = clean({"name": "Message this person", "inputs": [{"name": "template", "label": "Message"}],
                  "steps": [{"do": "copy", "css": "main h2", "as": "first_name", "words": 1},
                            {"do": "click", "css": "main a[href*='/messaging/compose/']"},
                            {"do": "type", "text": "Write a message", "value": "{{template}}"},
                            {"do": "click", "text": "Send"}]})
    assert item["steps"][0]["do"] == "copy"
    with pytest.raises(ValueError, match="list page"):
        clean({**item, "each": {"links": "linkedin.com/in/"}})


def test_one_step_can_be_deleted_unless_the_rest_would_not_work(tmp_path):
    store = automations.AutomationStore(tmp_path / "a.json")
    item = store.add(clean({"name": "Four", "inputs": [{"name": "template", "label": "Message"}],
                            "steps": [{"do": "open", "url": "https://a.example/"},
                                      {"do": "copy", "css": "h1", "as": "first_name", "words": 1},
                                      {"do": "type", "text": "Box", "value": "{{template}}"}]}))
    assert automations.view(item)["uses"] == [[], [], ["template"]]
    assert store.delete_step(item["id"], 0) == ""
    assert [s["do"] for s in store.get(item["id"])["steps"]] == ["copy", "type"]
    assert store.delete_step(item["id"], 7) == "that step isn't there anymore"
    item = store.add(clean({"name": "Uses", "steps": [{"do": "open", "url": "https://a.example/"},
                                                      {"do": "copy", "css": "h1", "as": "n"},
                                                      {"do": "type", "text": "Box", "value": "{{n}}"}]}))
    assert "without copying it first" in store.delete_step(item["id"], 1)
    assert len(store.get(item["id"])["steps"]) == 3


def test_play_types_the_message_with_its_line_breaks():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        item = hub.scripts.add(clean({"name": "Lines", "inputs": [{"name": "template", "label": "Message"}],
                                      "steps": [{"do": "open", "url": "https://a.example/"},
                                                {"do": "type", "css": "div.box", "value": "{{template}}"}]}))
        task = await hub.play(item, {"template": "Hola\r\n\r\nEstoy lanzando  AI\nLuis"})
        await finish(task)
        assert [a["value"] for c, a in browser.calls if c == "ghost_fill"] == ["Hola\n\nEstoy lanzando  AI\nLuis"]
    asyncio.run(main())


def test_full_access_skips_asking_only_when_the_person_presses_play():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        item = hub.scripts.add(clean({"name": "Send it", "steps": [{"do": "open", "url": "https://a.example/"},
                                                                    {"do": "click", "text": "Send"}]}))
        await hub.handle({"action": "automation_full_access", "automation": item["id"], "on": True})
        assert automations.view(hub.scripts.get(item["id"]))["full_access"] is True
        await hub.handle({"action": "play", "automation": item["id"]})
        task = next(t for t in hub.tasks.values() if t.automation == item["id"])
        await finish(task)
        assert task.status == "done" and not task.question
        assert any(c == "ghost_click" for c, _ in browser.calls)
        scheduled = await hub.play(hub.scripts.get(item["id"]))  # not pressed: still asks
        await finish(scheduled)
        assert scheduled.status == "needs_you" and "Send" in scheduled.question
        scheduled.job.cancel()
        await hub.handle({"action": "automation_full_access", "automation": item["id"], "on": False})
        assert hub.scripts.get(item["id"])["full_access"] is False
    asyncio.run(main())


def test_a_step_finds_its_element_again_when_the_site_redraws_it():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        fills = []

        async def call(command, args):
            if command == "ghost_fill":
                fills.append(args)
                if len(fills) == 1:
                    return False, "Selector not found: div.box"
            return await browser(command, args)
        hub.call = call
        item = hub.scripts.add(clean({"name": "Redraw", "steps": [{"do": "open", "url": "https://a.example/"},
                                                                   {"do": "type", "css": "div.box", "value": "Hi"}]}))
        task = await hub.play(item)
        await finish(task)
        assert task.status == "done" and len(fills) == 2
    asyncio.run(main())


def test_a_row_is_added_to_the_spreadsheet_with_the_page_and_copied_values():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        sheet = "https://docs.google.com/spreadsheets/d/abcdefghijklmnopqrstuv/edit#gid=7"
        appended = []

        async def call(command, args):
            if command == "ghost_tab_list":
                return True, {"tabs": [{"id": 91, "url": "https://www.linkedin.com/in/ana/?mini=1", "active": True}]}
            if command == "ghost_sheet_append":
                assert args["tab_id"] == 91  # its own tab, opened for the sheet
                appended.append(args)
                return True, {"added": True, "row": 590}
            return await browser(command, args)
        hub.call = call
        item = hub.scripts.add(clean({"name": "Log", "inputs": [{"name": "sheet_url", "label": "Spreadsheet link"},
                                                                {"name": "campaign", "label": "Campaign"}],
                                      "steps": [{"do": "open", "url": "https://www.linkedin.com/in/ana/"},
                                                {"do": "copy", "css": "#total", "as": "full_name"},
                                                {"do": "append", "sheet": "{{sheet_url}}", "tab": "Reach Out",
                                                 "row": ["{{full_name}}", "", "{{campaign}}", "{{page_url}}"],
                                                 "unique": "{{page_url}}"}]}))
        task = await hub.play(item, {"sheet_url": sheet, "campaign": "sdr"})
        await finish(task)
        assert appended[0]["row"] == ["42 open items", "", "sdr", "https://www.linkedin.com/in/ana"]
        assert appended[0]["unique"] == "https://www.linkedin.com/in/ana" and appended[0]["sheet"] == sheet
        assert task.status == "done" and "Added to the spreadsheet as row 590" in task.result
    asyncio.run(main())


def test_a_box_can_be_a_drop_down_of_saved_searches(tmp_path):
    store = AutomationStore(tmp_path / "a.json")
    item = store.add(clean({"name": "Pick", "inputs": [{"name": "campaign", "label": "Campaign", "choices": ["a", "a", " b ", ""]}],
                            "steps": [{"do": "open", "url": "https://a.example/"},
                                      {"do": "type", "text": "Box", "value": "{{campaign}}"}]}))
    assert item["inputs"][0]["choices"] == ["a", "b"]
    assert store.set_choices(item["id"], "campaign", ["a", "b", "sdr-sales-search"])
    assert automations.view(store.get(item["id"]))["inputs"][0]["choices"] == ["a", "b", "sdr-sales-search"]
    assert not store.set_choices(item["id"], "nope", ["x"])
    assert AutomationStore(tmp_path / "a.json").get(item["id"])["inputs"][0]["choices"] == ["a", "b", "sdr-sales-search"]


def test_an_automation_can_be_renamed_from_the_panel(tmp_path):
    store = AutomationStore(tmp_path / "a.json")
    steps = [{"do": "open", "url": "https://a.example/"}]
    one = store.add(clean({"name": "One", "steps": steps}))
    store.add(clean({"name": "Two", "steps": steps}))
    assert store.rename(one["id"], "  First   message ") == ""
    assert AutomationStore(tmp_path / "a.json").get(one["id"])["name"] == "First message"
    assert "empty" in store.rename(one["id"], "   ")
    assert "Two" in store.rename(one["id"], "two")
    assert store.rename(one["id"], "first MESSAGE") == "" and store.get(one["id"])["name"] == "first MESSAGE"
    assert "gone" in store.rename("auto-00000000", "X")


def test_a_drop_down_can_list_a_column_of_the_spreadsheet(tmp_path):
    store = AutomationStore(tmp_path / "a.json")
    item = store.add(clean({"name": "Add", "inputs": [{"name": "sheet_url", "label": "Sheet"},
                                                      {"name": "campaign", "label": "Campaign", "choices": ["new-one"]}],
                            "steps": [{"do": "append", "sheet": "{{sheet_url}}", "tab": "Reach Out",
                                       "row": ["{{campaign}}"]}]}))
    assert automations.view(store.get(item["id"]))["sheet_input"] == "sheet_url"
    assert store.set_column(item["id"], "campaign", "  Source / Campaign ")
    assert AutomationStore(tmp_path / "a.json").get(item["id"])["inputs"][1]["column"] == "Source / Campaign"
    assert store.set_choices(item["id"], "campaign", ["new-one", "two"])
    assert store.get(item["id"])["inputs"][1]["column"] == "Source / Campaign"
    assert store.set_column(item["id"], "campaign", "") and "column" not in store.get(item["id"])["inputs"][1]
    assert not store.set_column(item["id"], "sheet_url", "A")


def test_a_play_bot_lives_while_its_tab_is_open_and_goes_with_it():
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser)
        open_tabs = [{"id": 91, "url": "https://a.example/", "active": False},
                     {"id": 5, "url": "https://www.linkedin.com/in/ana/", "active": True}]

        async def call(command, args):
            if command == "ghost_tab_list":
                return True, {"tabs": open_tabs}
            return await browser(command, args)
        hub.call = call
        item = hub.scripts.add(clean({"name": "Look", "steps": [{"do": "open", "url": "https://a.example/"},
                                                                 {"do": "copy", "css": "#total", "as": "n"}]}))
        task = await hub.play(item)
        await finish(task)
        await asyncio.sleep(0.05)
        assert task.status == "done" and task.agent.id in hub.agents  # its tab is still open
        await hub.handle({"action": "tab_closed", "tab": 91})
        assert task.agent.id not in hub.agents

        # Its tab closed while nobody heard: the bot goes when the run ends.
        task = await hub.play(item)
        await finish(task)
        open_tabs[:] = [t for t in open_tabs if t["id"] != 91]
        await hub.end_play_bot(task)
        assert task.agent.id not in hub.agents

        # On the person's own tab it's that tab's bot, not a second one.
        mine = hub.agent_for(5, "https://www.linkedin.com/in/ana/")
        here = hub.scripts.add(clean({"name": "Here", "steps": [{"do": "copy", "css": "#total", "as": "n"}]}))
        task = await hub.play(here)
        await finish(task)
        await asyncio.sleep(0.05)
        assert task.agent is mine and [a for a in hub.agents.values() if a.tab_id == 5] == [mine]
    asyncio.run(main())
