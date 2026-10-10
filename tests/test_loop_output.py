"""Saved loops report actual per-item copies, including before failure or Stop."""
import asyncio
import json

import pytest

import ghost_chat
from automations import clean
from ghost_chat import Agent, Task
from tests.test_automations import ListBrowser, LOOP, play_hub


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ghost_chat, "PLAY_RETRIES", (0, 0, 0))


def setup(tmp_path):
    browser = ListBrowser()
    hub = play_hub(browser, tmp_path)
    item = hub.scripts.add(clean({**LOOP, "steps": LOOP["steps"][:2] + [
        {"do": "copy", "css": "h1", "as": "name"}]}))
    return hub, browser, item


def test_saved_play_reports_each_actual_value_and_preserves_history(tmp_path):
    async def main():
        hub, browser, item = setup(tmp_path)
        task = await hub.play(item, {"template": "unused"})
        await asyncio.wait_for(task.job, 3)
        assert task.status == "done", task.result
        assert [r["copied"]["name"] for r in task.play_results] == ["Ana Silva", "Bo Chen", "Cy Diaz"]
        assert all(r["status"] == "completed" for r in task.play_results)
        assert "Ana Silva" in task.result and "Cy Diaz" in task.result
        assert task.view()["play_results"] == task.play_results
        assert len(hub.scripts.get(item["id"])["done"]) == 3
    asyncio.run(main())


def test_failed_read_is_incomplete_and_not_previous_item_value(tmp_path):
    async def main():
        hub, browser, item = setup(tmp_path)
        original = hub.call
        async def call(command, args):
            if command == "ghost_read" and args.get("selector") == "h1" and "bo-chen" in browser.url:
                return False, "read failed"
            return await original(command, args)
        hub.call = call
        task = await hub.play(item, {"template": "unused"})
        await asyncio.wait_for(task.job, 3)
        failed = task.play_results[1]
        assert "bo-chen" in failed["link"] and failed["status"] == "incomplete"
        assert failed["copied"] == {} and "read failed" in failed["error"]
        assert task.play_finished == 2
        assert len(hub.scripts.get(item["id"])["done"]) == 2
    asyncio.run(main())


def test_stop_keeps_completed_outputs(tmp_path):
    async def main():
        hub, browser, item = setup(tmp_path)
        waiting = asyncio.Event()
        original = hub.call
        async def call(command, args):
            if command == "ghost_navigate" and "bo-chen" in args.get("url", ""):
                waiting.set()
                await asyncio.Future()
            return await original(command, args)
        hub.call = call
        task = await hub.play(item, {"template": "unused"})
        await asyncio.wait_for(waiting.wait(), 3)
        task.job.cancel()
        await task.job
        assert task.status == "stopped" and task.play_finished == 1
        assert "Ana Silva" in task.result and "Bo Chen" not in task.result
        assert len(task.play_results) == 1
    asyncio.run(main())


def test_use_automation_loop_exposes_all_items_not_final_variables(tmp_path):
    async def main():
        hub, browser, item = setup(tmp_path)
        agent = Agent(1, 91)
        task = Task(1, "Read", "Read names", "", "ask", "parallel", "", agent)
        task.approved_urls.add(item["steps"][0]["url"])
        hub.agents[agent.id], hub.tasks[task.id] = agent, task
        result = await hub.use_automation(task, {"name": item["name"]})
        assert "Ana Silva" in result and "Bo Chen" in result and "Cy Diaz" in result
        assert "\nCopied:" not in result
        assert len(task.play_results) == 3
    asyncio.run(main())


def test_result_storage_is_bounded_and_omissions_explicit(tmp_path):
    async def main():
        hub, browser, item = setup(tmp_path)
        task = Task(1, "Read", "Read names", "", "ask", "parallel", "", Agent(1, 91))
        for n in range(100):
            await hub.collect_play_result(task, {"scope": "loop_item", "link": f"https://test/{n}",
                                                "passed": True, "copied": {"name": "x" * 10000}})
        assert len(json.dumps(task.play_results, ensure_ascii=False)) <= ghost_chat.REPORT_CHARS
        assert len(task.play_results) + task.play_results_omitted == 100
        assert "omitted" in hub.play_result_report(task)
        assert all(len(r["copied"]["name"]) == 1000 for r in task.play_results)
    asyncio.run(main())


def test_use_automation_stop_preserves_completed_outputs_in_worker_report(tmp_path):
    async def main():
        hub, browser, item = setup(tmp_path)
        task = Task(1, "Read", "Read names", "", "ask", "parallel", "", Agent(1, 91))
        task.approved_urls.add(item["steps"][0]["url"])
        hub.agents[task.agent.id], hub.tasks[task.id] = task.agent, task
        waiting = asyncio.Event()
        original = hub.call
        async def call(command, args):
            if command == "ghost_navigate" and "bo-chen" in args.get("url", ""):
                waiting.set()
                await asyncio.Future()
            return await original(command, args)
        hub.call = call
        job = asyncio.create_task(hub.use_automation(task, {"name": item["name"]}))
        await asyncio.wait_for(waiting.wait(), 3)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert "1 completed links" in task.found and "Ana Silva" in task.found
        assert "Bo Chen" not in task.found and len(task.play_results) == 1
        assert len(hub.scripts.get(item["id"])["done"]) == 1
    asyncio.run(main())
