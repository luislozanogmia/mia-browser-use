"""Repeating builds must prove the full production loop without changing saved history."""
import asyncio

import pytest

import automations
import ghost_chat
from automation_build import BuildJournal
from ghost_chat import Agent, Task
from tests.test_automations import ListBrowser, LOOP, play_hub


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ghost_chat, "PLAY_RETRIES", (0, 0, 0))


def runtime(tmp_path, browser=None):
    browser = browser or ListBrowser()
    hub = play_hub(browser, tmp_path)
    item = {**LOOP, "steps": [*LOOP["steps"][:2], {"do": "copy", "css": "h1", "as": "name"}]}
    agent = Agent(1, 91)
    task = Task(1, "Build list", "Read every person's name", item["steps"][0]["url"],
                "build", "parallel", "claude-sonnet-5-5", agent)
    task.build = {"name": item["name"]}
    task.request = item["steps"][0]["url"]
    task.build_journal = BuildJournal(root=tmp_path / "builds", plan=["Read all names"])
    hub.agents[agent.id], hub.tasks[task.id] = agent, task
    return hub, browser, task, item


async def prefixes(hub, task, item):
    for n in range(1, len(item["steps"]) + 1):
        result = await hub.test_automation(task, {"automation": {**item, "steps": item["steps"][:n]},
                                                 "inputs": {"template": "unused"}})
        assert "Test passed" in result, result


def test_one_item_proof_cannot_complete_or_review_a_loop(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        await prefixes(hub, task, item)
        assert "scope=loop" in hub.check_build(task, item)
        assert task.draft is None
        task.draft = automations.clean(item)
        assert "scope=loop" in await hub.review_build(task)
        assert not task.build_journal.state["reviews"]
    asyncio.run(main())


def test_loop_scope_rejects_whole_script_shortcut_and_unverified_prefix(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        result = await hub.test_automation(task, {"automation": item, "scope": "loop"})
        assert "validate the current step" in result and not browser.calls
        one = {**item, "steps": item["steps"][:1]}
        await hub.test_automation(task, {"automation": one, "inputs": {"template": "unused"}})
        calls = list(browser.calls)
        result = await hub.test_automation(task, {"automation": {**item, "steps": item["steps"][:2]}, "scope": "loop"})
        assert "validate every candidate prefix" in result and browser.calls == calls
    asyncio.run(main())


def test_full_loop_records_each_actual_output_and_pages_without_saved_done_mutation(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        await prefixes(hub, task, item)
        saved = hub.scripts.add(automations.clean(item))
        hub.scripts.mark_done(saved["id"], "https://www.linkedin.com/in/ana-silva")
        before = hub.scripts.get(saved["id"])
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused"}})
        assert "Loop test passed: 3 items executed across 3 list pages" in result
        assert hub.scripts.get(saved["id"]) == before
        cases = task.build_journal.state["cases"]
        items = [c for c in cases if c.get("metadata", {}).get("scope") == "loop_item"]
        assert [c["copied"]["name"] for c in items] == ["Ana Silva", "Bo Chen", "Cy Diaz"]
        assert all(c["link"] in c["outcome"]["url"] for c in items)
        assert cases[-1]["metadata"]["scope"] == "loop" and cases[-1]["passed"]
        assert len(cases[-1]["outcome"]["pages"]) == 3
        assert task.current_url == browser.tabs[task.tab_id]
        assert "search" in task.current_url
        assert not task.control_approved and hub.args(task, {})["expected_url"] == ""
        assert hub.check_build(task, item) == ""
        loaded = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "builds")
        assert loaded.state["cases"][-1] == cases[-1]
        # Changing an apparently cosmetic field changes the canonical candidate identity.
        assert "scope=loop" in hub.check_build(task, {**item, "about": "New help text"})
    asyncio.run(main())


def test_failure_on_later_item_holds_first_failed_step_and_cannot_reuse_old_loop_pass(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        await prefixes(hub, task, item)
        assert "passed" in await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused"}})
        browser.page = 1
        call = hub.call

        async def fail_bo(command, args):
            if command == "ghost_read" and args.get("selector") == "h1" and "bo-chen" in browser.url:
                return True, {"url": browser.url, "content": ""}
            return await call(command, args)

        hub.call = fail_bo
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused"}})
        assert "Loop test failed: 1 items completed" in result and "no visible text" in result
        assert not task.tested and task.build_journal.state["failed_step"] == 3
        assert not task.build_journal.state["cases"][-1]["passed"]
        assert "hasn't passed" in hub.check_build(task, item)
        item_cases = [c for c in task.build_journal.state["cases"] if c.get("metadata", {}).get("scope") == "loop_item"]
        failed = item_cases[-1]
        assert failed["link"].endswith("bo-chen") and failed["copied"] == {}
        assert failed["metadata"]["failed_step"] == 3
    asyncio.run(main())


def test_cycle_does_not_count_as_successful_pagination(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        await prefixes(hub, task, item)
        call = hub.call

        async def cycle(command, args):
            if command == "ghost_click" and args.get("choice") == 4:
                return True, {"clicked": True}  # Next never changes the list.
            return await call(command, args)

        hub.call = cycle
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused"}})
        assert "Loop test failed" in result and "already checked" in result
        assert not task.tested and not task.build_journal.state["cases"][-1]["passed"]
        task.tested = hub.build_key(automations.clean(item))  # even a later prefix pass isn't full-list proof
        assert "scope=loop" in hub.check_build(task, item)
    asyncio.run(main())


def test_empty_list_is_observed_but_does_not_prove_reuse(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        await prefixes(hub, task, item)
        browser.page = 3
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused"}})
        assert "Loop test failed: 0 items completed" in result
        assert task.build_journal.state["cases"][-1]["metadata"]["item_count"] == 0
        assert not task.tested
    asyncio.run(main())


def test_live_loop_uses_normal_approvals_and_rehearsal_skipped_effects_never_pass(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        item = {**item, "steps": [*item["steps"], {"do": "type", "text": "Write a message", "value": "{{template}}"}, {"do": "click", "text": "Send"}]}
        await prefixes(hub, task, {**item, "steps": item["steps"][:4]})
        approvals = []

        async def approve(task, question, choice=None):
            approvals.append(question)
            return True

        hub.wait_for_approval = approve
        result = await hub.test_automation(task, {"automation": item, "mode": "live", "inputs": {"template": "unused"}})
        assert "Test passed" in result and len(approvals) == 2
        approvals.clear()
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused"}})
        assert "Hold for destination reconciliation" in result
        assert not task.tested and not approvals
        # A genuinely different disposable sample permits fresh effects, with ordinary approvals.
        before = list(browser.calls)
        async def reject(task, question, choice=None):
            return False
        hub.wait_for_approval = reject
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live", "inputs": {"template": "different disposable sample"}})
        assert "live execution was rejected" in result and browser.calls == before and not task.tested
        hub.wait_for_approval = approve
        approvals.clear()
        result = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live", "inputs": {"template": "different disposable sample"}})
        assert "Loop test passed: 3 items" in result and len(approvals) == 4
        assert len(task.build_journal.state["effects"]) == 4  # one prefix plus three fresh-sample items
        assert hub.check_build(task, item) == ""
    asyncio.run(main())


def test_new_loop_observations_require_reviewers_to_see_fresh_evidence(tmp_path):
    async def main():
        hub, browser, task, item = runtime(tmp_path)
        await prefixes(hub, task, item)
        task.build_journal.record_review("adversarial", "pass", "older evidence")
        await hub.test_automation(task, {"automation": item, "scope": "loop", "inputs": {"template": "unused", "unused_extra": "do not store"}})
        assert not task.build_journal.state["reviews"]
        assert task.build_journal.state["cases"][-1]["inputs"] == {"template": "unused"}
    asyncio.run(main())
