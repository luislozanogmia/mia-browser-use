"""A returned write can seed a loop only after fresh, pinned destination proof."""
import asyncio
import copy

import pytest

import automations
from automation_recovery import freeze_contract
from tests.test_loop_build import runtime
from tests.test_build_effects import approved

DEST = "https://records.example/"
SPEC = {"collection": "#records", "row": ".row", "id": ".id", "identity": ".identity",
        "fields": {"payload": ".payload"}, "total_count": "#count"}
LINK = "https://www.linkedin.com/in/ana-silva"


async def prepared(tmp_path, sole=False):
    hub, browser, task, original = runtime(tmp_path)
    hub.wait_for_approval = approved
    task.request += " " + DEST
    task.own_tab = True
    item = {**original, "steps": [*original["steps"], {"do": "click", "text": "Send report"}]}
    if sole:
        item = {**item, "each": {k: v for k, v in item["each"].items() if k != "next"}}
    journal = task.build_journal
    for n in range(1, len(item["steps"]) + 1):
        journal.propose({**item, "steps": item["steps"][:n]})
        journal.record_test(n)
    item = automations.clean(item)
    sample = {"template": "unused"}
    values = {**sample, "link": LINK, "name": "Ana Silva"}
    contract = freeze_contract(DEST, LINK, {"payload": "fixed"},
        {"destination_url": DEST, "complete": True, "records": []}, runtime_source="chrome:ghost_records")
    key = journal.begin_effect(sample, LINK, 4, item["steps"][3],
        recovery_contract=contract, recovery_observer={"destination_url": DEST, "selectors": SPEC})
    journal.effect_returned(key)
    browser.tabs[91] = LINK
    task.build_execution = sample
    await hub.checkpoint_build_continuation(task, item["steps"], values, 4)
    task.build_execution = None
    await hub.record_build_case(task, item, sample, values, False, True, 4, ["Verified full prefix"])
    call = hub.call
    observations = []
    async def observed(tool, args):
        if tool == "ghost_records":
            observations.append(copy.deepcopy(args))
            browser.calls.append((tool, args))
            return True, {"destination_url": DEST, "complete": True,
                          "records": [{"id": "prefix-row", "identity": LINK, "fields": {"payload": "fixed"}}]}
        return await call(tool, args)
    hub.call = observed
    # Each remaining item's write still follows ordinary journal intent semantics.
    step = hub.play_step
    writes = []
    async def write(task, action, values, dry=False):
        if action["do"] == "click" and action.get("text") == "Send report":
            writes.append(values["link"])
            receipt = hub.begin_build_effect(task, action, values)
            journal.effect_returned(receipt)
            return ""
        return await step(task, action, values, dry)
    hub.play_step = write
    return hub, browser, task, item, key, writes, observations


def test_confirmed_prefix_retained_and_every_remaining_item_executed(tmp_path):
    async def main():
        hub, browser, task, item, key, writes, observations = await prepared(tmp_path)
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}})
        assert "3 items verified" in out and "1 confirmed prefix item retained; 2 new items executed" in out, out
        assert writes == ["https://www.linkedin.com/in/bo-chen", "https://www.linkedin.com/in/cy-diaz"]
        assert len(observations) == 1 and observations[0]["spec"] == SPEC
        assert task.build_journal.state["effects"][key]["confirmation"]
        items = task.build_journal.state["cases"][-1]["outcome"]["items"]
        assert [event["execution"] for event in items] == ["retained", "executed", "executed"]
        assert items[0]["source_case"]["outcome"]["url"] == LINK
        assert items[0]["fresh_confirmation"][0]["receipt"] == key
        assert task.build_continuation is None
        assert len(browser.tabs) == 1
        # A completed loop has additional write receipts; it cannot seed another loop/retry.
        again = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                                "inputs": {"template": "unused"}})
        assert "Hold for destination reconciliation" in again
        assert len(writes) == 2
    asyncio.run(main())


@pytest.mark.parametrize("disruption", ["unpinned", "uncertain", "read_error", "case_inputs", "candidate",
                                       "rehearsal", "different_link", "tab", "page", "restart", "case_readback", "copied"])
def test_invalid_proof_holds_with_zero_new_writes(tmp_path, disruption):
    async def main():
        hub, browser, task, item, key, writes, observations = await prepared(tmp_path)
        journal = task.build_journal
        mode = "live"
        if disruption == "unpinned":
            journal.state["effects"][key].pop("recovery_contract")
        elif disruption == "uncertain":
            journal.state["effects"][key]["status"] = "uncertain"
        elif disruption == "read_error":
            journal.state["cases"][-1]["outcome"]["read_error"] = "Failed"
        elif disruption == "case_inputs":
            journal.state["cases"][-1]["inputs"] = {"template": "other"}
        elif disruption == "candidate":
            item = {**item, "about": "changed"}
        elif disruption == "rehearsal":
            mode = "rehearsal"
        elif disruption == "different_link":
            journal.state["effects"][key]["link"] = "https://other.example/"
        elif disruption == "tab":
            task.tab_id = 999
        elif disruption == "page":
            browser.tabs[91] = "https://www.linkedin.com/in/bo-chen"
        elif disruption == "restart":
            task.build_continuation = None
        elif disruption == "case_readback":
            journal.state["cases"][-1]["outcome"]["content"] = "different readback"
        elif disruption == "copied":
            journal.state["cases"][-1]["copied"]["name"] = "different"
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": mode,
                                              "inputs": {"template": "unused"}})
        assert "held" in out.lower() or "hold" in out.lower(), out
        assert not writes
    asyncio.run(main())


def test_partial_observer_and_missing_first_page_member_never_write(tmp_path):
    async def main():
        hub, browser, task, item, key, writes, observations = await prepared(tmp_path)
        original = hub.call
        async def partial(tool, args):
            if tool == "ghost_records":
                return True, {"destination_url": DEST, "complete": False, "records": []}
            return await original(tool, args)
        hub.call = partial
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}})
        assert "held" in out and not writes
        hub, browser, task, item, key, writes, observations = await prepared(tmp_path / "second")
        browser.page = 2
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}})
        assert "absent from the first actual list page" in out and not writes
    asyncio.run(main())


def test_sole_item_retained_after_confirmation_and_actual_list_membership(tmp_path):
    async def main():
        hub, browser, task, item, key, writes, observations = await prepared(tmp_path, sole=True)
        call = hub.call
        async def one(tool, args):
            ok, result = await call(tool, args)
            if tool == "ghost_read" and isinstance(result, dict) and "search" in result.get("url", ""):
                return True, {"url": result["url"], "content": f"[0] link: Ana ({LINK})"}
            return ok, result
        hub.call = one
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}})
        assert "1 items verified" in out and "0 new items executed" in out, out
        assert not writes and observations
    asyncio.run(main())


@pytest.mark.parametrize("key", ["verified_seed", "seed", "skip", "skip_links", "retained"])
def test_model_cannot_supply_retained_item(tmp_path, key):
    async def main():
        hub, browser, task, item, receipt, writes, observations = await prepared(tmp_path)
        out = await hub.test_automation(task, {"automation": item, key: LINK})
        assert "internal runtime evidence" in out and not writes and not observations
    asyncio.run(main())


@pytest.mark.parametrize("constant_identity", [False, True])
def test_real_prefix_executor_pins_observers_and_loop_keeps_normal_write_gates(tmp_path, constant_identity):
    async def main():
        hub, browser, task, original = runtime(tmp_path)
        item = {**original, "steps": [*original["steps"], {"do": "click", "text": "Send"}]}
        task.request += " " + DEST
        approvals, rows, writes = [], [], []
        async def approve(task, question, choice=None):
            approvals.append(question)
            return True
        hub.wait_for_approval = approve
        identity = LINK if constant_identity else "{{link}}"
        config = {"step": 4, "destination_url": DEST, "operation_identity": identity,
                  "expected_fields": {"payload": "fixed"}, "selectors": SPEC}
        call = hub.call
        async def records(tool, args):
            if tool == "ghost_records":
                browser.calls.append((tool, copy.deepcopy(args)))
                return True, {"destination_url": DEST, "complete": True, "records": copy.deepcopy(rows)}
            if tool == "ghost_click" and args.get("choice") == 3:
                receipt = list(task.build_journal.state["effects"].values())[-1]
                assert receipt["status"] == "uncertain"
                assert receipt["recovery_observer"]["selectors"] == SPEC
                link = browser.tabs[args["tab_id"]]
                writes.append(link)
                rows.append({"id": str(len(rows) + 1), "identity": LINK if constant_identity else link,
                             "fields": {"payload": "fixed"}})
            return await call(tool, args)
        hub.call = records
        for n in range(1, len(item["steps"]) + 1):
            args = {"automation": {**item, "steps": item["steps"][:n]}, "mode": "live",
                    "inputs": {"template": "unused"}}
            if n == 4:
                args["recovery"] = config
            out = await hub.test_automation(task, args)
            assert "Test passed" in out, out
        assert writes == [LINK] and task.build_continuation
        approvals.clear()
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}, "recovery": config})
        if constant_identity:
            assert "Loop test failed" in out and "baseline held" in out, out
            assert writes == [LINK] and len(rows) == 1
        else:
            assert "3 items verified" in out, out
            assert len(rows) == 3 and len(set(writes)) == 3
            assert len(approvals) == 3  # one live test and two ordinary per-write approvals
            effects = task.build_journal.state["effects"]
            assert len(effects) == 3
            assert all(receipt.get("recovery_contract") and receipt.get("recovery_observer")
                       and receipt["status"] == "dispatch_returned" for receipt in effects.values())
        before = list(writes)
        again = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                                "inputs": {"template": "unused"}, "recovery": config})
        assert "Not tested" in again and writes == before
    asyncio.run(main())


@pytest.mark.parametrize("other_link", [LINK, "https://www.linkedin.com/in/bo-chen"])
def test_every_prior_receipt_must_be_pinned_and_for_same_item(tmp_path, other_link):
    async def main():
        hub, browser, task, item, key, writes, observations = await prepared(tmp_path)
        receipt = copy.deepcopy(task.build_journal.state["effects"][key])
        receipt["link"] = other_link
        receipt.pop("recovery_contract")
        task.build_journal.state["effects"]["other-intent"] = receipt
        out = await hub.test_automation(task, {"automation": item, "scope": "loop", "mode": "live",
                                              "inputs": {"template": "unused"}})
        assert "held" in out and not writes and not observations
    asyncio.run(main())
