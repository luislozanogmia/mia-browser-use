import copy
import json
import stat
import uuid

import pytest

from automation_build import BuildJournal, MAX_CASES, MAX_EVENTS


STEPS = [
    {"do": "open", "url": "https://example.test/"},
    {"do": "copy", "css": "#total", "as": "total"},
    {"do": "click", "text": "Send report"},
    {"do": "wait", "css": "#sent"},
]


def item(count, **kwargs):
    return {"name": "Report", "steps": copy.deepcopy(STEPS[:count]), **kwargs}


def journal(tmp_path):
    return BuildJournal("Send a report", plan=["Open", "Copy", "Send", "Check"], root=tmp_path / "builds")


@pytest.mark.parametrize("full_cases", [False, True])
def test_large_nested_loop_context_is_sampled_without_changing_durable_proof(tmp_path, full_cases):
    build = journal(tmp_path)
    build.propose(item(1))
    pages = [{"url": f"https://example.test/page/{page}",
              "links": [f"https://example.test/item/{page}/{index}" for index in range(300)]}
             for page in range(40)]
    items = [{"link": f"https://example.test/item/{index}", "passed": index != 125,
              "copied": {f"field_{field}": f"item-{index}-field-{field}:" + "x" * 1000
                         for field in range(24)}, "pending_steps": list(range(1, 61))}
             for index in range(250)]
    build.record_case({}, "", "live", pages[-1]["url"], False, 1, {},
                      outcome={"pages": pages, "items": items},
                      metadata={"scope": "loop", "finished": 249, "item_count": 250,
                                "pages": pages, "skipped": [items[125]["link"]],
                                "error": "One item failed verification"})
    before = build.path.read_bytes()
    context = build.context(full_cases=full_cases)
    case = context["cases"][-1]
    assert len(json.dumps(context)) < 180_000
    assert case["passed"] is False
    assert case["metadata"]["finished"] == 249
    assert case["metadata"]["item_count"] == 250
    assert case["metadata"]["skipped"] == [items[125]["link"]]
    assert case["outcome"]["items"][0]["link"] == items[0]["link"]
    assert case["outcome"]["items"][-1]["link"] == items[-1]["link"]
    assert case["outcome"]["pages"][0]["url"] == pages[0]["url"]
    assert case["outcome"]["pages"][-1]["url"] == pages[-1]["url"]
    assert case["outcome"]["items"][0]["pending_steps"] == list(range(1, 61))
    summary = case["context_evidence"]
    assert summary["complete"] is False and "not shown" in summary["notice"]
    assert summary["omissions"]["outcome.items"]["omitted_entries"] == 242
    assert summary["omissions"]["outcome.pages"]["omitted_entries"] == 32
    assert summary["omissions"]["outcome.pages[0].links"]["omitted_entries"] == 292
    assert summary["omissions"]["outcome.items[0].copied"]["omitted_fields"] == 8
    assert summary["omissions"]["outcome.items[0].copied.field_0"]["omitted_characters"] > 0
    assert build.path.read_bytes() == before
    assert build.state["cases"][-1]["outcome"]["items"] == items
    # Reloaded audit evidence includes even a failure omitted by head/tail sampling.
    reloaded = BuildJournal.load(build.build_id, root=build.root)
    assert reloaded.state["cases"][-1]["outcome"]["items"][125]["passed"] is False
    assert reloaded.state["cases"][-1]["outcome"]["pages"] == pages
    assert "context_evidence" not in reloaded.state["cases"][-1]


def test_requires_single_first_step_then_one_verified_extension(tmp_path):
    build = journal(tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        build.propose(item(2))
    build.propose(item(1))
    with pytest.raises(ValueError, match="validate"):
        build.propose(item(2))
    build.record_test(1, details="Opened and observed report page")
    build.propose(item(2))
    with pytest.raises(ValueError, match="validate"):
        build.propose(item(3))


def test_repair_clears_approval_but_preserves_review_reason_for_recovery(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1)
    fingerprint = build.candidate_fingerprint()
    build.record_review("adversarial", "revise", "Exact-title lookup needs a reusable result target")
    build.invalidate_from(1, "Repair the reviewed target")
    restored = BuildJournal.load(build.build_id, root=build.root)
    assert restored.state["reviews"] == []
    review = next(event for event in restored.state["events"] if event["kind"] == "review")
    assert "reusable result target" in review["details"]
    assert review["candidate"] == fingerprint


def test_failure_holds_until_same_step_repaired_and_passed(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1)
    build.propose(item(2))
    build.record_test(1, failed_step=2, details="Total selector missing")
    with pytest.raises(ValueError, match="validate"):
        build.propose(item(3))
    repair = item(2)
    repair["steps"][1]["css"] = "[data-total]"
    build.propose(repair)
    assert build.context()["failed_step"] == 2
    build.record_test(2, details="Read displayed total")
    assert build.context()["failed_step"] is None
    build.propose({**repair, "steps": repair["steps"] + [STEPS[2]]})


def test_validated_prefix_and_inputs_are_locked(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1, inputs=[{"name": "message", "label": "Message"}]))
    build.record_test(1)
    with pytest.raises(ValueError, match="prefix is locked"):
        build.propose({"name": "Report", "steps": [{"do": "open", "url": "https://other.test/"}],
                       "inputs": [{"name": "message", "label": "Message"}]})
    with pytest.raises(ValueError, match="configuration are locked"):
        build.propose(item(2))


def test_invalidation_requires_reason_and_clears_downstream_evidence(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1, details="First step")
    build.propose(item(2))
    build.record_test(2, details="Second step")
    build.record_review("adversarial", "pass", "Selectors reviewed")
    with pytest.raises(ValueError, match="reason"):
        build.invalidate_from(2, " ")
    build.invalidate_from(2, "Total moved")
    state = build.context()
    assert state["validated_prefix"] == state["rehearsed_prefix"] == 1
    assert state["reviews"] == []
    assert [event["details"] for event in state["events"] if event["kind"] == "test"] == ["First step"]
    repair = item(2)
    repair["steps"][1]["css"] = "#new-total"
    build.propose(repair)
    build.record_test(2)
    build.invalidate_from(1, "Page changed")
    build.propose(item(1, inputs=[{"name": "url"}]))


def test_private_atomic_artifacts_and_restart_context(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1, details="Observed page")
    build.save_checkpoint({"task_id": "task-42", "model": "claude", "status": "building"})
    build.record_review("human", "needs_work", "Use shorter labels")
    loaded = BuildJournal.load(build.build_id, root=build.root)
    state = loaded.context()
    assert uuid.UUID(state["build_id"])
    assert state["checkpoint"]["task_id"] == "task-42"
    assert state["request"] == "Send a report"
    assert state["plan"] == ["Open", "Copy", "Send", "Check"]
    assert state["reviews"][0]["verdict"] == "needs_work"
    assert json.loads(build.path.read_text())["candidate"] == state["candidate"]
    assert "Observed page" in build.progress_path.read_text()
    for path in (build.root, build.directory):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (build.path, build.progress_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(build.directory.glob(".journal-*")) == []
    state["candidate"]["steps"].clear()
    assert len(loaded.context()["candidate"]["steps"]) == 1


def test_rehearsal_can_extend_without_claiming_skipped_effect_verified(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1)
    build.propose(item(2))
    build.record_test(2)
    build.propose(item(3))
    build.record_rehearsal(3, [3], details="Send button exists; send skipped")
    state = build.context()
    assert state["validated_prefix"] == 2
    assert state["rehearsed_prefix"] == 3
    assert state["pending_steps"] == [3]
    assert "[pending side effect]" in build.progress_path.read_text()
    build.propose(item(4))
    build.record_rehearsal(4, [], details="Sent confirmation probe")
    assert build.context()["pending_steps"] == [3]
    assert build.context()["validated_prefix"] == 2
    build.record_test(4, details="Live send and observed confirmation")
    assert build.context()["validated_prefix"] == 4
    assert build.context()["pending_steps"] == []


def test_record_test_rejects_counting_pending_or_noncontiguous_evidence(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    with pytest.raises(ValueError, match="skipped side effects"):
        build.record_test(1, pending_steps=[1])
    with pytest.raises(ValueError, match="first step"):
        build.record_test(0, failed_step=2)
    with pytest.raises(ValueError, match="success_count"):
        build.record_test(True)
    with pytest.raises(ValueError, match="pending"):
        build.record_test(0, pending_steps=[True])


def test_each_can_start_with_only_list_step_and_is_locked(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1, each={"links": "/profile/", "next": "Next"}))
    assert len(build.context()["candidate"]["steps"]) == 1
    build.record_test(1)
    with pytest.raises(ValueError, match="configuration are locked"):
        build.propose(item(2, each={"links": "/other/", "next": "Next"}))
    build.propose(item(2, each={"links": "/profile/", "next": "Next"}))


def test_malformed_steps_are_not_silently_removed(tmp_path):
    build = journal(tmp_path)
    with pytest.raises(ValueError, match="every proposed step"):
        build.propose({"name": "Bad", "steps": [{"do": "eval", "code": "anything"}]})
    assert build.context()["candidate"] is None


def test_regression_requires_explicit_invalidation_before_repair(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1)
    build.propose(item(2))
    build.record_test(0, failed_step=1, details="Page no longer available")
    with pytest.raises(ValueError, match="previously validated step failed"):
        build.propose(item(2))
    build.invalidate_from(1, "Repair changed page")
    build.propose(item(1))


def test_bounded_evidence_and_symlink_rejection(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    for _ in range(MAX_EVENTS + 5):
        build.record_test(1, details="x" * 5000)
    stored = json.loads(build.path.read_text())
    assert len(stored["events"]) == MAX_EVENTS
    assert len(build.context()["events"]) == 8
    assert len(stored["events"][-1]["details"]) == 4000
    build.progress_path.unlink()
    build.progress_path.symlink_to(tmp_path / "unrelated.md")
    with pytest.raises(ValueError, match="symbolic links"):
        BuildJournal.load(build.build_id, root=build.root)


def test_cases_keep_samples_readback_and_live_distinction_through_restart(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1, inputs=[{"name": "sample", "label": "Sample"}]))
    samples = {"sample": "first"}
    outcome = {"content": "Report visible", "url": "https://example.test/result"}
    case = build.record_case(samples, "https://example.test/item/1", "rehearsal",
                             outcome["url"], True, 1, {"total": "42"}, outcome,
                             "Read actual page")
    samples["sample"] = "mutated"
    outcome["content"] = "mutated"
    case["copied"]["total"] = "mutated"
    build.record_case({"sample": "second"}, "https://example.test/item/2", "live",
                      "https://example.test/result", True, 1, {"total": "53"})
    loaded = BuildJournal.load(build.build_id, root=build.root)
    cases = loaded.context()["cases"]
    assert [c["mode"] for c in cases] == ["rehearsal", "live"]
    assert [c["inputs"]["sample"] for c in cases] == ["first", "second"]
    assert cases[0]["copied"] == {"total": "42"}
    assert cases[0]["outcome"]["content"] == "Report visible"
    assert all(c["current_candidate"] for c in cases)
    assert cases[0]["candidate_fingerprint"] == loaded.context()["candidate_fingerprint"]
    assert loaded.state["validated_prefix"] == 0  # a case cannot manufacture step proof
    assert "Read actual page" in loaded.progress_path.read_text()


def test_extended_and_repaired_candidates_cannot_reuse_stale_case_evidence(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_test(1)
    build.record_case({}, "", "live", "https://example.test/", True, 1, {})
    first_fingerprint = build.context()["candidate_fingerprint"]
    build.propose(item(2))
    assert not build.context()["cases"][0]["current_candidate"]
    build.record_case({}, "", "live", "https://example.test/", False, 2, {},
                      details="Total missing")
    build.invalidate_from(2, "Repair total selector")
    assert len(build.state["cases"]) == 1
    repair = item(2)
    repair["steps"][1]["css"] = "#replacement"
    build.propose(repair)
    assert build.context()["candidate_fingerprint"] != first_fingerprint
    assert not build.context()["cases"][0]["current_candidate"]
    build.invalidate_from(1, "Start page changed")
    assert build.context()["cases"] == []


def test_cases_remain_available_after_rolling_events_are_evicted(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    build.record_case({}, "", "live", "https://example.test/", True, 1, {},
                      details="Representative sample readback")
    for _ in range(MAX_EVENTS + 1):
        build.record_test(1)
    assert build.context()["cases"][0]["details"] == "Representative sample readback"
    for n in range(MAX_CASES + 1):
        build.record_case({"sample": n}, "", "rehearsal", "", False, 1, {}, details="x" * 5000)
    assert len(build.context(full_cases=True)["cases"]) == MAX_CASES
    assert len(build.context()["cases"]) == 8
    assert build.context()["case_count"] == MAX_CASES
    assert build.context()["cases"][-1]["inputs"] == {"sample": MAX_CASES}
    assert len(build.context()["cases"][-1]["details"]) == 4000


def test_old_journal_without_cases_migrates_on_load(tmp_path):
    build = journal(tmp_path)
    state = json.loads(build.path.read_text())
    state.pop("cases")
    build.path.write_text(json.dumps(state))
    loaded = BuildJournal.load(build.build_id, root=build.root)
    assert loaded.context()["cases"] == []
    assert json.loads(loaded.path.read_text())["cases"] == []


@pytest.mark.parametrize("plan", [None, "Open", [], [""], [" "], [42], ["x" * 501], ["Step"] * 121])
def test_malformed_plans_cannot_corrupt_durable_journal(tmp_path, plan):
    build = journal(tmp_path)
    before = build.path.read_text()
    with pytest.raises(ValueError, match="plan needs"):
        build.set_plan(plan)
    assert build.path.read_text() == before
    assert BuildJournal.load(build.build_id, root=build.root).state["plan"] == build.state["plan"]


def test_compact_cases_preserve_full_reviewer_evidence_and_durable_readback(tmp_path):
    build = journal(tmp_path)
    build.propose(item(1))
    for n in range(12):
        build.record_case({"sample": n}, "", "live", "https://example.test/", True, 1, {},
                          outcome={"content": "x" * 8000})
    compact = build.context()
    full = build.context(full_cases=True)
    assert [c["inputs"]["sample"] for c in compact["cases"]] == list(range(4, 12))
    assert [c["inputs"]["sample"] for c in full["cases"]] == list(range(12))
    assert compact["case_count"] == full["case_count"] == 12
    assert len(full["cases"][0]["outcome"]["content"]) == 2500
    assert len(json.loads(build.path.read_text())["cases"][0]["outcome"]["content"]) == 8000
    full["cases"][0]["inputs"]["sample"] = "changed"
    assert build.context(full_cases=True)["cases"][0]["inputs"]["sample"] == 0


def test_about_revision_preserves_execution_identity_but_requires_fresh_reviews(tmp_path):
    build = journal(tmp_path)
    original = item(1, about='Original description')
    build.propose(original)
    build.record_test(1)
    case = build.record_case({}, '', 'live', 'https://example.test/', True, 1, {},
                             outcome={'url':'https://example.test/', 'content':'Verified result'})
    build.record_review('human', 'pass')
    fingerprint = case['candidate_fingerprint']
    build.propose({**original, 'about':'Clearer description'})
    assert build.state['reviews'] == []
    assert build.state['cases'][0]['candidate_fingerprint'] == fingerprint
    assert build.candidate_fingerprint() != fingerprint
    assert build.case_matches_execution(build.state['cases'][0])
    assert build.context()['cases'][0]['current_candidate'] is False
    assert build.context()['cases'][0]['current_execution'] is True
    build.invalidate_from(1, 'Changed navigation')
    build.propose(item(1, about='Clearer description', steps=[{'do':'open','url':'https://different.test/'}]))
    assert not build.case_matches_execution(case)


def test_legacy_about_proof_requires_exact_reconstructed_candidate_hash(tmp_path):
    build = journal(tmp_path)
    build.state['checkpoint']['build'] = {'about':'Original description'}
    original = item(1, about='Original description')
    build.propose(original)
    build.record_test(1)
    case = build.record_case({}, '', 'live', 'https://example.test/', True, 1, {})
    case.pop('execution_fingerprint')
    build.state['candidate']['about'] = 'Revised description'
    assert build.case_matches_execution(case)
    build.state['candidate']['schedule'] = {'kind':'daily', 'at':'09:00'}
    assert not build.case_matches_execution(case)
