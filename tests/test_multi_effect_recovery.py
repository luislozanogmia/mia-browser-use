"""Per-step destination witnesses exercised through the actual Python executor.

The browser transport is a deterministic fake, not live Chrome evidence.
"""
import asyncio
import copy

import pytest

from tests.test_loop_build import runtime
from tests.test_confirmed_loop_continuation import LINK

DESTINATIONS = ["https://records.example/first", "https://records.example/second"]


def configs():
    return [{"step": 4 + index, "destination_url": destination,
             "operation_identity": "{{link}}", "expected_fields": {"payload": f"write-{index}"},
             "selectors": {"collection": f"#records-{index}", "row": ".row", "id": ".id",
                           "identity": ".identity", "fields": {"payload": f".payload-{index}"},
                           "total_count": f"#count-{index}"}}
            for index, destination in enumerate(DESTINATIONS)]


async def setup(tmp_path, *, partial_second=False, omit_second=False, target="Send"):
    hub, browser, task, original = runtime(tmp_path)
    item = {**original, "steps": [*original["steps"], {"do": "click", "text": target},
                                 {"do": "click", "text": target}]}
    task.request += " " + " ".join(DESTINATIONS)
    approvals, rows, writes, observations = [], [[], []], [], []
    config = configs()
    async def approve(task, question, choice=None):
        approvals.append(question)
        return True
    hub.wait_for_approval = approve
    call = hub.call
    async def transport(tool, args):
        if tool == "ghost_records":
            index = next(i for i, conf in enumerate(config) if conf["selectors"] == args["spec"])
            observations.append(index)
            browser.calls.append((tool, copy.deepcopy(args)))
            return True, {"destination_url": DESTINATIONS[index], "complete": not (partial_second and index == 1),
                          "records": copy.deepcopy(rows[index])}
        if tool == "ghost_click" and args.get("choice") in {1, 3}:
            index = task.build_step_number - 4
            receipt = list(task.build_journal.state["effects"].values())[-1]
            assert receipt["status"] == "uncertain"
            if not (omit_second and index == 1):
                assert receipt["recovery_observer"] == {
                    "destination_url": DESTINATIONS[index], "selectors": config[index]["selectors"]}
                assert receipt["recovery_contract"]["expected_fields"] == {"payload": f"write-{index}"}
                assert receipt["recovery_contract"]["baseline"] == {
                    "destination_url": DESTINATIONS[index], "complete": True, "records": rows[index]}
            link = browser.tabs[args["tab_id"]]
            assert receipt.get("recovery_contract", {}).get("operation_identity", link) == link
            writes.append((link, index))
            rows[index].append({"id": f"row-{index}-{len(rows[index])}", "identity": link,
                                "fields": {"payload": f"write-{index}"}})
        return await call(tool, args)
    hub.call = transport
    return hub, browser, task, item, config, approvals, rows, writes, observations


async def run_prefix(hub, task, item, config, *, omit_second=False, omit_all_second=False):
    out = ""
    for n in range(1, len(item["steps"]) + 1):
        args = {"automation": {**item, "steps": item["steps"][:n]}, "mode": "live",
                "inputs": {"template": "unused"}}
        if n >= 4 and not (n == 5 and omit_all_second):
            args["recovery"] = config[:1] if n == 4 or omit_second else config
        out = await hub.test_automation(task, args)
        if "Test passed" not in out:
            break
    return out


@pytest.mark.parametrize("target", ["Send", "Message"])
def test_two_write_prefix_pins_independent_baselines_and_freshly_confirms_both(tmp_path, target):
    async def main():
        hub, browser, task, item, config, approvals, rows, writes, observations = await setup(tmp_path, target=target)
        assert "Test passed" in await run_prefix(hub, task, item, config)
        assert writes == [(LINK, 0), (LINK, 1)]
        assert observations == [0, 1]
        receipts = copy.deepcopy(task.build_journal.state["effects"])
        approvals.clear()
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}, "recovery": config})
        assert "3 items verified" in out and "1 confirmed prefix item retained; 2 new items executed" in out, out
        assert observations == [0, 1, 0, 1, 0, 1, 0, 1]
        assert len(writes) == 6 and len(set(writes)) == 6
        assert [len(records) for records in rows] == [3, 3]
        assert len(approvals) == 5  # one live test plus four actual action approvals
        effects = task.build_journal.state["effects"]
        assert len(effects) == 6
        for key in receipts:
            assert effects[key]["confirmation"]["contract_fingerprint"] == receipts[key]["recovery_fingerprint"]
        first_event = task.build_journal.state["cases"][-1]["outcome"]["items"][0]
        assert first_event["execution"] == "retained"
        assert {entry["receipt"] for entry in first_event["fresh_confirmation"]} == set(receipts)
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())


@pytest.mark.parametrize("invalid", [[], [None], [[{}]], [{}] * 61,
    [configs()[0], configs()[0]], [configs()[0], {**configs()[1], "step": 6}],
    [configs()[0], {**configs()[1], "step": 3}], [configs()[0], {**configs()[1], "step": True}],
    [configs()[0], {**configs()[1], "confirmed": True}]])
def test_invalid_list_rejected_before_proposal_or_browser(tmp_path, invalid):
    async def main():
        hub, browser, task, item, *_ = await setup(tmp_path)
        before = copy.deepcopy(task.build_journal.state)
        out = await hub.test_automation(task, {"automation": item, "recovery": invalid})
        assert "Not tested" in out
        assert task.build_journal.state == before and not browser.calls
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())


def test_second_incomplete_baseline_holds_before_second_write(tmp_path):
    async def main():
        hub, browser, task, item, config, approvals, rows, writes, observations = await setup(tmp_path, partial_second=True)
        out = await run_prefix(hub, task, item, config)
        assert "Test failed" in out and "baseline held" in out, out
        assert writes == [(LINK, 0)] and observations == [0, 1]
        assert len(task.build_journal.state["effects"]) == 1
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())


@pytest.mark.parametrize("omit_all", [False, True])
def test_missing_second_witness_holds_before_second_dispatch(tmp_path, omit_all):
    async def main():
        hub, browser, task, item, config, approvals, rows, writes, observations = await setup(tmp_path, omit_second=True)
        out = await run_prefix(hub, task, item, config, omit_second=True, omit_all_second=omit_all)
        assert "Test failed" in out and "needs its own observer" in out, out
        assert writes == [(LINK, 0)] and observations == [0]
        assert len(task.build_journal.state["effects"]) == 1
        assert next(iter(task.build_journal.state["effects"].values()))["status"] == "dispatch_returned"
    asyncio.run(main())


def test_swapped_configuration_cannot_replace_pinned_prefix_observers(tmp_path):
    async def main():
        hub, browser, task, item, config, approvals, rows, writes, observations = await setup(tmp_path)
        assert "Test passed" in await run_prefix(hub, task, item, config)
        swapped = [{**config[1], "step": 4}, {**config[0], "step": 5}]
        async def reject_new_execution(*args):
            return False
        hub.wait_for_approval = reject_new_execution
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}, "recovery": swapped})
        assert "live execution was rejected" in out, out
        # Reconciliation uses only the old pinned observers even when the requested configs are swapped.
        assert observations[2:] == [0, 1]
        assert writes == [(LINK, 0), (LINK, 1)]
    asyncio.run(main())


def test_rehearsal_skips_both_observers_and_writes(tmp_path):
    async def main():
        hub, browser, task, item, config, approvals, rows, writes, observations = await setup(tmp_path)
        journal = task.build_journal
        for n in range(1, 4):
            journal.propose({**item, "steps": item["steps"][:n]})
            journal.record_test(n)
        # Proposals stay one-step-at-a-time; pre-propose and validate the fourth step for this dry suffix check.
        journal.propose({**item, "steps": item["steps"][:4]})
        journal.record_test(4)
        out = await hub.test_automation(task, {"automation": item, "recovery": config,
                                              "inputs": {"template": "unused"}})
        assert "steps [4, 5] were skipped" in out, out
        assert not writes and not observations and not journal.state["effects"]
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())


def test_stop_clears_all_configs_and_single_use_witness(tmp_path):
    async def main():
        hub, browser, task, item, config, *_ = await setup(tmp_path)
        async def inner(task, args, configs):
            task.build_recovery_config = configs
            task.build_recovery_contract = {"stale": True}
            raise asyncio.CancelledError()
        hub._test_automation = inner
        with pytest.raises(asyncio.CancelledError):
            await hub.test_automation(task, {"automation": item, "recovery": config})
        assert task.build_recovery_config is None and task.build_recovery_contract is None
    asyncio.run(main())


@pytest.mark.parametrize("same_destination", [False, True])
def test_all_exact_destinations_approved_together_before_dispatch(tmp_path, same_destination):
    async def main():
        hub, browser, task, item, config, *_ = await setup(tmp_path)
        journal = task.build_journal
        for n in range(1, 5):
            journal.propose({**item, "steps": item["steps"][:n]})
            journal.record_test(n)
        if same_destination:
            config[1]["destination_url"] = DESTINATIONS[0]
        expected = list(dict.fromkeys(entry["destination_url"] for entry in config))
        calls = []
        async def urls(task, addresses):
            calls.append(addresses)
            return addresses != expected
        hub.approve_urls = urls
        out = await hub.test_automation(task, {"automation": item, "mode": "live", "recovery": config,
                                              "inputs": {"template": "unused"}})
        assert "recovery destination rejected" in out
        assert calls[-1] == expected
        assert not browser.calls and not journal.state["effects"]
    asyncio.run(main())
