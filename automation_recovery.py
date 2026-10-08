"""Deterministic destination reconciliation, without browser or ledger mutation.

Callers MUST obtain observations from a trusted runtime adapter. This module
validates their structure and compares their contents; it cannot authenticate a
Python dictionary or prove that an adapter covered the entire destination.
Successful confirmation never permits retry, advances a prefix, or restores a
browser. Contracts must be frozen and journaled before consequential dispatch.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlsplit


class RecoveryHeld(ValueError):
    """Evidence is insufficient or inconsistent; leave the intent held."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identifier(value, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RecoveryHeld(f"{label} must be a nonempty, unambiguous string")
    return value


def _destination(value):
    _identifier(value, "destination URL")
    try:
        parsed = urlsplit(value)
        # Accessing port also validates malformed numeric or out-of-range ports.
        parsed.port
    except ValueError as exc:
        raise RecoveryHeld("destination URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RecoveryHeld("destination must be an HTTP(S) URL without credentials")
    return value


def _fields(value):
    if not isinstance(value, dict) or not value:
        raise RecoveryHeld("field payload must be a nonempty mapping")
    for name, text in value.items():
        _identifier(name, "field name")
        if not isinstance(text, str):
            raise RecoveryHeld("field values must be strings; implicit coercion is forbidden")
    return dict(value)


def _observation(value, destination):
    if not isinstance(value, dict) or set(value) != {"destination_url", "complete", "records"}:
        raise RecoveryHeld("observation must contain only destination_url, complete, and records")
    if _destination(value["destination_url"]) != destination:
        raise RecoveryHeld("observation destination differs from the frozen contract")
    if value["complete"] is not True:
        raise RecoveryHeld("complete destination coverage is required")
    if not isinstance(value["records"], list):
        raise RecoveryHeld("records must be a list")
    records, seen = [], set()
    for row in value["records"]:
        if not isinstance(row, dict) or set(row) != {"id", "identity", "fields"}:
            raise RecoveryHeld("each record requires exactly id, identity, and fields")
        record_id = _identifier(row["id"], "record ID")
        if record_id in seen:
            raise RecoveryHeld("destination record IDs must be unique")
        seen.add(record_id)
        records.append({"id": record_id, "identity": _identifier(row["identity"], "operation identity"),
                        "fields": _fields(row["fields"])})
    return {"destination_url": destination, "complete": True, "records": records}


@dataclass(frozen=True)
class RecoveryContract:
    """Immutable canonical contents; use freeze_contract or from_dict."""

    _canonical: str

    def to_dict(self):
        return json.loads(self._canonical)

    @property
    def fingerprint(self):
        return hashlib.sha256(self._canonical.encode()).hexdigest()

    @classmethod
    def from_dict(cls, value):
        keys = {"version", "destination_url", "operation_identity", "expected_fields", "baseline", "baseline_source"}
        if not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int or value["version"] != 1:
            raise RecoveryHeld("invalid frozen recovery contract")
        return freeze_contract(value["destination_url"], value["operation_identity"], value["expected_fields"],
                               value["baseline"], runtime_source=value["baseline_source"])


def freeze_contract(destination_url, operation_identity, expected_fields, baseline, *, runtime_source):
    """Validate an actual complete zero-match baseline BEFORE dispatch."""
    destination = _destination(destination_url)
    identity = _identifier(operation_identity, "operation identity")
    expected = _fields(expected_fields)
    before = _observation(baseline, destination)
    if any(row["identity"] == identity for row in before["records"]):
        raise RecoveryHeld("operation identity already exists in the pre-dispatch baseline")
    return RecoveryContract(_json({"version": 1, "destination_url": destination,
        "operation_identity": identity, "expected_fields": expected, "baseline": before,
        "baseline_source": _identifier(runtime_source, "runtime source")}))


@dataclass(frozen=True)
class ConfirmationEvidence:
    """Effect observation only; no replay permission or continuation state."""

    _canonical: str

    def to_dict(self):
        return json.loads(self._canonical)


def reconcile(contract, observation, *, runtime_source):
    """Read-only deterministic comparison of trusted-runtime destination data."""
    if not isinstance(contract, RecoveryContract):
        raise RecoveryHeld("a frozen RecoveryContract is required")
    # Revalidate even if a caller bypassed the factory by constructing the class.
    try:
        serialized = contract.to_dict()
    except (TypeError, ValueError) as exc:
        raise RecoveryHeld("invalid frozen recovery contract") from exc
    contract = RecoveryContract.from_dict(serialized)
    frozen = contract.to_dict()
    after = _observation(observation, frozen["destination_url"])
    matches = [row for row in after["records"] if row["identity"] == frozen["operation_identity"]]
    if len(matches) != 1:
        raise RecoveryHeld(f"expected exactly one destination match; observed {len(matches)}")
    record = matches[0]
    if record["fields"] != frozen["expected_fields"]:
        raise RecoveryHeld("destination payload differs from the frozen submitted payload")
    if record["id"] in {row["id"] for row in frozen["baseline"]["records"]}:
        raise RecoveryHeld("matching record ID existed before dispatch")
    return ConfirmationEvidence(_json({"kind": "destination_effect_confirmation", "version": 1,
        "contract_fingerprint": contract.fingerprint, "record_id": record["id"],
        "destination_url": frozen["destination_url"], "operation_identity": frozen["operation_identity"],
        "expected_fields": frozen["expected_fields"], "observation": after,
        "runtime_source": _identifier(runtime_source, "runtime source"),
        "observed_at": datetime.now(timezone.utc).isoformat()}))
