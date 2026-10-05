import asyncio
import json

import ghost_chat
from ghost_chat import ChatHub, check_action, needs_approval, parse_plan

PAGE = "Inbox\n[0] link: Home (/)\n[1] button: Send\n[2] input(password): Password\n[3] button: Next page"

class FakeSession:
    """A model that replies from a script, one JSON action per turn."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []
        self.closed = False

    async def turn(self, text):
        self.prompts.append(text)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step if isinstance(step, str) else json.dumps(step)

    async def close(self):
        self.closed = True

class MiaAnswers(FakeSession):
    """Mia taking in her bots' results: she answers with what the last bot reported, marked as hers."""

    def __init__(self):
        super().__init__([])

    async def turn(self, text):
        self.prompts.append(text)
        return "Mia: " + text.rsplit("<<<\n", 1)[-1].split("\n>>>")[0]


class Browser:
    def __init__(self):
        self.calls = []

    async def __call__(self, command, args):
        self.calls.append((command, args))
        if command == "ghost_read":
            return True, {"url": "https://mail.example/", "title": "Inbox", "content": PAGE}
        if command == "ghost_tab_open":
            return True, {"id": 77, "url": args["url"]}
        return True, {"ok": True}

    def actions(self):
        return [(c, a) for c, a in self.calls if c != "ghost_show"]

def make_hub(scripts, plan=None):
    states, sessions, made = [], [], []

    def session(model, system, effort):
        made.append((system, model, effort))
        if system == ghost_chat.ANSWER_PROMPT:
            return MiaAnswers()
        sessions.append(FakeSession(scripts.pop(0)))
        return sessions[-1]

    async def push(state):
        states.append(state)

    async def planner(model, prompt):
        return json.dumps(plan or {})

    browser = Browser()
    hub = ChatHub(browser, push, session=session, plan=planner)
    hub.made = made
    hub.claude_status = lambda fresh=False: {"installed": True, "signed_in": True}
    return hub, browser, states, sessions

async def settle(hub):
    for _ in range(50):
        jobs = [t.job for t in hub.tasks.values() if t.job and not t.job.done()]
        if not jobs:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.01)

def send(text, mode="ask", run="parallel"):
    return {"action": "send", "text": text, "mode": mode, "run": run,
            "tab": {"id": 5, "url": "https://mail.example/", "title": "Inbox"}}

def test_looking_things_up_goes_to_other_sites_in_its_own_tab_and_answers_in_chat():
    async def main():
        hub, browser, states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {"max_chars": 999999}},
            {"tool": "ghost_vacuum", "args": {"url": "https://jobs.example/search?q=ai"}},
            {"done": "Top 5 jobs: ..."},
        ]], plan={"reply": "", "tasks": [{"title": "Find jobs", "goal": "Read my profile, find jobs", "kind": "ask"}]})
        await hub.handle(send("find me jobs"))
        await settle(hub)
        calls = [(c, a) for c, a in browser.actions() if c != "ghost_tab_list"]
        assert [c for c, _ in calls] == ["ghost_read", "ghost_tab_open", "ghost_wait", "ghost_read", "ghost_tab_close"]
        read = calls[0][1]
        assert read["max_chars"] == 8000 and read["tab_id"] == 5 and read["human_ok"] is True
        # The person's tab stays put: the search opens in a tab of the worker's own, which keeps
        # working even while the person watches it.
        assert calls[1][1]["url"] == "https://jobs.example/search?q=ai"
        assert calls[3][1]["tab_id"] == 77 and calls[3][1]["human_ok"] is True
        assert hub.messages[-1]["text"] == "Mia: Top 5 jobs: ..."  # Mia answers, from what her bot found
        assert sessions[0].closed
        # Mia closes the tab her bot opened once it has the answer, and the bot leaves the list.
        assert ("ghost_tab_close", {"tab_id": 77, "actor_id": "mia-2"}) in browser.calls
        assert "task-1" not in hub.tasks and not [a for a in hub.agents.values() if a.tab_id == 77]
    asyncio.run(main())

def test_do_waits_for_approval_before_a_risky_click():
    async def main():
        hub, browser, states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {}},
            {"note": "sending", "tool": "ghost_click", "args": {"choice": 1}},
            {"done": "Sent."},
        ]], plan={"reply": "On it.", "tasks": [{"title": "Send it", "goal": "Send the draft", "url": ""}]})
        await hub.handle(send("send my draft", mode="do"))
        for _ in range(50):
            task = next(iter(hub.tasks.values()))
            if task.status == "needs_you":
                break
            await asyncio.sleep(0.01)
        assert task.status == "needs_you" and "Send" in task.question
        assert "ghost_click" not in [c for c, _ in browser.actions()]
        await hub.handle({"action": "approve", "task": task.id})
        await settle(hub)
        # Mia posts her answer just after the task finishes; give a busy machine time to get there.
        for _ in range(500):
            if hub.messages and hub.messages[-1]["text"] == "Mia: Sent.":
                break
            await asyncio.sleep(0.01)
        clicks = [a for c, a in browser.actions() if c == "ghost_click"]
        assert clicks == [{"choice": 1, "tab_id": 5, "actor_id": "mia-1", "human_ok": True}]
        assert task.status == "done" and hub.messages[-1]["text"] == "Mia: Sent."
    asyncio.run(main())

def test_planner_cannot_upgrade_persons_ask_mode_to_do():
    async def main():
        hub, browser, _, _ = make_hub([[
            {"tool": "ghost_click", "args": {"choice": 3}},
            {"done": "I did not click."},
        ]], plan={"tasks": [{"title": "Inspect", "goal": "Inspect the page", "kind": "do"}]})
        await hub.handle(send("what is here?", mode="ask"))
        await settle(hub)
        assert next(iter(hub.tasks.values())).kind == "ask"
        assert "ghost_click" not in [command for command, _ in browser.actions()]
    asyncio.run(main())

def test_rejected_action_never_runs():
    async def main():
        hub, browser, _, sessions = make_hub([[
            {"tool": "ghost_read", "args": {}},
            {"tool": "ghost_click", "args": {"choice": 3}, "confirm": "Go to the next page?"},
            {"done": "Left it."},
        ]], plan={"tasks": [{"title": "Page", "goal": "next page", "url": ""}]})
        await hub.handle(send("next", mode="do"))
        task = next(iter(hub.tasks.values()))
        for _ in range(50):
            if task.status == "needs_you":
                break
            await asyncio.sleep(0.01)
        assert task.question == "Go to the next page?"
        await hub.handle({"action": "reject", "task": task.id})
        await settle(hub)
        assert "ghost_click" not in [c for c, _ in browser.actions()]
        assert "rejected" in sessions[0].prompts[2]
    asyncio.run(main())

def test_parallel_tasks_get_their_own_tabs_and_never_eval():
    async def main():
        # Bots start in any order and take these scripts in that order; two read, so a new tab is always read.
        scripts = [[{"tool": "ghost_eval", "args": {"script": "1"}}, {"done": "a"}],
                   [{"tool": "ghost_read", "args": {"tab_id": 1, "actor_id": "x"}}, {"done": "b"}],
                   [{"tool": "ghost_read", "args": {"tab_id": 1, "actor_id": "x"}}, {"done": "c"}]]
        hub, browser, _, sessions = make_hub(scripts, plan={"tasks": [
            {"title": "A", "goal": "do a", "url": "https://a.example/"},
            {"title": "B", "goal": "do b", "url": "https://b.example/"},
            {"title": "C", "goal": "bad", "url": "javascript:alert(1)"}]})
        await hub.handle(send("both", mode="do"))
        await settle(hub)
        commands = [c for c, _ in browser.actions()]
        assert "ghost_eval" not in commands and commands.count("ghost_tab_open") == 2
        # The three bots run at once, so the bot on the current tab may read first: look at a new tab's read.
        read = next(a for c, a in browser.actions() if c == "ghost_read" and a.get("tab_id") == 77)
        assert read["actor_id"].startswith("mia-") and read["human_ok"] is True
        # The javascript: url was dropped, so that task works on the current tab instead.
        assert [t.own_tab for t in hub.tasks.values()] == [True, True, False]
    asyncio.run(main())

def test_stop_cancels_a_waiting_task():
    async def main():
        hub, browser, _, _ = make_hub([[{"tool": "ghost_click", "args": {"choice": 1}, "confirm": "Send?"}]],
                                      plan={"tasks": [{"title": "T", "goal": "g", "url": ""}]})
        await hub.handle(send("x", mode="do"))
        task = next(iter(hub.tasks.values()))
        for _ in range(50):
            if task.status == "needs_you":
                break
            await asyncio.sleep(0.01)
        await hub.handle({"action": "stop", "task": task.id})
        await settle(hub)
        assert task.status == "stopped"
        assert browser.calls[-1][1].get("clear") is True
    asyncio.run(main())

def test_rules():
    elements = {1: "button: Send", 2: "input(password): Password", 3: "button: Next page"}
    assert needs_approval("ghost_click", {"choice": 1}, elements, "") == "Click “Send”?"
    assert needs_approval("ghost_click", {"choice": 3}, elements, "") == ""
    assert needs_approval("ghost_click", {"choice": 9}, elements, "")  # unknown element: ask
    assert needs_approval("ghost_key", {"key": "Enter"}, elements, "")
    assert check_action("ghost_fill", {"choice": 2, "value": "x"}, {"ghost_fill"}, elements)
    assert check_action("ghost_click", {"selector": "#x"}, {"ghost_click"}, elements)
    assert check_action("ghost_navigate", {"url": "file:///etc"}, {"ghost_navigate"}, elements)
    assert check_action("ghost_eval", {}, {"ghost_read"}, elements)
    plan = parse_plan('ok {"reply": "hi", "tasks": [{"title": "t", "goal": ""}, {"goal": "g", "url": "ftp://x"}]}')
    assert plan == {"reply": "hi", "tasks": [{"title": "Task", "goal": "g", "url": "", "kind": "do", "tab": 0, "keep_open": False,
                                              "done_when": "", "needs": [], "save_as": None}],
                    "automation": None, "run_automation": ""}


def test_panel_sees_claude_status_and_can_start_setup():
    from unittest import mock

    async def run():
        hub, _browser, states, _sessions = make_hub([])
        hub.claude_status = lambda fresh=False: {"installed": True, "signed_in": False}
        await hub.handle({"action": "sync"})
        assert states[-1]["claude"] == {"installed": True, "signed_in": False, "busy": ""}
        calls = []
        with mock.patch("claude_setup.status", side_effect=[{"installed": True, "signed_in": False},
                                                              {"installed": True, "signed_in": False},
                                                              {"installed": True, "signed_in": True}]), \
             mock.patch("claude_setup.login", side_effect=lambda: calls.append("login")), \
             mock.patch("ghost_chat.asyncio.sleep", new=mock.AsyncMock()):
            await hub.setup_claude()
        assert calls == ["login"]
        assert "signing_in" in [s["claude"]["busy"] for s in states]
        assert states[-1]["claude"]["busy"] == ""
    asyncio.run(run())


class FakeBot:
    def __init__(self, agent, answers):
        self.agent, self.answers, self.calls = agent, answers, []

    def handle(self, ask, tab_id):
        self.calls.append((ask["id"], tab_id))
        return self.answers.pop(0)


def test_one_agent_per_tab_holds_selection_and_chat_tasks():
    async def run():
        hub, _browser, states, _sessions = make_hub([])
        bots = []
        hub.make_bot = lambda agent: bots.append(FakeBot(agent, [("Cats", "They purr."), ("Dogs", "They bark.")])) or bots[-1]
        hub.me = lambda: "luis"
        await hub.explain({"id": "a1", "question": "Explain this.", "text": "cats are great"}, 5, "https://www.pets.example/x")
        await hub.explain({"id": "a2", "question": "And dogs?", "text": "dogs"}, 5, "https://www.pets.example/x")
        await hub.explain({"id": "a3", "question": "Explain this.", "text": "other"}, 9, "https://other.example/")
        await settle(hub)  # the bots answer on a thread: wait for them, not for a number of ticks
        tasks = list(hub.tasks.values())
        assert tasks[0].agent is tasks[1].agent is not tasks[2].agent
        agent = tasks[0].agent
        assert (agent.name, agent.host, agent.tab_id) == ("Pets bot", "pets.example", 5)
        assert agent.id == "luis-mia-1"
        assert tasks[0].title == "Explain “cats are great…”" and tasks[1].title == "And dogs?"
        assert [t.status for t in tasks[:2]] == ["done", "done"]
        assert tasks[0].result == "Cats. They purr."
        # One bot per agent, on its tab. Agents start their bots in either order.
        assert len(bots) == 2 and sorted(b.calls for b in bots) == [[("a1", 5), ("a2", 5)], [("a3", 9)]]
        last = states[-1]
        assert [a["name"] for a in last["agents"]] == ["Pets bot", "Other bot"]
        assert {t["agent"] for t in last["tasks"]} == {"luis-mia-1", "luis-mia-2"}
    asyncio.run(run())


def test_tasks_on_one_tab_wait_for_each_other():
    async def run():
        hub, _browser, _states, _sessions = make_hub([])
        started = []
        release = asyncio.Event()

        class SlowBot:
            def handle(self, ask, tab_id):
                started.append(ask["id"])
                return ("Done", "")

        hub.make_bot = lambda agent: SlowBot()
        agent = hub.agent_for(5)
        await agent.lock.acquire()  # the agent is busy with another task
        await hub.explain({"id": "a1", "question": "Why?"}, 5)
        for _ in range(20):
            await asyncio.sleep(0)
        assert started == [] and hub.tasks["task-1"].status == "waiting"
        agent.lock.release()
        for _ in range(50):
            await asyncio.sleep(0)
            if hub.tasks["task-1"].status == "done":
                break
        assert started == ["a1"]
    asyncio.run(run())


def test_a_task_with_a_link_gets_its_own_agent():
    hub, _browser, _states, _sessions = make_hub([])
    here = hub.agent_for(5, "https://a.example/")
    assert hub.agent_for(5) is here
    assert hub.agent_for(None) is not here and hub.agent_for(None) is not hub.agent_for(None)


def test_a_question_answered_from_context_starts_no_task_and_sees_what_was_explained():
    async def main():
        prompts = []
        hub, _browser, _states, _sessions = make_hub([])

        async def planner(model, prompt):
            prompts.append(prompt)
            return json.dumps({"reply": "It means part-time.", "tasks": []})
        hub.plan_run = planner
        hub.make_bot = lambda agent: FakeBot(agent, [("Fractional", "Part-time role.")])
        await hub.explain({"id": "a1", "question": "Explain this.", "text": "Fractional"}, 5, "https://mail.example/")
        for _ in range(50):
            await asyncio.sleep(0)
        await hub.handle(send("so is this full time?"))
        assert len(hub.tasks) == 1 and hub.messages[-1]["text"] == "It means part-time."
        assert "Fractional. Part-time role." in prompts[0]  # the planner sees the tab's earlier answer
    asyncio.run(main())


class ActingBot(FakeBot):
    def __init__(self, agent):
        super().__init__(agent, [("On it", "Invite the 4 waitlist users")])
        self.actions = {}

    def handle(self, ask, tab_id):
        self.actions[ask["id"]] = "Invite the 4 waitlist users: a@x.io, b@x.io, c@x.io, d@x.io"
        return super().handle(ask, tab_id)


def test_a_page_question_asking_for_action_becomes_a_do_task_that_reports_on_the_card():
    async def run():
        hub, browser, _states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {"max_chars": 2000}},
            {"done": "Invited all 4."},
        ]])
        hub.make_bot = lambda agent: ActingBot(agent)
        await hub.explain({"id": "a1", "question": "can you invite these 4?", "text": "a@x.io b@x.io",
                           "url": "https://dash.example/waitlist"}, 5, "https://dash.example/waitlist")
        await settle(hub)
        explain, todo = list(hub.tasks.values())
        assert explain.status == "done" and explain.result.startswith("Handed to a task")
        assert (todo.kind, todo.tab_id, todo.agent) == ("do", 5, explain.agent)
        assert "a@x.io b@x.io" in todo.goal and todo.status == "done"
        cards = [a for c, a in browser.calls if c == "ghost_suggest"]
        assert cards[-1]["id"] == "re-a1" and cards[-1]["reply_to"] == "a1"
        assert (cards[-1]["title"], cards[-1]["body"]) == ("Done", "Invited all 4.")
    asyncio.run(run())


def test_requested_action_reads_only_a_leading_do_line():
    from ask_bot import requested_action
    assert requested_action("DO: Invite  these\n4 people") == "Invite these 4 people"
    assert requested_action("do: click Save") == "click Save"
    assert requested_action("Title\nYou could DO: this") == ""


def test_closing_an_agent_stops_it_and_takes_its_tasks_off_the_list():
    async def run():
        hub, _browser, states, _sessions = make_hub([])
        hub.make_bot = lambda agent: FakeBot(agent, [("Cats", "They purr."), ("Dogs", "They bark.")])
        await hub.explain({"id": "a1", "question": "Explain this.", "text": "cats"}, 5, "https://pets.example/")
        await hub.explain({"id": "a2", "question": "Explain this.", "text": "dogs"}, 9, "https://dogs.example/")
        await settle(hub)
        first = next(iter(hub.tasks.values())).agent.id
        await hub.handle({"action": "close", "agent": first})
        assert {t["agent"] for t in states[-1]["tasks"]} == {a["id"] for a in states[-1]["agents"]} != {first}
        assert len(states[-1]["tasks"]) == 1
    asyncio.run(run())


def test_a_worker_that_runs_out_of_steps_is_told_to_finish_with_what_it_has(monkeypatch):
    import ghost_chat
    monkeypatch.setattr(ghost_chat, "MAX_STEPS", 3)
    monkeypatch.setattr(ghost_chat, "WRAP_UP_STEPS", 2)

    async def main():
        hub, _browser, _states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {}}, {"tool": "ghost_scroll", "args": {"direction": "down"}},
            {"tool": "ghost_read", "args": {}}, {"done": "Partial: 2 jobs found."},
        ]], plan={"reply": "", "tasks": [{"title": "Find jobs", "goal": "find", "kind": "ask"}]})
        await hub.handle(send("find jobs"))
        await settle(hub)
        prompts = sessions[0].prompts
        assert "2 steps left" in prompts[1] and "1 step left" in prompts[2] and "out of steps" in prompts[3]
        assert hub.tasks["task-1"].status == "done" and hub.tasks["task-1"].result == "Partial: 2 jobs found."
    asyncio.run(main())


class TabsBrowser(Browser):
    """Two open tabs: LinkedIn (where a bot already is) and Upwork."""

    async def __call__(self, command, args):
        if command == "ghost_tab_list":
            self.calls.append((command, args))
            return True, {"tabs": [{"id": 5, "url": "https://mail.example/", "title": "Inbox"},
                                   {"id": 21, "url": "https://www.linkedin.com/in/me/", "title": "Me | LinkedIn"},
                                   {"id": 22, "url": "https://www.upwork.com/nx/search/jobs/", "title": "Upwork"},
                                   {"id": 23, "url": "chrome://settings", "title": "Settings"}]}
        return await super().__call__(command, args)


def test_mia_sends_bots_to_open_tabs_chains_them_and_answers_from_their_results():
    import ghost_chat

    async def main():
        states, prompts = [], {}
        # Keyed by the start of each goal (the jobs prompt also quotes the profile task).
        scripts = {"Find jobs:": [{"tool": "ghost_read", "args": {}}, {"done": "Job A, Job B."}],
                   "Read profile:": [{"tool": "ghost_read", "args": {}}, {"done": "AI research lead, Python."}]}

        class Session(FakeSession):
            def __init__(self, system):
                self.system = system
                super().__init__([])

            async def turn(self, text):
                self.prompts.append(text)
                if self.system == ghost_chat.ANSWER_PROMPT:
                    prompts["answer"] = text
                    return "Job A fits your AI research background best."
                if not self.script:
                    name = next(n for n in scripts if n in text)
                    prompts[name] = text
                    self.script = scripts[name]
                return json.dumps(self.script.pop(0))

        async def push(state):
            states.append(state)

        async def planner(model, prompt):
            prompts["plan"] = prompt
            return json.dumps({"reply": "Checking both tabs.", "tasks": [
                {"title": "Read profile", "goal": "Read profile: skills", "kind": "ask", "tab": 21},
                {"title": "Find jobs", "goal": "Find jobs: that fit the profile", "kind": "ask", "tab": 22, "needs": [0]},
            ]})

        browser = TabsBrowser()
        hub = ChatHub(browser, push, session=lambda model, system, effort: Session(system), plan=planner)
        hub.claude_status = lambda fresh=False: {"installed": True, "signed_in": True}
        linkedin_bot = hub.agent_for(21, "https://www.linkedin.com/in/me/")
        await hub.handle(send("check the upwork tab against my linkedin"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if hub.messages[-1]["text"].startswith("Job A fits"):
                break
        # Mia saw the open web tabs, which one they're on and where a bot already is.
        assert "tab 21: Me | LinkedIn" in prompts["plan"] and f"bot {linkedin_bot.name} is here" in prompts["plan"]
        assert "tab 5: Inbox (https://mail.example/) · their current tab" in prompts["plan"]
        assert "chrome://settings" not in prompts["plan"]
        profile, jobs = hub.tasks["task-1"], hub.tasks["task-2"]
        assert profile.agent is linkedin_bot and profile.tab_id == 21 and not profile.own_tab
        assert jobs.tab_id == 22 and jobs.agent is not linkedin_bot
        assert "Me | LinkedIn" in prompts["Read profile:"]
        # The jobs bot got the profile bot's result; Mia answered once, from both.
        assert "AI research lead, Python." in prompts["Find jobs:"]
        assert "AI research lead" in prompts["answer"] and "Job A, Job B." in prompts["answer"]
        said = [m["text"] for m in hub.messages if m["who"] == "mia"]
        assert said == ["Checking both tabs.", "Job A fits your AI research background best."]
        assert not any(c == "ghost_tab_open" for c, _ in browser.calls)
    asyncio.run(main())


def test_bots_are_named_after_their_site():
    from ghost_chat import site_name
    assert [site_name(h) for h in ("upwork.com", "mail.google.com", "linkedin.com", "dashboard.clerk.com",
                                   "bbc.co.uk", "localhost")] == ["Upwork", "Gmail", "LinkedIn", "Clerk", "Bbc", "localhost"]

    async def main():
        hub, *_ = make_hub([])
        first = hub.agent_for(1, "https://www.upwork.com/jobs")
        second = hub.agent_for(2, "https://www.upwork.com/nx/search")
        gmail = hub.agent_for(3, "https://mail.google.com/mail/u/0/")
        blank = hub.agent_for(4, "")
        assert [a.name for a in (first, second, gmail)] == ["Upwork bot", "Upwork bot 2", "Gmail bot"]
        assert blank.name.startswith("Bot ")
    asyncio.run(main())


def test_a_bot_leaves_the_list_when_its_tab_closes_and_the_x_stops_and_closes_it():
    async def main():
        hub, browser, states, _ = make_hub([[{"tool": "ghost_wait", "args": {"ms": 5000}}] * 3,
                                           [{"tool": "ghost_wait", "args": {"ms": 5000}}] * 3],
                                          plan={"reply": "", "tasks": [
                                              {"title": "Here", "goal": "g", "kind": "ask"},
                                              {"title": "There", "goal": "g", "kind": "ask", "url": "https://b.example/"}]})

        async def slow(command, args, _call=browser.__call__):
            if command == "ghost_wait":
                await asyncio.sleep(10)
            return await _call(command, args)
        hub.call = slow
        await hub.handle(send("two things"))
        for _ in range(20):
            await asyncio.sleep(0.01)
        here = next(t for t in hub.tasks.values() if t.title == "Here").agent
        there = next(t for t in hub.tasks.values() if t.title == "There").agent
        assert there.opened and not here.opened
        await hub.handle({"action": "tab_closed", "tab": here.tab_id})
        assert {t.agent for t in hub.tasks.values()} == {there}
        await hub.handle({"action": "close", "agent": there.id})
        assert ("ghost_tab_close", {"tab_id": 77, "actor_id": there.id}) in browser.calls
        assert not hub.tasks and states[-1]["tasks"] == []
    asyncio.run(main())


def test_a_dropped_bot_leaves_the_room():
    async def main():
        hub, *_ = make_hub([])
        left = []

        async def retire(actor_id):
            left.append(actor_id)
        hub.retire = retire
        bot = hub.agent_for(3, "https://mail.google.com/")
        await hub.handle({"action": "tab_closed", "tab": 3})
        other = hub.agent_for(4, "https://upwork.com/")
        await hub.handle({"action": "new"})
        assert left == [bot.id, other.id] and not hub.agents
    asyncio.run(main())


def test_a_tab_the_person_asked_to_open_stays_open():
    async def main():
        hub, browser, _, _ = make_hub([[{"done": "LinkedIn is open."}]], plan={"reply": "", "tasks": [
            {"title": "Open LinkedIn", "goal": "open it", "kind": "ask", "url": "https://www.linkedin.com/", "keep_open": True}]})
        await hub.handle(send("open a linkedin bot"))
        await settle(hub)
        assert not any(c == "ghost_tab_close" for c, _ in browser.calls)
        assert hub.tasks["task-1"].status == "done" and hub.tasks["task-1"].agent.name == "LinkedIn bot"
    asyncio.run(main())


def test_chats_are_saved_survive_a_restart_and_can_be_reopened(tmp_path):
    from chat_store import ChatStore

    async def push(state):
        pushed.append(state)

    async def browser(tool, args):
        return True, {}

    async def main():
        store = ChatStore(tmp_path / "chats")
        hub = ChatHub(browser, push, store=store)
        hub.say("you", "find  me\nAI research jobs")
        hub.say("mia", "Here are three.")
        first = hub.chat_id
        assert oct((tmp_path / "chats" / f"{first}.json").stat().st_mode & 0o777) == "0o600"

        # A restart (closing Chrome) carries on with the same conversation.
        again = ChatHub(browser, push, store=store)
        assert again.chat_id == first and [m["text"] for m in again.messages] == ["find  me\nAI research jobs", "Here are three."]
        again.say("you", "thanks")
        assert again.messages[-1]["id"] > again.messages[-2]["id"]

        # New chat: the old one stays in the history and can be opened again.
        await again.handle({"action": "new"})
        assert again.messages == [] and again.chat_id != first
        again.say("you", "second chat")
        await again.handle({"action": "sync"})
        chats = pushed[-1]["chats"]
        assert [c["title"] for c in chats] == ["second chat", "find me AI research jobs"]
        await again.handle({"action": "open_chat", "chat": first})
        assert again.chat_id == first and again.messages[-1]["text"] == "thanks"

        # Deleting removes the file; bad ids touch nothing outside the folder.
        await again.handle({"action": "delete_chat", "chat": "../../etc/passwd"})
        await again.handle({"action": "delete_chat", "chat": first})
        assert again.messages == [] and [c["title"] for c in store.list()] == ["second chat"]

    pushed = []
    asyncio.run(main())


def test_without_a_store_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hub, *_ = make_hub([])
    hub.say("you", "hello")
    assert hub.state()["chats"] == [] and not (tmp_path / ".ghost").exists()


def test_bots_run_sonnet_on_low_and_mia_runs_the_picked_model_on_medium(monkeypatch):
    async def main():
        hub, *_ = make_hub([[{"done": "Found it."}]], plan={"reply": "", "tasks": [
            {"title": "Look", "goal": "look it up", "kind": "ask", "url": "https://example.com/"}]})
        await hub.handle(send("look it up") | {"model": "claude-opus-5-5"})
        await settle(hub)
        assert (ghost_chat.WORKER_PROMPT, "claude-sonnet-5-5", "low") in hub.made

        args = []

        async def spawn(*cmd, **kw):
            args.extend(cmd)
            raise RuntimeError("stop here")
        monkeypatch.setattr(ghost_chat.shutil, "which", lambda b: b)
        monkeypatch.setattr(ghost_chat.asyncio, "create_subprocess_exec", spawn)
        try:
            await ghost_chat.ClaudeSession("claude-opus-5-5", "plan").turn("hi")
        except RuntimeError:
            pass
        assert args[args.index("--model") + 1] == "claude-opus-5-5" and args[args.index("--effort") + 1] == "medium"
    asyncio.run(main())


def test_a_worker_that_slips_into_a_native_tool_call_is_reminded_and_sees_the_exact_request():
    async def main():
        link = "https://docs.google.com/spreadsheets/d/abc123/edit"
        hub, _, _, sessions = make_hub([[RuntimeError("The model's tool call could not be parsed (retry also failed)."),
                                         {"done": "Read 12 names."}]],
                                       plan={"reply": "", "tasks": [{"title": "Read sheet", "goal": "read the sheet", "kind": "ask"}]})
        await hub.handle(send(f"use this {link}"))
        await settle(hub)
        assert hub.tasks["task-1"].status == "done"
        assert link in sessions[0].prompts[0] and "native tool call" in sessions[0].prompts[1]
    asyncio.run(main())


def test_mia_keeps_line_breaks_and_a_sheet_read_comes_whole():
    assert ghost_chat._lines("Best fits:\n- Ana  Ruiz\n\n\n\n- Bo Li ", 100) == "Best fits:\n- Ana Ruiz\n\n- Bo Li"
    sheet = ghost_chat.page_block("ghost_read", {"title": "Tracker", "url": "https://docs.google.com/spreadsheets/d/x",
                                                "content": "Cells of the open sheet tab, as CSV:\n" + "a,b\n" * 5000})
    page = ghost_chat.page_block("ghost_read", {"title": "Page", "url": "https://example.com", "content": "x" * 20000})
    assert len(sheet) > 20000 and len(page) < 8000


def test_pressing_enter_in_a_search_box_needs_no_approval_but_elsewhere_it_does():
    elements = {38: "input(text): Search employees by title, keyword or school", 4: "textarea: Write a message"}
    assert needs_approval("ghost_key", {"key": "Enter", "choice": 38}, elements, "") == ""
    assert needs_approval("ghost_key", {"key": "Enter", "choice": 4}, elements, "").startswith("Press Enter")


def test_mia_sees_which_bot_is_waiting_for_the_person():
    hub, *_ = make_hub([])
    task = hub.add(ghost_chat.Task(1, "Find leaders", "find them", "", "do", "parallel", "m", hub.agent_for(7, "https://www.linkedin.com/")))
    task.status, task.question = "needs_you", "Press Enter in Search? It may send or submit."
    context = hub.context({"title": "LinkedIn", "url": "https://www.linkedin.com/"}, "English", 7)
    assert "LinkedIn bot · Find leaders: needs you, waiting for the person to approve" in context
    assert ghost_chat._why(asyncio.TimeoutError()) == "The model took too long to answer."


def test_stop_on_the_page_calls_off_the_bot_and_closes_the_question():
    import threading

    release = threading.Event()

    class SlowBot:
        def __init__(self):
            self.cancelled, self.actions = set(), {}

        def handle(self, ask, tab_id):
            release.wait(5)
            return ("Stopped", "") if ask["id"] in self.cancelled else ("An answer", "")

    async def main():
        hub, browser, _, _ = make_hub([])
        bot = SlowBot()
        hub.make_bot = lambda agent: bot
        await hub.explain({"id": "q1", "question": "Explain this.", "text": "word", "url": "https://x.example/"}, 9, "https://x.example/")
        task = next(iter(hub.tasks.values()))
        await asyncio.sleep(0.05)
        await hub.cancel_ask("q1")
        release.set()
        await asyncio.sleep(0.05)
        assert task.status == "stopped" and "q1" in bot.cancelled
        stopped = [a for c, a in browser.calls if c == "ghost_suggest"]
        assert stopped and stopped[-1]["reply_to"] == "q1" and stopped[-1]["title"] == "Stopped"
        await hub.cancel_ask("unknown")  # someone else's question: nothing happens
    asyncio.run(main())


def test_a_bot_with_a_goal_is_sent_back_when_it_reports_early_and_a_quick_job_is_not():
    async def main():
        hub, _browser, _states, sessions = make_hub([[
            {"tool": "ghost_read", "args": {}}, {"done": "Found 1 of 10."},
            {"tool": "ghost_scroll", "args": {"direction": "down"}}, {"done": "Found 10 of 10: ..."},
            {"done": "Found 10 of 10: ... (all)"},
        ]], plan={"reply": "", "tasks": [{"title": "Find 10", "goal": "find leaders", "kind": "ask",
                                          "done_when": "10 people who pass, or every page checked"}]})
        await hub.handle(send("find 10 until you finish"))
        await settle(hub)
        prompts = sessions[0].prompts
        assert "Your goal: 10 people who pass" in prompts[0]
        assert "From Mia: is your goal met?" in prompts[2] and "From Mia: is your goal met?" in prompts[4]
        assert hub.tasks["task-1"].status == "done" and hub.tasks["task-1"].result == "Found 10 of 10: ... (all)"

        quick, _, _, quick_sessions = make_hub([[{"done": "It's Tuesday."}]],
                                               plan={"reply": "", "tasks": [{"title": "Day", "goal": "read", "kind": "ask"}]})
        await quick.handle(send("what day is it"))
        await settle(quick)
        assert len(quick_sessions[0].prompts) == 1 and quick.tasks["task-1"].result == "It's Tuesday."
    asyncio.run(main())


def test_long_reports_reach_mia():
    async def main():
        report = "x" * 3500
        hub, _, _, _ = make_hub([[{"done": report}]], plan={"reply": "", "tasks": [{"title": "List", "goal": "list", "kind": "ask"}]})
        await hub.handle(send("list them"))
        await settle(hub)
        assert hub.tasks["task-1"].result == report
    asyncio.run(main())


def test_a_message_while_signed_out_points_to_the_sign_in_button_and_plans_nothing():
    async def main():
        hub, _, _, _ = make_hub([], plan={"reply": "On it", "tasks": [{"title": "t", "goal": "g", "kind": "ask"}]})
        hub.claude_status = lambda fresh=False: {"installed": True, "signed_in": False}
        await hub.handle(send("find jobs"))
        await settle(hub)
        assert not hub.tasks and "Sign in to Claude" in hub.messages[-1]["text"]
        hub.claude_status = lambda fresh=False: {"installed": False, "signed_in": False}
        await hub.handle(send("find jobs"))
        assert "Set up Claude" in hub.messages[-1]["text"]
    asyncio.run(main())


def test_task_bots_wait_for_the_model_while_chat_keeps_its_time_limit():
    async def main():
        hub, _, _, sessions = make_hub([[{"done": "a"}]], plan={"tasks": [{"title": "A", "goal": "do a"}]})
        await hub.handle(send("go", mode="do"))
        await settle(hub)
        assert sessions[0].timeout is None
    asyncio.run(main())
    assert ghost_chat.ClaudeSession("m", "s").timeout == ghost_chat.TURN_TIMEOUT == 150


def test_a_step_is_read_from_a_reply_with_text_a_fence_or_a_second_object():
    from ghost_chat import parse_json
    step = {"tool": "ghost_read", "args": {"max_chars": 4000}}
    assert parse_json('Reading next. {"tool": "ghost_read", "args": {"max_chars": 4000}} {"tool": "ghost_scroll"}') == step
    assert parse_json('```json\n{"tool": "ghost_read", "args": {"max_chars": 4000}}\n```') == step
    assert parse_json('{not json} then {"tool": "ghost_read", "args": {"max_chars": 4000}}') == step
    assert parse_json("no step here") == {}


def test_an_unreadable_reply_is_sent_back_without_running_anything():
    async def main():
        hub, browser, _, sessions = make_hub([["I'll read the page now.", {"done": "a"}]],
                                             plan={"tasks": [{"title": "A", "goal": "do a"}]})
        await hub.handle(send("go", mode="do"))
        await settle(hub)
        task = next(iter(hub.tasks.values()))
        assert task.status == "done" and "wasn't a step" in sessions[0].prompts[1]
        assert not [c for c, _ in browser.actions() if c not in {"ghost_read", "ghost_tab_list"}]
    asyncio.run(main())


def test_a_bot_that_fails_or_is_stopped_still_reports_what_it_found():
    async def main():
        script = [{"tool": "ghost_read", "args": {}, "found": ["Ana · https://x.example/ana", "Bo · https://x.example/bo"]},
                  {"tool": "ghost_read", "args": {}}, RuntimeError("model crashed")]
        hub, _, _, _ = make_hub([script], plan={"tasks": [{"title": "A", "goal": "find people"}]})
        await hub.handle(send("go", mode="do"))
        await settle(hub)
        task = next(iter(hub.tasks.values()))
        assert task.status == "failed"
        assert "model crashed" in task.result and "https://x.example/ana" in task.result and "https://x.example/bo" in task.result

        hub, _, _, _ = make_hub([[{"tool": "ghost_click", "args": {"choice": 1}, "confirm": "Send?", "found": "Cy · https://x.example/cy"}]],
                                plan={"tasks": [{"title": "T", "goal": "g"}]})
        await hub.handle(send("x", mode="do"))
        task = next(iter(hub.tasks.values()))
        for _ in range(50):
            if task.status == "needs_you":
                break
            await asyncio.sleep(0.01)
        await hub.handle({"action": "stop", "task": task.id})
        await settle(hub)
        assert task.status == "stopped" and "https://x.example/cy" in task.result
    asyncio.run(main())
