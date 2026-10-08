"""Client pagination stays in its own tab; item tab ownership survives failure and Stop."""
import asyncio

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
    browser.tabs[91] = LOOP["steps"][0]["url"]
    hub = play_hub(browser, tmp_path)
    item = hub.scripts.add(clean({**LOOP, "steps": LOOP["steps"][:2] + [
        {"do": "copy", "css": "h1", "as": "name"}]}))
    task = Task(1, "Read names", "Read every name", "", "play", "parallel", "", Agent(1, 91))
    task.agent.opened, task.agent.host = False, "www.linkedin.com"
    return hub, browser, item, task


def assert_restored(browser, task):
    assert task.tab_id == task.agent.tab_id == 91
    assert browser.tabs == {91: LOOP["steps"][0]["url"]}
    assert not task.agent.opened and task.agent.host == "www.linkedin.com"
    assert [a["tab_id"] for c, a in browser.calls if c == "ghost_tab_close"] == [92]


def test_same_url_client_pages_preserve_list_and_reuse_one_item_tab(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        events = []
        async def observe(event):
            events.append(event)
        finished, skipped = await hub.play_each(task, item, {}, observe=observe)
        assert finished == 3 and not skipped
        assert [e["copied"]["name"] for e in events if e["scope"] == "loop_item"] == [
            "Ana Silva", "Bo Chen", "Cy Diaz"]
        pages = [e for e in events if e["scope"] == "loop_page"]
        assert len(pages) == 3 and len({e["url"] for e in pages}) == 1
        assert len([c for c, _ in browser.calls if c == "ghost_tab_open"]) == 1
        assert all(a["tab_id"] == 92 for c, a in browser.calls if c == "ghost_navigate")
        assert all(a["tab_id"] == 91 for c, a in browser.calls if c == "ghost_click")
        assert_restored(browser, task)
    asyncio.run(main())


def test_all_done_pages_traverse_without_opening_or_closing_a_tab(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        for name in ["ana-silva", "bo-chen", "cy-diaz"]:
            hub.scripts.mark_done(item["id"], f"https://www.linkedin.com/in/{name}")
        assert await hub.play_each(task, item, {}) == (0, [])
        assert task.tab_id == 91 and browser.page == 3
        assert not any(c in {"ghost_tab_open", "ghost_tab_close", "ghost_navigate"} for c, _ in browser.calls)
    asyncio.run(main())


def test_copy_only_body_visits_every_current_link_instead_of_reusing_previous_page(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        item = hub.scripts.add(clean({**LOOP, "steps": [LOOP["steps"][0],
            {"do": "copy", "css": "h1", "as": "name"}]}))
        events = []
        async def observe(event):
            events.append(event)
        assert await hub.play_each(task, item, {}, observe=observe) == (3, [])
        assert [e["copied"]["name"] for e in events if e["scope"] == "loop_item"] == [
            "Ana Silva", "Bo Chen", "Cy Diaz"]
        assert_restored(browser, task)
    asyncio.run(main())


def test_failure_closes_only_item_tab_and_keeps_list_and_completed_output(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        original = hub.call
        async def call(command, args):
            if command == "ghost_read" and args.get("selector") == "h1" and "bo-chen" in browser.url:
                return False, "rejected reading this item"
            return await original(command, args)
        hub.call = call
        with pytest.raises(RuntimeError, match="rejected"):
            await hub.play_each(task, item, {}, observe=lambda e: hub.collect_play_result(task, e))
        assert task.play_finished == 1
        assert task.play_results[0]["copied"] == {"name": "Ana Silva"}
        assert task.play_results[1]["copied"] == {} and task.play_results[1]["status"] == "incomplete"
        assert len(hub.scripts.get(item["id"])["done"]) == 1
        assert_restored(browser, task)
    asyncio.run(main())


def test_stop_during_item_restores_list_and_closes_temporary_tab(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        original, started = hub.call, asyncio.Event()
        async def call(command, args):
            if command == "ghost_navigate" and "bo-chen" in args.get("url", ""):
                started.set()
                await asyncio.Future()
            return await original(command, args)
        hub.call = call
        job = asyncio.create_task(hub.play_each(task, item, {}))
        await asyncio.wait_for(started.wait(), 1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert task.play_finished == 1
        assert_restored(browser, task)
    asyncio.run(main())


def test_stop_during_tab_creation_waits_for_created_id_then_closes_it(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        original, started, release = hub.call, asyncio.Event(), asyncio.Event()
        async def call(command, args):
            if command == "ghost_tab_open":
                started.set()
                await release.wait()
            return await original(command, args)
        hub.call = call
        job = asyncio.create_task(hub.play_each(task, item, {}))
        await asyncio.wait_for(started.wait(), 1)
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert task.play_finished == 0 and not hub.scripts.get(item["id"])["done"]
        assert_restored(browser, task)
    asyncio.run(main())


def test_external_list_mutation_is_detected_before_next(tmp_path):
    async def main():
        hub, browser, item, task = setup(tmp_path)
        async def observe(event):
            if event["scope"] == "loop_item" and event["link"].endswith("bo-chen"):
                browser.page = 2
        with pytest.raises(RuntimeError, match="preserved list changed"):
            await hub.play_each(task, item, {}, observe=observe)
        assert not any(c == "ghost_click" for c, _ in browser.calls)
        assert_restored(browser, task)
    asyncio.run(main())
