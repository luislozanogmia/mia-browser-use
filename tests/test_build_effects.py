"""Consequential build tests preserve intent and never silently replay a send."""
import asyncio

import pytest

from automation_build import BuildJournal
from tests.test_build_runtime import runtime


def script(*body):
    return {"name": "Daily report", "schedule": {"kind": "manual"},
            "steps": [{"do": "open", "url": "https://reports.example/"}, *body]}


async def approved(*args):
    return True


async def send_prefix(hub, task):
    hub.wait_for_approval = approved
    for item in [script(), script({"do": "copy", "css": "#total", "as": "total"}),
                 script({"do": "copy", "css": "#total", "as": "total"},
                        {"do": "click", "text": "Send report"})]:
        assert "Test passed" in await hub.test_automation(task, {"automation": item, "mode": "live"})
    return item


def sends(browser):
    return [args for tool, args in browser.calls if tool == "ghost_click" and args.get("choice") == 3]


def test_intent_survives_crash_reload_invalidation_and_changed_definition(tmp_path):
    journal = BuildJournal(root=tmp_path)
    item = script({"do": "click", "text": "Send report"})
    journal.propose(script())
    journal.record_test(1)
    journal.propose(item)
    key = journal.begin_effect({"name": "Ada"}, "https://item/", 2, item["steps"][1])
    restored = BuildJournal.load(journal.build_id, root=tmp_path)
    assert restored.state["effects"][key]["status"] == "uncertain"
    restored.invalidate_from(2, "repair target after a lost response")
    with pytest.raises(ValueError, match="reconciliation"):
        restored.begin_effect({"name": "Ada"}, "https://item/", 2, {"do": "key", "key": "Enter"})
    assert len(restored.state["effects"]) == 1
    assert "uncertain" in restored.progress_path.read_text()
    restored.effect_returned(key)
    assert BuildJournal.load(journal.build_id, root=tmp_path).state["effects"][key]["status"] == "dispatch_returned"


def test_successful_send_continues_only_suffix_and_keeps_copied_values(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = await send_prefix(hub, task)
        assert len(sends(browser)) == 1
        browser.calls.clear()
        extended = {**item, "steps": [*item["steps"], {"do": "type", "text": "Search reports", "value": "{{total}}"}]}
        out = await hub.test_automation(task, {"automation": extended, "mode": "live"})
        assert "Test passed" in out and "not replayed" in out
        assert not sends(browser)
        assert not any(tool in {"ghost_navigate", "ghost_tab_open"} for tool, _ in browser.calls)
        assert next(args["value"] for tool, args in browser.calls if tool == "ghost_fill") == "42 open items"
        assert len(task.build_journal.state["effects"]) == 1
    asyncio.run(main())


def test_pre_dispatch_snapshot_survives_restart_without_authorizing_replay(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = await send_prefix(hub, task)
        restored = BuildJournal.load(task.build_journal.build_id, root=task.build_journal.root)
        receipt = next(iter(restored.state["effects"].values()))
        snapshot = receipt["execution"]
        assert snapshot["prefix"] == item["steps"]
        assert snapshot["values"]["total"] == "42 open items"
        assert snapshot["inputs"] == {}
        assert "" not in snapshot["values"]
        task.build_journal.state["candidate"]["steps"][0]["url"] = "https://changed.example/"
        assert snapshot["prefix"][0]["url"] == item["steps"][0]["url"]
        task.build_journal = restored
        task.build_continuation = None
        browser.calls.clear()
        out = await hub.test_automation(task, {"automation": item, "mode": "live"})
        assert "Hold for destination reconciliation" in out
        assert not any(tool in {"ghost_navigate", "ghost_click", "ghost_tab_open"} for tool, _ in browser.calls)
    asyncio.run(main())


@pytest.mark.parametrize("disrupt", ["restart", "invalidation", "tab", "page", "manual"])
def test_disrupted_continuation_holds_before_replay(tmp_path, disrupt):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = await send_prefix(hub, task)
        if disrupt == "restart":
            task.build_journal = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "builds")
            task.build_continuation = None
        elif disrupt == "invalidation":
            task.build_journal.invalidate_from(3, "repair send")
            item["steps"][2] = {"do": "key", "key": "Enter"}
        elif disrupt == "tab":
            task.tab_id += 1
        elif disrupt == "page":
            task.build_continuation["page"] = "changed"
        else:
            await hub.act(task, {"tool": "ghost_scroll", "args": {"direction": "down"}}, {"ghost_scroll"})
        browser.calls.clear()
        out = await hub.test_automation(task, {"automation": item, "mode": "rehearsal"})
        assert "Hold for destination reconciliation" in out
        assert not any(tool in {"ghost_navigate", "ghost_tab_open", "ghost_click", "ghost_key"} for tool, _ in browser.calls)
    asyncio.run(main())


def test_lost_acknowledgement_is_not_retried_even_when_error_says_target_gone(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        hub.wait_for_approval = approved
        first = script()
        assert "Test passed" in await hub.test_automation(task, {"automation": first, "mode": "live"})
        original = hub.call
        attempts = []
        async def lost(tool, args):
            if tool == "ghost_click":
                attempts.append(args)
                reloaded = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "builds")
                assert next(iter(reloaded.state["effects"].values()))["status"] == "uncertain"
                return False, "is gone; response lost after send"
            return await original(tool, args)
        hub.call = lost
        item = script({"do": "click", "text": "Send report"})
        assert "Test failed" in await hub.test_automation(task, {"automation": item, "mode": "live"})
        assert len(attempts) == 1
        receipt = next(iter(task.build_journal.state["effects"].values()))
        assert receipt["status"] == "uncertain"
        task.build_continuation = None
        assert "hold for destination reconciliation" in await hub.test_automation(task, {"automation": item, "mode": "live"})
        assert len(attempts) == 1
    asyncio.run(main())


def test_readonly_search_has_no_effect_ledger(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        hub.wait_for_approval = approved
        for item in [script(), script({"do": "key", "text": "Search reports", "key": "Enter"})]:
            assert "Test passed" in await hub.test_automation(task, {"automation": item})
        assert task.build_journal.state["effects"] == {}
    asyncio.run(main())


def test_append_intent_is_durable_before_mutation_and_suffix_does_not_append_again(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        hub.wait_for_approval = approved
        original = hub.call
        writes = []
        async def sheet(tool, args):
            if tool == "ghost_sheet_append":
                persisted = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "builds")
                assert next(iter(persisted.state["effects"].values()))["status"] == "uncertain"
                writes.append(args)
                return True, {"row": 7}
            return await original(tool, args)
        hub.call = sheet
        append = {"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test/edit",
                  "tab": "Sheet1", "row": ["QA sample"]}
        first = script()
        assert "Test passed" in await hub.test_automation(task, {"automation": first, "mode": "live"})
        item = script(append)
        assert "Test passed" in await hub.test_automation(task, {"automation": item, "mode": "live"})
        extended = script(append, {"do": "copy", "css": "#total", "as": "receipt"})
        assert "Test passed" in await hub.test_automation(task, {"automation": extended, "mode": "live"})
        assert len(writes) == 1
        assert task.build_journal.state["cases"][-1]["copied"]["receipt"] == "42 open items"
        task.build_continuation = None
        assert "Hold for destination reconciliation" in await hub.test_automation(task, {"automation": extended})
        assert len(writes) == 1
    asyncio.run(main())


def test_uncertain_effect_blocks_new_sample_renamed_contract_and_save(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = {**script(), "inputs": [{"name": "person"}]}
        task.build_journal.propose(item)
        task.build_journal.record_test(1)
        send = {**item, "steps": [*item["steps"], {"do": "click", "text": "Send report"}]}
        task.build_journal.propose(send)
        task.build_journal.begin_effect({"person": "Ada"}, "", 2, send["steps"][1])
        task.build_journal.invalidate_from(1, "try a new sample or name")
        before = list(browser.calls)
        for candidate, values in [(send, {"person": "Grace"}),
                                  ({**item, "inputs": [{"name": "renamed"}]}, {"renamed": "Ada"})]:
            out = await hub.test_automation(task, {"automation": candidate, "inputs": values, "mode": "live"})
            assert "hold for destination reconciliation" in out
            assert browser.calls == before
        task.tested = hub.build_key(send)
        assert "hold for destination reconciliation" in hub.check_build(task, send)
        assert task.draft is None
        assert task.build_journal.state["failed_step"] is None
        assert len(task.build_journal.state["effects"]) == 1
    asyncio.run(main())


def test_unused_input_change_cannot_replay_returned_dispatch(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        hub.wait_for_approval = approved
        inputs = [{"name": "unused"}]
        first = {**script(), "inputs": inputs}
        await hub.test_automation(task, {"automation": first, "inputs": {"unused": "a"}, "mode": "live"})
        item = {**script({"do": "click", "text": "Send report"}), "inputs": inputs}
        assert "Test passed" in await hub.test_automation(task, {"automation": item, "inputs": {"unused": "a"}, "mode": "live"})
        assert len(sends(browser)) == 1
        assert next(iter(task.build_journal.state["effects"].values()))["dependencies"] == []
        assert "Hold for destination reconciliation" in await hub.test_automation(task, {"automation": item, "inputs": {"unused": "b"}, "mode": "live"})
        assert len(sends(browser)) == 1
        task.build_journal.invalidate_from(1, "rename unused input")
        renamed = {**first, "inputs": [{"name": "other"}]}
        assert "input contract cannot change" in await hub.test_automation(task, {"automation": renamed, "inputs": {"other": "a"}})
    asyncio.run(main())


def test_transitive_template_inputs_participate_in_dispatch_identity(tmp_path):
    journal = BuildJournal(root=tmp_path)
    item = {**script({"do": "type", "text": "Search reports", "value": "{{template}}"}),
            "inputs": [{"name": "template"}, {"name": "person"}, {"name": "unused"}]}
    journal.propose({**item, "steps": item["steps"][:1]})
    journal.record_test(1)
    journal.propose(item)
    sample = {"template": "Hi {{person}}", "person": "Ada", "unused": "a"}
    key = journal.begin_effect(sample, "", 2, item["steps"][1])
    journal.effect_returned(key)
    assert journal.state["effects"][key]["dependencies"] == ["person", "template"]
    assert journal.effects_for_sample({**sample, "unused": "b"})
    assert not journal.effects_for_sample({**sample, "person": "Grace"})


def test_prefix_end_readback_syncs_manual_discovery_url(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        original = hub.call
        async def destination(tool, args):
            ok, result = await original(tool, args)
            if tool == "ghost_read":
                result = {**result, "url": "https://reports.example/receipt/123"}
            return ok, result
        hub.call = destination
        assert "Test passed" in await hub.test_automation(task, {"automation": script()})
        assert task.current_url == "https://reports.example/receipt/123"
        task.control_approved, task.control_url = True, "https://reports.example/old-page"
        await hub.record_build_case(task, task.build_journal.state["candidate"], {}, {}, True, True, 1, [])
        assert task.current_url == "https://reports.example/receipt/123"
        assert not task.control_approved and hub.args(task, {})["expected_url"] == ""
    asyncio.run(main())


def test_legacy_receipts_without_dependencies_hold_new_samples(tmp_path):
    journal = BuildJournal(root=tmp_path)
    journal.state["effects"] = {"legacy": {"sample": journal.sample_identity({"unused": "a"}),
                                            "status": "dispatch_returned", "step": 2, "link": ""}}
    journal._save()
    restored = BuildJournal.load(journal.build_id, root=tmp_path)
    assert restored.effects_for_sample({"unused": "b"})


def test_copied_value_override_does_not_make_unused_initial_input_a_new_dispatch(tmp_path):
    journal = BuildJournal(root=tmp_path)
    item = {**script({"do": "copy", "css": "#total", "as": "person"},
                     {"do": "type", "text": "Search reports", "value": "{{person}}"}),
            "inputs": [{"name": "person"}]}
    for count in range(1, 4):
        journal.propose({**item, "steps": item["steps"][:count]})
        journal.record_test(count)
    key = journal.begin_effect({"person": "unused Ada"}, "", 3, item["steps"][2])
    journal.effect_returned(key)
    assert journal.state["effects"][key]["dependencies"] == []
    assert journal.effects_for_sample({"person": "unused Grace"})


@pytest.mark.parametrize("full_cases", [False, True])
def test_large_effect_context_is_bounded_with_all_uncertain_intents_retained(tmp_path, full_cases):
    import json
    journal = BuildJournal(root=tmp_path)
    journal.state["effects"] = {f"receipt-{n}": {"status": "dispatch_returned", "step": 3,
                                                "sample": str(n), "link": "https://example/" + "x" * 4000,
                                                "definition": {"do": "click", "text": "Submit"}}
                                for n in range(2000)}
    journal.state["effects"]["unknown-response"] = {"status": "uncertain", "step": 4,
                                                      "sample": "pending", "link": "https://receipt/"}
    journal._save()
    before = journal.path.read_bytes()
    context = journal.context(full_cases=full_cases)
    assert len(context["effects"]) == 9
    assert "unknown-response" in context["effects"]
    assert list(context["effects"]) == [*(f"receipt-{n}" for n in range(4)),
                                       *(f"receipt-{n}" for n in range(1996, 2000)), "unknown-response"]
    summary = context["effect_context"]
    assert summary["receipt_count"] == 2001
    assert summary["status_counts"] == {"dispatch_returned": 2000, "uncertain": 1}
    assert summary["omitted_returned_count"] == 1992 and summary["retained_uncertain_count"] == 1
    assert not summary["complete"] and summary["state_path"] == str(journal.path)
    assert summary["omissions"]["effects.receipt-0.link"]["omitted_characters"] > 0
    assert len(json.dumps(context)) < 18000
    assert journal.path.read_bytes() == before
    loaded = BuildJournal.load(journal.build_id, root=tmp_path)
    assert len(loaded.state["effects"]) == 2001
    assert len(loaded.state["effects"]["receipt-999"]["link"]) > 4000
    assert "receipt-999" in loaded.progress_path.read_text()
    context["effects"]["unknown-response"]["status"] = "dispatch_returned"
    assert loaded.has_uncertain_effects()


def test_description_review_fix_retains_live_proof_without_replaying_effect(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = await send_prefix(hub, task)
        before = task.build_journal.state['cases'][-1]['candidate_fingerprint']
        task.build_continuation = None
        browser.calls.clear()
        updated = {**item, 'about':'Clearer behavior and limitations.'}
        # A later rehearsal on another sample may mark the same final action
        # pending; the completed live case is retained only for its own sample.
        task.build_journal.state['pending_steps'] = [3]
        out = await hub.test_automation(task, {'automation':updated, 'mode':'live'})
        assert 'proof retained' in out and 'no action was replayed' in out
        assert task.tested == hub.build_key(task.build_journal.state['candidate'])
        assert not browser.calls
        assert task.build_journal.state['cases'][-1]['candidate_fingerprint'] == before
        assert len(task.build_journal.state['effects']) == 1
        assert task.build_journal.state['pending_steps'] == []
        assert not hub.check_build(task, updated)
    asyncio.run(main())
