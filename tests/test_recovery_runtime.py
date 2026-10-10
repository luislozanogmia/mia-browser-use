"""Destination observations come from the adapter, never model attestations."""
import asyncio
import copy

import pytest

from tests.test_build_runtime import runtime
from tests.test_build_effects import approved, script

DEST = "https://records.example/"
SPEC = {"collection": "#records", "row": ".row", "id": ".id", "identity": ".identity",
        "fields": {"total": ".total"}, "total_count": "#count"}
CONFIG = {"step": 2, "destination_url": DEST, "operation_identity": "operation-{{total}}",
          "expected_fields": {"total": "{{total}}"}, "selectors": SPEC}


def setup(tmp_path, observation=None):
    hub, browser, task = runtime(tmp_path)
    hub.wait_for_approval = approved
    task.request += " " + DEST
    task.own_tab = True
    browser.next_tab = 300
    journal = task.build_journal
    journal.propose(script())
    journal.record_test(1)
    item = journal.propose(script({"do": "click", "text": "Send report"}))
    task.build_execution = {}
    task.build_step_number = 2
    task.build_recovery_config = copy.deepcopy(CONFIG)
    original = hub.call
    async def call(tool, args):
        if tool == "ghost_records":
            browser.calls.append((tool, args))
            return True, observation or {"destination_url": DEST, "complete": True, "records": []}
        if tool == "ghost_click":
            receipt = next(iter(journal.state["effects"].values()))
            assert receipt["status"] == "uncertain"
            assert receipt["recovery_contract"]["expected_fields"] == {"total": "42"}
            assert receipt["recovery_observer"] == {"destination_url": DEST, "selectors": SPEC}
        return await original(tool, args)
    hub.call = call
    return hub, browser, task, item


def test_actual_baseline_is_pinned_before_click_and_owned_tab_closed(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        await hub.play_step(task, item["steps"][1], {"total": "42"})
        tools = [t for t, _ in browser.calls]
        assert tools.index("ghost_records") < tools.index("ghost_tab_close") < tools.index("ghost_click")
        opened = next(a for t, a in browser.calls if t == "ghost_tab_open")
        assert opened["active"] is False and opened["actor_id"] == task.agent.id
        assert next(a["tab_id"] for t, a in browser.calls if t == "ghost_tab_close") == 300
        assert task.tab_id == 91 and task.build_recovery_contract is None
        receipt = next(iter(task.build_journal.state["effects"].values()))
        assert receipt["status"] == "dispatch_returned"
    asyncio.run(main())


@pytest.mark.parametrize("observation", [
    {"destination_url": DEST, "complete": False, "records": []},
    {"destination_url": "https://wrong.example/", "complete": True, "records": []},
    {"destination_url": DEST, "complete": True, "records": [], "confirmed": True},
    {"destination_url": DEST, "complete": True, "records": [{"id": "old", "identity": "operation-42", "fields": {"total": "42"}}]},
])
def test_partial_wrong_or_existing_baseline_holds_without_dispatch(tmp_path, observation):
    async def main():
        hub, browser, task, item = setup(tmp_path, observation)
        with pytest.raises(RuntimeError, match="baseline held"):
            await hub.play_step(task, item["steps"][1], {"total": "42"})
        assert not any(t == "ghost_click" for t, _ in browser.calls)
        assert not task.build_journal.state.get("effects")
        assert any(t == "ghost_tab_close" for t, _ in browser.calls)
    asyncio.run(main())


def test_rehearsal_skips_baseline_and_write(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        out = await hub.play_step(task, item["steps"][1], {"total": "42"}, dry=True)
        assert "skipped" in out
        assert not any(t in {"ghost_records", "ghost_tab_open", "ghost_click"} for t, _ in browser.calls)
    asyncio.run(main())


def test_confirmation_pinned_observer_only_and_never_replays(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        key = await hub.prepare_build_effect(task, item["steps"][1], {"total": "42"})
        task.build_recovery_config["selectors"]["collection"] = "#wrong"
        task.build_continuation = None
        before = copy.deepcopy(task.build_journal.state)
        original = hub.call
        async def after(tool, args):
            if tool == "ghost_records":
                browser.calls.append((tool, args))
                assert args["spec"] == SPEC
                return True, {"destination_url": DEST, "complete": True, "records": [
                    {"id": "new", "identity": "operation-42", "fields": {"total": "42"}}]}
            return await original(tool, args)
        hub.call = after
        browser.calls.clear()
        out = await hub.reconcile_build(task, {"receipt": key})
        assert "Destination confirmed" in out and "restoration remains held" in out
        assert task.build_journal.state["effects"][key]["status"] == "uncertain"
        assert task.build_journal.state["validated_prefix"] == before["validated_prefix"]
        assert not any(t in {"ghost_click", "ghost_sheet_append", "ghost_key", "ghost_fill"} for t, _ in browser.calls)
        assert task.build_continuation is None
        browser.calls.clear()
        out = await hub.reconcile_build(task, {"receipt": key, "observation": {"confirmed": True}})
        assert "forbidden" in out and not browser.calls
    asyncio.run(main())


def test_legacy_receipt_holds_before_browser_read(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        key = task.build_journal.begin_effect({}, "", 2, item["steps"][1])
        browser.calls.clear()
        assert "held" in await hub.reconcile_build(task, {"receipt": key})
        assert not browser.calls
    asyncio.run(main())


def test_cancelled_create_is_identified_and_closed(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        original = hub.call
        async def call(tool, args):
            if tool == "ghost_tab_open":
                started.set()
                await release.wait()
            return await original(tool, args)
        hub.call = call
        job = asyncio.create_task(hub.recovery_observation(task, {"destination_url": DEST, "selectors": SPEC}))
        await started.wait()
        job.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert [a["tab_id"] for t, a in browser.calls if t == "ghost_tab_close"] == [300]
        assert task.tab_id == 91
    asyncio.run(main())


@pytest.mark.parametrize("change", [
    {"confirmed": True}, {"step": True}, {"step": 1}, {"destination_url": "https://user:pass@example/"},
    {"selectors": {**SPEC, "fields": {"extra": ".extra"}}}, {"operation_identity": 3},
])
def test_invalid_config_rejected_before_calls(tmp_path, change):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        out = await hub.test_automation(task, {"automation": item, "recovery": {**CONFIG, **change}})
        assert "Not tested" in out and not browser.calls
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())


def test_rejected_destination_never_opens_or_dispatches(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        task.request = "https://reports.example/"
        async def reject(*args):
            return False
        hub.wait_for_approval = reject
        with pytest.raises(RuntimeError, match="rejected"):
            await hub.prepare_build_effect(task, item["steps"][1], {"total": "42"})
        assert not browser.calls
        assert not task.build_journal.state.get("effects")
    asyncio.run(main())


def test_cancelled_observation_closes_only_temporary_tab(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        reading = asyncio.Event()
        original = hub.call
        async def call(tool, args):
            if tool == "ghost_records":
                reading.set()
                await asyncio.Future()
            return await original(tool, args)
        hub.call = call
        job = asyncio.create_task(hub.prepare_build_effect(task, item["steps"][1], {"total": "42"}))
        await reading.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert [a["tab_id"] for t, a in browser.calls if t == "ghost_tab_close"] == [300]
        assert not task.build_journal.state.get("effects")
    asyncio.run(main())


def test_runtime_witness_mismatched_values_cannot_pin_intent(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        actual_begin = hub.begin_build_effect
        def altered(task, step, values):
            task.build_recovery_contract["values"]["total"] = "model replacement"
            return actual_begin(task, step, values)
        hub.begin_build_effect = altered
        with pytest.raises(RuntimeError, match="witness differs"):
            await hub.play_step(task, item["steps"][1], {"total": "42"})
        assert not any(t == "ghost_click" for t, _ in browser.calls)
        assert not task.build_journal.state.get("effects")
    asyncio.run(main())


def test_test_wrapper_clears_recovery_after_failure_and_stop(tmp_path):
    async def main():
        hub, browser, task, item = setup(tmp_path)
        async def inner(task, args, config):
            task.build_recovery_config = config
            task.build_recovery_contract = {"stale": True}
            raise asyncio.CancelledError()
        hub._test_automation = inner
        with pytest.raises(asyncio.CancelledError):
            await hub.test_automation(task, {"automation": item, "recovery": CONFIG})
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())
