import copy
from dataclasses import FrozenInstanceError

import pytest

from automation_recovery import RecoveryContract, RecoveryHeld, freeze_contract, reconcile


URL = "http://localhost:9408/records"
PAYLOAD = {"name": "Avery QA", "operation": "operation-123"}


def row(record_id="receipt-1", identity="operation-123", fields=None):
    return {"id": record_id, "identity": identity, "fields": dict(PAYLOAD if fields is None else fields)}


def observation(records=None, **changes):
    return {"destination_url": URL, "complete": True, "records": [] if records is None else records, **changes}


def contract(baseline=None):
    return freeze_contract(URL, "operation-123", PAYLOAD, observation() if baseline is None else baseline,
                           runtime_source="destination runtime adapter")


def test_journal_pins_contract_before_dispatch_and_rejects_replacement(tmp_path):
    from automation_build import BuildJournal
    journal = BuildJournal(root=tmp_path)
    first = {"name": "QA", "steps": [{"do": "open", "url": URL}]}
    journal.propose(first)
    journal.record_test(1)
    step = {"do": "click", "text": "Submit"}
    journal.propose({**first, "steps": [*first["steps"], step]})
    frozen = contract()
    key = journal.begin_effect({}, "", 2, step, recovery_contract=frozen)
    restored = BuildJournal.load(journal.build_id, root=tmp_path)
    assert restored.recovery_contract(key).fingerprint == frozen.fingerprint
    assert restored.has_uncertain_effects()
    restored.state["effects"][key]["recovery_contract"]["expected_fields"]["name"] = "Different write"
    with pytest.raises(RecoveryHeld, match="fingerprint"):
        restored.recovery_contract(key)


def test_legacy_intent_without_pre_dispatch_witness_cannot_be_reconciled(tmp_path):
    from automation_build import BuildJournal
    journal = BuildJournal(root=tmp_path)
    first = {"name": "QA", "steps": [{"do": "open", "url": URL}]}
    journal.propose(first)
    journal.record_test(1)
    step = {"do": "click", "text": "Submit"}
    journal.propose({**first, "steps": [*first["steps"], step]})
    key = journal.begin_effect({}, "", 2, step)
    with pytest.raises(RecoveryHeld):
        journal.recovery_contract(key)


def test_runtime_witness_is_consumed_for_exactly_one_dispatch(tmp_path):
    from tests.test_build_runtime import runtime
    hub, _, task = runtime(tmp_path)
    first = {"name": "Daily report", "steps": [{"do": "open", "url": URL}]}
    journal = task.build_journal
    journal.propose(first)
    journal.record_test(1)
    step = {"do": "click", "text": "Submit"}
    second = {**first, "steps": [*first["steps"], step]}
    journal.propose(second)
    task.build_execution, task.build_step_number = {}, 2
    task.build_recovery_contract = {"contract": contract(), "inputs": {}, "values": {},
                                    "step": 2, "prefix": second["steps"]}
    key = hub.begin_build_effect(task, step, {})
    assert task.build_recovery_contract is None
    assert journal.recovery_contract(key).fingerprint == contract().fingerprint
    journal.effect_returned(key)
    journal.record_test(2)
    journal.propose({**second, "steps": [*second["steps"], step]})
    task.build_step_number = 3
    next_key = hub.begin_build_effect(task, step, {})
    assert "recovery_contract" not in journal.state["effects"][next_key]


@pytest.mark.parametrize("changed", ["inputs", "values", "step", "prefix"])
def test_pending_witness_cannot_survive_changed_execution(changed, tmp_path):
    from tests.test_build_runtime import runtime
    hub, _, task = runtime(tmp_path)
    first = {"name": "Daily report", "steps": [{"do": "open", "url": URL}]}
    journal = task.build_journal
    journal.propose(first)
    journal.record_test(1)
    step = {"do": "click", "text": "Submit"}
    second = {**first, "steps": [*first["steps"], step]}
    journal.propose(second)
    task.build_execution, task.build_step_number = {}, 2
    witness = {"contract": contract(), "inputs": {}, "values": {}, "step": 2, "prefix": second["steps"]}
    witness[changed] = {"different": "sample"} if changed in {"inputs", "values"} else 99 if changed == "step" else []
    task.build_recovery_contract = witness
    with pytest.raises(RuntimeError, match="execution sample or prefix"):
        hub.begin_build_effect(task, step, {})
    assert not journal.state["effects"]
    assert task.build_recovery_contract is None


def pinned_observer_journal(tmp_path):
    from automation_build import BuildJournal
    journal = BuildJournal(root=tmp_path)
    first = {"name": "QA", "steps": [{"do": "open", "url": URL}]}
    journal.propose(first)
    journal.record_test(1)
    step = {"do": "click", "text": "Submit"}
    journal.propose({**first, "steps": [*first["steps"], step]})
    reader = {"destination_url": URL, "selectors": {"collection": "#records", "row": ".record",
              "id": ".receipt", "identity": ".identity", "total_count": "#total",
              "fields": {"name": ".name", "operation": ".identity"}}}
    key = journal.begin_effect({}, "", 2, step, recovery_contract=contract(), recovery_observer=reader)
    return journal, key, reader


def test_observer_is_pinned_and_readback_is_a_copy(tmp_path):
    journal, key, reader = pinned_observer_journal(tmp_path)
    loaded = journal.recovery_observer(key)
    loaded["selectors"]["id"] = ".changed"
    assert journal.recovery_observer(key) == reader
    journal.state["effects"][key]["recovery_observer"] = loaded
    with pytest.raises(RecoveryHeld, match="observer differs"):
        journal.recovery_observer(key)


def test_destination_evidence_is_durable_but_never_grants_retry_or_prefix_progress(tmp_path):
    from automation_build import BuildJournal
    journal, key, _ = pinned_observer_journal(tmp_path)
    evidence = journal.record_recovery_evidence(key, observation([row()]), "runtime DOM adapter")
    assert evidence["record_id"] == "receipt-1"
    restored = BuildJournal.load(journal.build_id, root=tmp_path)
    assert restored.state["effects"][key]["confirmation"] == evidence
    assert restored.state["effects"][key]["status"] == "uncertain"
    assert restored.has_uncertain_effects()
    assert restored.state["validated_prefix"] == 1
    with pytest.raises(ValueError, match="reconciliation"):
        restored.propose(restored.state["candidate"])


def test_failed_observation_cannot_be_saved_as_confirmation(tmp_path):
    journal, key, _ = pinned_observer_journal(tmp_path)
    with pytest.raises(RecoveryHeld):
        journal.record_recovery_evidence(key, observation([], complete=False), "runtime DOM adapter")
    assert "confirmation" not in journal.state["effects"][key]


def confirm(value, frozen=None):
    return reconcile(contract() if frozen is None else frozen, value, runtime_source="destination runtime adapter")


def test_confirm_new_exact_record_without_mutating_input_or_granting_retry():
    frozen = contract(observation([row("old", "other-operation")]))
    actual = observation([row("old", "other-operation"), row()])
    original = copy.deepcopy(actual)
    evidence = confirm(actual, frozen).to_dict()
    assert evidence["kind"] == "destination_effect_confirmation"
    assert evidence["record_id"] == "receipt-1"
    assert evidence["contract_fingerprint"] == frozen.fingerprint
    assert evidence["observation"] == actual == original
    assert evidence["runtime_source"] == "destination runtime adapter"
    assert evidence["observed_at"]
    assert not ({"retry", "passed", "validated_prefix", "continuation", "confirmed"} & evidence.keys())


def test_frozen_contract_survives_serialization_and_external_mutation():
    baseline = observation([row("old", "other-operation")])
    payload = dict(PAYLOAD)
    frozen = freeze_contract(URL, "operation-123", payload, baseline, runtime_source="adapter")
    digest = frozen.fingerprint
    baseline["records"].clear()
    payload["name"] = "Changed"
    exported = frozen.to_dict()
    restored = RecoveryContract.from_dict(exported)
    assert restored.fingerprint == digest
    exported["expected_fields"]["name"] = "Changed again"
    assert frozen.to_dict()["expected_fields"] == PAYLOAD
    assert len(frozen.to_dict()["baseline"]["records"]) == 1
    with pytest.raises(FrozenInstanceError):
        frozen._canonical = "{}"


def test_confirmation_evidence_export_does_not_mutate_stored_evidence():
    evidence = confirm(observation([row()]))
    exported = evidence.to_dict()
    exported["observation"]["records"].clear()
    assert len(evidence.to_dict()["observation"]["records"]) == 1


@pytest.mark.parametrize("records", [[], [row(), row("receipt-2")]])
def test_zero_and_duplicate_identity_matches_hold(records):
    with pytest.raises(RecoveryHeld, match="exactly one"):
        confirm(observation(records))


@pytest.mark.parametrize("fields", [{"name": "Wrong", "operation": "operation-123"},
                                  {"name": "Avery QA"}, {**PAYLOAD, "extra": "unexpected"}])
def test_payload_must_match_all_fields_exactly(fields):
    with pytest.raises(RecoveryHeld, match="payload differs"):
        confirm(observation([row(fields=fields)]))


def test_baseline_existing_identity_cannot_freeze_even_with_different_payload():
    with pytest.raises(RecoveryHeld, match="already exists"):
        contract(observation([row(fields={"name": "Different"})]))


def test_preexisting_record_id_cannot_be_repurposed_as_confirmation():
    frozen = contract(observation([row("receipt-1", "old-operation")]))
    with pytest.raises(RecoveryHeld, match="existed before"):
        confirm(observation([row()]), frozen)


@pytest.mark.parametrize("complete", [False, None, 1, "true"])
def test_incomplete_coverage_never_freezes_or_confirms(complete):
    value = observation([row()], complete=complete)
    with pytest.raises(RecoveryHeld, match="coverage"):
        confirm(value)
    with pytest.raises(RecoveryHeld, match="coverage"):
        contract(observation(complete=complete))


@pytest.mark.parametrize("destination", ["http://localhost:9408/records?other=1", "http://other/records",
                                       "https://localhost:9408/records"])
def test_destination_must_match_exactly(destination):
    with pytest.raises(RecoveryHeld, match="destination differs"):
        confirm(observation([row()], destination_url=destination))


@pytest.mark.parametrize("record_id", ["", " ", "receipt-1 ", "receipt\n1", 123, None])
def test_malformed_record_ids_hold(record_id):
    with pytest.raises(RecoveryHeld, match="record ID"):
        confirm(observation([row(record_id)]))


def test_duplicate_ids_hold_even_for_unrelated_records():
    with pytest.raises(RecoveryHeld, match="IDs must be unique"):
        confirm(observation([row(), row("old", "other"), row("old", "another")]))


@pytest.mark.parametrize("changes", [{"confirmed": True}, {"passed": True}, {"truncated": True}])
def test_self_attestation_and_extra_coverage_flags_are_not_evidence(changes):
    with pytest.raises(RecoveryHeld, match="only destination_url"):
        confirm(observation([row()], **changes))


@pytest.mark.parametrize("bad", [None, {}, [], {"name": 1}])
def test_payload_types_are_strict(bad):
    with pytest.raises(RecoveryHeld):
        freeze_contract(URL, "operation-123", bad, observation(), runtime_source="adapter")


@pytest.mark.parametrize("bad", ["", "javascript:alert(1)", "file:///records", "https://user:secret@example.com/records",
                               "http://[malformed/records", "http://localhost:99999/records"])
def test_invalid_destination_contracts_hold(bad):
    with pytest.raises(RecoveryHeld):
        freeze_contract(bad, "operation-123", PAYLOAD, observation(destination_url=bad), runtime_source="adapter")


def test_changed_serialized_contract_is_not_the_original_frozen_identity():
    frozen = contract()
    changed = frozen.to_dict()
    changed["expected_fields"]["name"] = "different"
    other = RecoveryContract.from_dict(changed)
    assert other.fingerprint != frozen.fingerprint
    with pytest.raises(RecoveryHeld, match="payload differs"):
        confirm(observation([row()]), other)


@pytest.mark.parametrize("serialized", ['{}', 'not JSON', 'null'])
def test_forged_factory_bypass_is_revalidated(serialized):
    with pytest.raises(RecoveryHeld, match="invalid frozen"):
        confirm(observation([row()]), RecoveryContract(serialized))


def test_runtime_provenance_is_required_but_is_not_an_authentication_claim():
    with pytest.raises(RecoveryHeld, match="runtime source"):
        reconcile(contract(), observation([row()]), runtime_source="")
    with pytest.raises(RecoveryHeld, match="runtime source"):
        freeze_contract(URL, "operation-123", PAYLOAD, observation(), runtime_source="")
