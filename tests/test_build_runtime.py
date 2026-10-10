"""Build gates must stop work before browser effects and before unreviewed saves."""

import asyncio

import pytest

import ghost_chat
from automation_build import BuildJournal
from ghost_chat import Agent, Task
from tests.test_automations import PlayBrowser, SCRIPT, builder_hub, play_hub, run_task
from tests.test_chat import send


@pytest.fixture(autouse=True)
def isolated_builds(monkeypatch, tmp_path):
    monkeypatch.setattr(ghost_chat, "PLAY_RETRIES", (0, 0, 0))
    monkeypatch.setattr(ghost_chat, "BuildJournal",
                        lambda **kwargs: BuildJournal(root=tmp_path / "builds", **kwargs))


def runtime(tmp_path):
    browser = PlayBrowser()
    hub = play_hub(browser, tmp_path)
    agent = Agent(1, 91)
    task = Task(1, "Build report", "Export a report", SCRIPT["steps"][0]["url"],
                "build", "parallel", "claude-opus-5-5", agent)
    task.build = {"name": SCRIPT["name"]}
    task.request = SCRIPT["steps"][0]["url"]
    task.build_journal = BuildJournal(root=tmp_path / "builds",
                                     plan=["Open the report", "Copy its total", "Verify the requested result"])
    hub.agents[agent.id] = agent
    hub.tasks[task.id] = task
    return hub, browser, task


def test_unplanned_build_cannot_test_or_replace_candidate_evidence(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        task.build_journal = BuildJournal(root=tmp_path / "unplanned")
        task.tested = "stale proof"
        first = {**SCRIPT, "steps": SCRIPT["steps"][:1]}
        before = task.build_journal.context()
        result = await hub.test_automation(task, {"automation": first})
        assert "build_plan before testing" in result
        assert not browser.calls and not task.tested
        assert task.build_journal.context() == before
        # Manual discovery is still available; then the persisted plan unlocks testing.
        await hub.act(task, {"tool": "ghost_read", "args": {}}, ghost_chat.BUILD_TOOLS)
        assert any(tool == "ghost_read" for tool, _ in browser.calls)
        await hub.act(task, {"tool": "build_plan", "args": {
            "steps": ["Open the report", "Copy its total", "Verify the result"]}}, ghost_chat.BUILD_TOOLS)
        restored = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "unplanned")
        assert restored.state["plan"] == task.build_journal.state["plan"]
        assert "Test passed" in await hub.test_automation(task, {"automation": first})
    asyncio.run(main())


def test_panel_progress_reports_durable_gates_without_private_evidence(tmp_path):
    hub, browser, task = runtime(tmp_path)
    journal = task.build_journal
    journal.set_plan(["Open", "Copy", "Send", "Verify"])
    first = {**SCRIPT, "steps": SCRIPT["steps"][:1]}
    journal.propose(first)
    journal.record_test(1)
    candidate = journal.propose({**SCRIPT, "steps": [*first["steps"], {"do": "click", "text": "Send report"}]})
    journal.record_test(1, failed_step=2, details="Private destination evidence")
    journal.begin_effect({}, "https://private.example/receipt", 2, candidate["steps"][1])
    journal.state["reviews"] = [
        {"kind": "human", "verdict": "pass", "metadata": {"candidate": "an older candidate"}},
        {"kind": "adversarial", "verdict": "pass", "metadata": {"candidate": ghost_chat.json.dumps(candidate, sort_keys=True)}}]
    progress = task.view()["build_progress"]
    assert progress == {"verified": 1, "candidate_steps": 2, "planned_steps": 4,
                        "failed_step": 2, "pending_steps": [], "uncertain_action": True,
                        "destination_observed": False,
                        "reviews_passed": ["adversarial"]}
    assert "Private destination" not in str(progress)
    assert "private.example" not in str(progress)
    task.build_journal = BuildJournal.load(journal.build_id, root=tmp_path / "builds")
    assert task.view()["build_progress"]["uncertain_action"] is True


def test_regular_task_has_no_automation_progress(tmp_path):
    _, _, task = runtime(tmp_path)
    task.build_journal = None
    task.build = None
    assert task.view()["build_progress"] is None


@pytest.mark.parametrize("selected", [0, 5])
def test_build_on_existing_page_uses_separate_owned_tab(tmp_path, selected):
    async def main():
        browser = PlayBrowser()
        hub = builder_hub(browser, [[{"tool": "ghost_read", "args": {}},
                                    {"fail": "Intentional discovery stop"}]],
            {"tasks": [{"title": "Build report", "kind": "build", "tab": selected,
                        "goal": "Discover current report", "build": {"name": "Report"}}]}, tmp_path)
        async def tabs():
            return [{"id": 5, "url": "https://reports.example/", "title": "Report"}]
        hub.open_tabs = tabs
        msg = send("/goal Create an automation from https://reports.example/")
        msg["tab"]["url"] = "https://reports.example/"
        await hub.handle(msg)
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        assert task.own_tab and task.agent.opened
        assert task.tab_id != 5
        opens = [args for tool, args in browser.calls if tool == "ghost_tab_open"]
        assert opens and opens[0]["url"] == "https://reports.example/"
        assert all(args.get("tab_id") != 5 for tool, args in browser.calls
                   if tool in {"ghost_read", "ghost_click", "ghost_fill", "ghost_navigate", "ghost_key"})
    asyncio.run(main())


@pytest.mark.parametrize("wrong", [
    {"tasks": [{"title": "Ordinary job", "kind": "do", "goal": "Click Send"}]},
    {"reply": "Here is a suggestion"},
    {"run_automation": "Report"},
    {"tasks": [{"title": "One", "kind": "build"}, {"title": "Two", "kind": "build"}]},
])
@pytest.mark.parametrize("corrected", [False, True])
def test_goal_cannot_be_routed_to_ordinary_or_multiple_tasks(tmp_path, wrong, corrected):
    async def main():
        browser = PlayBrowser()
        hub = builder_hub(browser, [], {}, tmp_path)
        prompts, started = [], []
        correct = {"tasks": [{"title": "Report", "kind": "build", "goal": "Copy report total",
                              "url": "https://reports.example/", "build": {"name": "Report"}}]}
        async def plan(model, prompt):
            prompts.append(prompt)
            return ghost_chat.json.dumps(correct if corrected and len(prompts) == 2 else wrong)
        async def work(task):
            started.append(task)
            task.status = "stopped"
        hub.plan_run, hub.work = plan, work
        await hub.handle(send("/goal Create a reusable report automation from https://reports.example/"))
        assert len(prompts) == 2 and "Routing correction" in prompts[1]
        assert hub.planning == 0
        for task in hub.tasks.values():
            await task.job
        if corrected:
            assert len(started) == 1 and started[0].build == {"name": "Report"}
            assert started[0].kind == "build" and started[0].own_tab
            assert started[0].request.startswith("/goal ")
            restored = BuildJournal.load(started[0].build_journal.build_id, root=tmp_path / "builds")
            assert restored.state["request"] == started[0].request
            assert restored.state["checkpoint"]["goal"] == "Copy report total"
            assert restored.state["checkpoint"]["status"] == "waiting"
        else:
            assert not started and not hub.tasks
            assert "No browser actions were started" in hub.messages[-1]["text"]
        assert not any(tool in {"ghost_tab_open", "ghost_click", "ghost_fill", "ghost_key", "ghost_navigate"}
                       for tool, _ in browser.calls)
    asyncio.run(main())


def test_build_journal_startup_failure_stops_before_browser_work(tmp_path, monkeypatch):
    async def main():
        browser = PlayBrowser()
        hub = builder_hub(browser, [], {"tasks": [{"title": "Report", "kind": "build",
            "goal": "Copy report", "url": "https://reports.example/", "build": {"name": "Report"}}]}, tmp_path)
        def unavailable(**kwargs):
            raise OSError("Storage unavailable")
        monkeypatch.setattr(ghost_chat, "BuildJournal", unavailable)
        await hub.handle(send("/goal Create a report automation from https://reports.example/"))
        task = next(iter(hub.tasks.values()))
        assert task.status == "failed" and task.job.done()
        assert "No browser work started" in task.result
        assert "Storage unavailable" in task.result
        assert not hub.made
        assert not any(tool == "ghost_tab_open" for tool, _ in browser.calls)
    asyncio.run(main())


def test_panel_confirmation_does_not_hide_other_unknown_writes(tmp_path):
    _, _, task = runtime(tmp_path)
    task.build_journal.state["effects"] = {
        "first": {"status": "uncertain", "confirmation": {"record_id": "private-id"}},
        "second": {"status": "uncertain"}}
    progress = task.view()["build_progress"]
    assert progress["uncertain_action"] is True
    assert progress["destination_observed"] is False
    task.build_journal.state["effects"]["second"]["confirmation"] = {"record_id": "another-private-id"}
    progress = task.view()["build_progress"]
    assert progress["uncertain_action"] is True
    assert progress["destination_observed"] is True
    assert "private-id" not in str(progress)


def test_text_target_uses_current_sample_and_never_reuses_previous_target(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        task.full_access = True
        values = {"target": "Export"}
        step = {"do": "click", "text": "{{target}}"}
        await hub.play_step(task, step, values)
        values["target"] = "Home"
        await hub.play_step(task, step, values)
        assert [args["choice"] for tool, args in browser.calls if tool == "ghost_click"] == [2, 0]
        assert step["text"] == "{{target}}"
        await hub.play_step(task, {"do": "copy", "text": "{{target}}", "as": "heading"}, values)
        assert values["heading"] == "Home"
    asyncio.run(main())


def test_locked_prefix_is_rejected_before_any_browser_call(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        first = {**SCRIPT, "steps": SCRIPT["steps"][:1]}
        assert "Test passed" in await hub.test_automation(task, {"automation": first})
        before = list(browser.calls)
        changed = {**SCRIPT, "steps": [{"do": "open", "url": "https://different.example/"},
                                        SCRIPT["steps"][1]]}
        assert "validated prefix is locked" in await hub.test_automation(task, {"automation": changed})
        assert browser.calls == before
        assert task.build_journal.state["candidate"]["steps"] == first["steps"]
    asyncio.run(main())


def test_failure_locks_advancement_until_the_failing_step_is_repaired(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        first = {**SCRIPT, "steps": SCRIPT["steps"][:1]}
        await hub.test_automation(task, {"automation": first})
        failed = {**SCRIPT, "steps": [first["steps"][0], {"do": "click", "text": "Missing export"}]}
        assert "Test failed" in await hub.test_automation(task, {"automation": failed})
        assert task.build_journal.state["failed_step"] == 2
        before = list(browser.calls)
        extended = {**failed, "steps": [*failed["steps"], {"do": "click", "text": "Export"}]}
        assert "validate the current step" in await hub.test_automation(task, {"automation": extended})
        assert browser.calls == before
        repaired = {**failed, "steps": [first["steps"][0], {"do": "click", "text": "Export"}]}
        assert "Test passed" in await hub.test_automation(task, {"automation": repaired})
        assert task.build_journal.state["failed_step"] is None
        assert task.build_journal.state["validated_prefix"] == 2
    asyncio.run(main())


def test_missing_input_samples_reject_before_browser_calls(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = {**SCRIPT, "steps": SCRIPT["steps"][:1], "inputs": [{"name": "message"}]}
        assert "give sample values for every input: message" in await hub.test_automation(task, {"automation": item})
        assert not browser.calls and not task.tested
        assert task.build_journal.state["validated_prefix"] == 0
    asyncio.run(main())


@pytest.mark.parametrize("effect", [
    {"do": "click", "text": "Send report"},
    {"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test",
     "tab": "Reports", "row": ["42"]},
])
def test_skipped_side_effects_cannot_be_saved_as_tested(tmp_path, effect):
    async def main():
        hub, browser, task = runtime(tmp_path)
        await hub.test_automation(task, {"automation": {**SCRIPT, "steps": SCRIPT["steps"][:1]}})
        candidate = {**SCRIPT, "steps": [SCRIPT["steps"][0], effect]}
        result = await hub.test_automation(task, {"automation": candidate})
        assert "remain unverified" in result
        assert task.build_journal.state["pending_steps"] == [2]
        assert task.build_journal.state["validated_prefix"] == 1
        assert "hasn't passed test_automation" in hub.check_build(task, candidate)
        assert task.draft is None and not hub.scripts.list()
        assert not any(c in {"ghost_click", "ghost_sheet_append"} for c, _ in browser.calls)
    asyncio.run(main())


def test_current_page_script_is_tested_without_requiring_an_open_step(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        candidate = {**SCRIPT, "steps": [{"do": "copy", "css": "#total", "as": "total"}]}
        result = await hub.test_automation(task, {"automation": candidate})
        assert "Test passed: 1 steps executed" in result and "copied “42 open items”" in result
        assert task.build_journal.state["validated_prefix"] == 1
        assert task.tested and any(c == "ghost_read" for c, _ in browser.calls)
    asyncio.run(main())


def test_empty_copy_output_stops_the_prefix_instead_of_claiming_a_pass(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        first = {**SCRIPT, "steps": SCRIPT["steps"][:1]}
        await hub.test_automation(task, {"automation": first})
        original = hub.call

        async def empty_read(command, args):
            if command == "ghost_read" and args.get("selector"):
                return True, {"content": "", "url": task.current_url}
            return await original(command, args)

        hub.call = empty_read
        result = await hub.test_automation(task, {"automation": {**SCRIPT, "steps": SCRIPT["steps"][:2]}})
        assert "Test failed" in result and "copy target has no visible text" in result
        assert task.build_journal.state["failed_step"] == 2
        assert task.build_journal.state["validated_prefix"] == 1
        assert not task.tested
    asyncio.run(main())


def completion_hub(tmp_path, reviews, final):
    candidate = {**SCRIPT, "steps": SCRIPT["steps"][:2]}
    builder = [{"tool": "build_plan", "args": {"steps": ["Open the report", "Copy its total"]}}]
    builder += [{"tool": "test_automation", "args": {"automation": {**candidate, "steps": candidate["steps"][:n]}}}
               for n in (1, 2)]
    builder += [{"done": "Copies the report total.", "automation": candidate}, *final]
    hub = builder_hub(PlayBrowser(), [builder, *[[r] for r in reviews]],
                      {"reply": "Building", "tasks": [{"title": "Report", "kind": "build",
                       "url": SCRIPT["steps"][0]["url"], "goal": "Copy the report total",
                       "build": {"name": SCRIPT["name"]}}]}, tmp_path)
    return hub


def test_completed_harmless_build_requires_two_independent_review_sessions(tmp_path):
    async def main():
        hub = completion_hub(tmp_path, [{"verdict": "pass", "issues": []}] * 2, [])
        await hub.handle(send("Copy the total from https://reports.example/ as an automation"))
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        assert task.status == "done", task.result
        assert hub.scripts.find(SCRIPT["name"]) is not None
        assert len(hub.made) == 3
        assert [r["kind"] for r in task.build_journal.state["reviews"]] == ["adversarial", "human"]
        assert hub.made[2]["model"] == "claude-opus-5-5" and hub.made[2]["effort"] == "medium"
        assert all(s["session"].closed for s in hub.made)
    asyncio.run(main())


def test_reviewer_rejection_prevents_saving_despite_passing_runtime_tests(tmp_path):
    async def main():
        hub = completion_hub(tmp_path, [{"verdict": "revise", "issues": ["No representative second report tested"]}],
                             [{"fail": "Need another report to verify reuse."}])
        await hub.handle(send("Copy the total from https://reports.example/ as an automation"))
        task = next(iter(hub.tasks.values()))
        await run_task(hub, task)
        assert task.status == "failed" and not hub.scripts.list()
        assert "Adversarial review requires changes" in hub.made[0]["session"].prompts[-1]
        assert task.build_journal.state["reviews"][0]["verdict"] == "revise"
        assert len(hub.made) == 2 and task.draft is None
    asyncio.run(main())


@pytest.mark.parametrize("source_text,url,allowed", [
    ("Use https://reports.example/", "https://reports.example/", True),
    ("Use (https://reports.example/).", "https://reports.example/", True),
    ("Use https://reports.example/private", "https://reports.example/", False),
    ("Use https://reports.example/?token=123", "https://reports.example/", False),
    ("Use https://reports.example/", "https://reports.example/private", False),
    ("Use https://reports.example.evil/", "https://reports.example", False),
])
def test_navigation_authority_requires_the_complete_requested_url(source_text, url, allowed):
    assert ghost_chat.request_allows_url(source_text, url) is allowed


def test_partial_requested_url_requires_approval_before_navigation(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        task.request = "Read https://reports.example/private"
        asked = []
        async def reject(t, question, choice=None):
            asked.append(question)
            return False
        hub.wait_for_approval = reject
        assert not await hub.approve_urls(task, ["https://reports.example/"])
        assert asked == ["Open https://reports.example/? This sends the address to that site."]
        assert not browser.calls
        assert await hub.approve_urls(task, ["https://reports.example/private"])
        assert len(asked) == 1
    asyncio.run(main())


@pytest.mark.parametrize("failure", ["malformed", "empty_sample", "template", "navigation", "live", "runtime"])
def test_failed_retest_revokes_previous_completion_authority(tmp_path, failure):
    async def main():
        hub, browser, task = runtime(tmp_path)
        candidate = {**SCRIPT, "steps": SCRIPT["steps"][:1], "inputs": [{"name": "message"}]}
        assert "Test passed" in await hub.test_automation(task, {"automation": candidate, "inputs": {"message": "hello"}})
        tested = task.tested
        assert tested
        before = list(browser.calls)
        args = {"automation": candidate, "inputs": {"message": "hello"}}
        if failure == "malformed":
            args["automation"] = None
        elif failure == "empty_sample":
            args["inputs"] = {}
        elif failure == "template":
            candidate = {**candidate, "steps": [*candidate["steps"], {"do": "type", "text": "Search reports", "value": "{{message}}"}]}
            args.update(automation=candidate, inputs={"message": "{{missing}}"})
        elif failure in {"navigation", "live"}:
            async def reject(*a, **kw):
                return False
            hub.wait_for_approval = reject
            if failure == "navigation":
                task.request = "Unrelated request"
                task.approved_urls.clear()
            else:
                args["mode"] = "live"
        else:
            async def unavailable(*a, **kw):
                raise RuntimeError("Browser disconnected")
            hub.into_own_tab = unavailable
        result = await hub.test_automation(task, args)
        assert "Not tested" in result or "couldn't run" in result
        assert not task.tested
        assert "hasn't passed test_automation" in hub.check_build(task, {**SCRIPT, "steps": SCRIPT["steps"][:1], "inputs": [{"name": "message"}]})
        assert browser.calls == before
    asyncio.run(main())


def test_goal_lists_named_build_and_resumes_durable_state_without_replanning(tmp_path, monkeypatch):
    async def main():
        hub, _, _ = runtime(tmp_path)
        hub.tasks.clear()
        journal = BuildJournal("Export report", root=tmp_path / "builds")
        journal.propose({**SCRIPT, "steps": SCRIPT["steps"][:1]})
        journal.record_test(1)
        journal.save_checkpoint({"build": {"name": "Daily report"}, "goal": "Export report",
                                 "url": SCRIPT["steps"][0]["url"], "model": "claude-opus-5-5", "status": "stopped"})
        from automation_build import list_builds
        monkeypatch.setattr(ghost_chat, "list_builds", lambda: list_builds(journal.root))
        class Loader:
            @staticmethod
            def load(identifier):
                return BuildJournal.load(identifier, root=journal.root)
        monkeypatch.setattr(ghost_chat, "BuildJournal", Loader)
        resumed = []
        async def observe(task):
            resumed.append(task)
        hub.work = observe
        await hub.send({"text": "/goal"})
        assert "Daily report — stopped, 1/1 steps verified" in hub.messages[-1]["text"]
        assert "/goal resume Daily report" in hub.messages[-1]["text"]
        await hub.send({"text": "/goal resume daily REPORT"})
        task = next(iter(hub.tasks.values()))
        await task.job
        assert resumed == [task]
        assert task.build_journal.build_id == journal.build_id
        assert task.build_journal.state["validated_prefix"] == 1
        assert task.request == "Export report"
        assert "Read the current page" in task.context
    asyncio.run(main())


def test_builder_cannot_act_by_old_number_after_a_prefix_test(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        task.elements = {3: 'button: Send report'}
        await hub.test_automation(task, {'automation': {**SCRIPT, 'steps': SCRIPT['steps'][:1]}})
        before = list(browser.calls)
        from ghost_chat import BUILD_TOOLS, check_action
        for tool in ('ghost_click', 'ghost_fill'):
            message = check_action(tool, {'choice': 3, 'value': 'example'}, BUILD_TOOLS, task.elements)
            assert 'Read the page again' in message
        assert browser.calls == before
        assert not check_action('ghost_click', {'choice': 3}, BUILD_TOOLS, {3:'button: Generate preview'})
    asyncio.run(main())


@pytest.mark.parametrize('clarified, saves', [
    ({'verdict':'pass','issues':[],'suggestions':['Optional wording improvement']}, True),
    ({'verdict':'revise','issues':['Wrong result selected'],'suggestions':[]}, False),
])
def test_conflicting_pass_review_is_clarified_before_build_changes(tmp_path, clarified, saves):
    async def main():
        hub = completion_hub(tmp_path, [{'verdict':'pass','issues':[]}, {'verdict':'pass','issues':[]}],
                             [] if saves else [{'fail':'Need to repair the result target'}])
        original_session = hub.session
        count = 0
        def session(model, system, effort):
            nonlocal count
            result = original_session(model, system, effort)
            count += 1
            if count == 3:
                result.script = [{'verdict':'pass','issues':['Optional polish: shorten the label']}, clarified]
            return result
        hub.session = session
        await hub.handle(send('Copy the total from https://reports.example/ as an automation'))
        task = next(iter(hub.tasks.values()))
        await run_task(hub,task)
        assert bool(hub.scripts.list()) is saves
        reviewer = hub.made[2]['session']
        assert len(reviewer.prompts) == 2 and 'Clarify your actual decision' in reviewer.prompts[1]
        assert task.build_journal.state['validated_prefix'] == 2
        assert task.build_journal.state['reviews'][-1]['verdict'] == ('pass' if saves else 'revise')
    asyncio.run(main())


def test_saved_build_report_uses_post_review_facts_not_stale_builder_claims(tmp_path):
    async def main():
        hub = completion_hub(tmp_path,[{'verdict':'pass','issues':[]}] * 2,[])
        hub_session=hub.session
        def session(model,system,effort):
            result=hub_session(model,system,effort)
            for step in result.script:
                if isinstance(step,dict) and step.get('automation') and step.get('done'):
                    step['done']='Not fully met yet: reviews and saving are still pending.'
            return result
        hub.session=session
        await hub.handle(send('Copy the total from https://reports.example/ as an automation'))
        task=next(iter(hub.tasks.values()))
        await run_task(hub,task)
        assert task.status=='done'
        assert task.result.startswith('Reviewed and saved')
        assert 'Verified 2 steps' in task.result and 'Reuse and usability reviews passed' in task.result
        assert 'Verified pages: https://reports.example/' in task.result
        assert 'still pending' not in task.result and 'Not fully met' not in task.result
        assert task.build_journal.state['checkpoint']['result']==task.result
    asyncio.run(main())
