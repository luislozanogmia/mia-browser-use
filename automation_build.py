"""Private, durable evidence for building a Play Automation one step at a time.

Step numbers in evidence are one-based; ``success_count`` is a count of the
contiguous steps actually executed and verified. A dry-run skipped action must
be listed in ``pending_steps`` and never contributes to that count.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automations import MAX_STEPS, clean, clean_step, describe_step

MAX_EVENTS = 80
MAX_CASES = 80
MAX_DETAILS = 4000
MAX_DISCOVERY = 24
MAX_DISCOVERY_RESULT = 12000
MAX_RECORD_PROBES = 4
LOOP_CONTEXT_ITEMS = 8
LOOP_CONTEXT_FIELDS = 16
LOOP_CONTEXT_TEXT = 500


def _compact_loop_evidence(value: Any, path: str, omissions: dict) -> Any:
    """Sample only transmitted evidence; durable records remain complete.

    Preserve scalar verdicts/counts and small numeric lists (step numbers),
    while sampling head/tail entries from potentially large nested collections.
    Every shortened value has an explicit count at its original JSON path.
    """
    if isinstance(value, str):
        if len(value) > LOOP_CONTEXT_TEXT:
            omissions[path] = {"characters": len(value), "omitted_characters": len(value) - LOOP_CONTEXT_TEXT}
        return value[:LOOP_CONTEXT_TEXT]
    if isinstance(value, list):
        if len(value) <= MAX_STEPS and all(type(entry) is int for entry in value):
            return value[:]
        indices = list(range(len(value)))
        if len(indices) > LOOP_CONTEXT_ITEMS:
            half = LOOP_CONTEXT_ITEMS // 2
            indices = indices[:half] + indices[-half:]
            omissions[path] = {"entries": len(value), "omitted_entries": len(value) - len(indices),
                               "selection": "head and tail", "retained_indices": indices}
        return [_compact_loop_evidence(value[index], f"{path}[{index}]", omissions) for index in indices]
    if isinstance(value, dict):
        keys = list(value)
        if len(keys) > LOOP_CONTEXT_FIELDS:
            half = LOOP_CONTEXT_FIELDS // 2
            keys = keys[:half] + keys[-half:]
            omissions[path] = {"fields": len(value), "omitted_fields": len(value) - len(keys),
                               "selection": "head and tail"}
        return {key: _compact_loop_evidence(value[key], f"{path}.{key}", omissions) for key in keys}
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _discovery_key(entry: dict) -> str:
    args = entry["args"]
    return ("records:" + json.dumps(args["spec"], sort_keys=True)
            if entry["tool"] == "ghost_records" else "read:" + args.get("selector", ""))


def _retain_discovery(recent: list, observation: dict) -> list:
    key = _discovery_key(observation)
    recent = [entry for entry in recent if _discovery_key(entry) != key] + [observation]
    probes = [entry for entry in recent if entry["tool"] == "ghost_records"]
    omitted = {id(entry) for entry in probes[:-MAX_RECORD_PROBES]}
    return [entry for entry in recent if id(entry) not in omitted][-MAX_DISCOVERY:]


def _record_spec(spec) -> dict | None:
    if (not isinstance(spec, dict) or set(spec) != {"collection", "row", "id", "identity", "fields", "total_count"}
            or not isinstance(spec["fields"], dict) or not 1 <= len(spec["fields"]) <= 32):
        return None
    selectors = [spec[k] for k in ("collection", "row", "id", "identity", "total_count")]
    selectors += list(spec["fields"].values())
    if (any(not isinstance(s, str) or not s.strip() or len(s) > 500 for s in selectors)
            or any(not isinstance(k, str) or not k.strip() or len(k) > 100 for k in spec["fields"])):
        return None
    return _clone(spec)


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _atomic(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class BuildJournal:
    """A resumable build. Pass its UUID again to load the authoritative state.

    ``root`` is the builds directory (defaults to ~/.ghost/builds). No browser
    work happens here: the caller supplies actual runtime test evidence.
    """

    def __init__(self, request: str = "", plan: list | None = None,
                 build_id: str | None = None, root: Path | str | None = None,
                 checkpoint: dict | None = None):
        self.build_id = str(uuid.UUID(build_id)) if build_id else str(uuid.uuid4())
        self.root = Path(root) if root is not None else Path.home() / ".ghost" / "builds"
        if self.root.is_symlink():
            raise ValueError("the build root must not be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.directory = self.root / self.build_id
        if self.directory.is_symlink():
            raise ValueError("the build directory must not be a symbolic link")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / "state.json"
        self.progress_path = self.directory / "progress.md"
        self.discovery_path = self.directory / "discovery.jsonl"
        if any(path.is_symlink() for path in (self.path, self.progress_path, self.discovery_path)):
            raise ValueError("build artifacts must not be symbolic links")
        if self.path.exists():
            self.path.chmod(0o600)
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("build_id") != self.build_id or self.state.get("version") != 1:
                raise ValueError("invalid build journal")
            self.state.setdefault("effects", {})
            self.state.setdefault("cases", [])
            self.state.setdefault("discovery", [])
            self.state.setdefault("discovery_count", 0)
            self._restore_discovery()
            self._save()  # regenerate the human-readable checkpoint after interruption
        else:
            self.state = {
                "version": 1, "build_id": self.build_id, "request": str(request),
                "plan": _clone(plan or []), "candidate": None,
                "validated_prefix": 0, "rehearsed_prefix": 0, "failed_step": None, "pending_steps": [],
                "effects": {}, "events": [], "cases": [], "reviews": [], "discovery": [], "discovery_count": 0,
                "checkpoint": _clone(checkpoint or {}),
                "created_at": _now(), "updated_at": _now(),
            }
            self._event("created", details="Build started")
            self._save()

    @classmethod
    def load(cls, build_id: str, root: Path | str | None = None) -> "BuildJournal":
        journal_root = Path(root) if root is not None else Path.home() / ".ghost" / "builds"
        path = journal_root / str(uuid.UUID(build_id)) / "state.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        return cls(build_id=build_id, root=journal_root)

    def _event(self, kind: str, **fields: Any) -> None:
        if "details" in fields:
            fields["details"] = str(fields["details"])[:MAX_DETAILS]
        self.state["events"].append({"kind": kind, "at": _now(), **_clone(fields)})
        self.state["events"] = self.state["events"][-MAX_EVENTS:]

    def _save(self) -> None:
        self.state["updated_at"] = _now()
        _atomic(self.path, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")
        lines = ["# Play Automation build", "", f"Build: {self.build_id}", "",
                 "## Request", "", self.state["request"], "", "## Plan", ""]
        lines += [f"{index}. {step}" for index, step in enumerate(self.state["plan"], 1)]
        lines += ["", "## Progress", "", f"Validated prefix: {self.state['validated_prefix']}",
                  f"Rehearsed prefix: {self.state.get('rehearsed_prefix', 0)}",
                  f"Failed step: {self.state['failed_step'] or 'none'}",
                  f"Pending side effects: {self.state['pending_steps']}", ""]
        candidate = self.state["candidate"]
        if candidate:
            for index, step in enumerate(candidate["steps"], 1):
                status = "verified" if index <= self.state["validated_prefix"] else "unverified"
                if index in self.state["pending_steps"]:
                    status = "pending side effect"
                if index == self.state["failed_step"]:
                    status = "failed"
                lines.append(f"{index}. [{status}] {describe_step(step)}")
            lines += ["", "## Canonical candidate", "", "```json",
                      json.dumps(candidate, ensure_ascii=False, indent=2), "```"]
        lines += ["", "## Checkpoint metadata", "", "```json",
                  json.dumps(self.state["checkpoint"], ensure_ascii=False, indent=2), "```",
                  "", "## Consequential intents (dispatch is not destination confirmation)", "",
                  "```json", json.dumps(self.state.get("effects", {}), ensure_ascii=False, indent=2), "```",
                  "", "## Reviews", ""]
        lines += [json.dumps(review, ensure_ascii=False) for review in self.state["reviews"]]
        lines += ["", "## Test cases", ""]
        lines += [json.dumps(case, ensure_ascii=False) for case in self.state["cases"]]
        lines += ["", "## Manual DOM discovery (untrusted page evidence)", "",
                  f"Chronological read evidence: {self.discovery_path}",
                  f"Read observations recorded: {self.state['discovery_count']}", ""]
        lines += [json.dumps(observation, ensure_ascii=False) for observation in self.state["discovery"]]
        lines += ["", "## Recent evidence", ""]
        lines += [json.dumps(event, ensure_ascii=False) for event in self.state["events"][-15:]]
        _atomic(self.progress_path, "\n".join(lines) + "\n")

    def propose(self, item: dict) -> dict:
        """Canonicalize a candidate; only the next unverified step can change."""
        if self.has_uncertain_effects():
            raise ValueError("unresolved consequential action intent: hold for destination reconciliation")
        raw = item.get("steps") if isinstance(item, dict) else None
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:
            raise ValueError("propose between one and sixty steps")
        if any(clean_step(step, loop=bool(item.get("each"))) is None for step in raw):
            raise ValueError("every proposed step must be valid; steps cannot be silently dropped")
        # The runtime requires two steps in a loop. Its first list-page step can
        # still be built and tested alone; a temporary wait validates configuration.
        partial_loop = len(raw) == 1 and bool(item.get("each"))
        value = copy.deepcopy(item)
        if partial_loop:
            value["steps"].append({"do": "wait", "ms": 100})
        canonical = clean(value)
        if partial_loop:
            canonical["steps"].pop()
        old = self.state["candidate"]
        prefix = max(self.state["validated_prefix"], self.state.get("rehearsed_prefix", 0))
        failed = self.state["failed_step"]
        if failed is not None and failed <= prefix:
            raise ValueError("a previously validated step failed; invalidate_from before repairing it")
        if len(raw) < prefix or len(raw) > prefix + 1:
            raise ValueError("validate the current step before extending by exactly one step")
        if old:
            if self.state.get("effects") and canonical.get("inputs") != old.get("inputs"):
                raise ValueError("input contract cannot change after consequential dispatch; destination reconciliation is required")
            if canonical["steps"][:prefix] != old["steps"][:prefix]:
                raise ValueError("the validated prefix is locked; use invalidate_from with a reason")
            if prefix and any(canonical.get(key) != old.get(key) for key in ("inputs", "each")):
                raise ValueError("validated inputs and each configuration are locked; invalidate from step 1")
        if old != canonical:
            # Preserve execution identity separately from reviewer-facing prose.
            # Never rewrite the original candidate fingerprint on a recorded case.
            old_fingerprint = self.candidate_fingerprint()
            for case in self.state["cases"]:
                if case.get("candidate_fingerprint") == old_fingerprint:
                    case.setdefault("execution_fingerprint", self.execution_fingerprint())
            self.state["reviews"] = []
            self.state["pending_steps"] = [n for n in self.state["pending_steps"] if n <= prefix]
        self.state["candidate"] = canonical
        self._event("proposal", step_count=len(raw), details="Candidate checkpoint")
        self._save()
        return _clone(canonical)

    def record_test(self, success_count: int, failed_step: int | None = None,
                    details: str = "", pending_steps: list[int] | None = None) -> dict:
        """Record executed evidence, never counting a skipped side effect as passed."""
        candidate = self.state["candidate"]
        if not candidate:
            raise ValueError("propose a candidate before recording a test")
        count = len(candidate["steps"])
        if type(success_count) is not int or not 0 <= success_count <= count:
            raise ValueError("success_count must be a contiguous prefix within the candidate")
        pending = (pending_steps if pending_steps is not None
                   else [n for n in self.state["pending_steps"] if n > success_count])
        if any(type(n) is not int or not 1 <= n <= count for n in pending):
            raise ValueError("pending side-effect step numbers must be within the candidate")
        pending = sorted(set(pending))
        if pending and success_count >= pending[0]:
            raise ValueError("skipped side effects cannot count as verified steps")
        if failed_step is not None and (type(failed_step) is not int or failed_step != success_count + 1
                                        or failed_step > count):
            raise ValueError("failed_step must be the first step after the successful prefix")
        prefix = self.state["validated_prefix"]
        if success_count < prefix and failed_step is None:
            raise ValueError("a shorter retest must identify the failure or explicitly invalidate evidence")
        # A regression remains visible and blocks proposals until an explicit
        # invalidation. Historical prefix evidence does not authorize advancement.
        self.state["validated_prefix"] = max(prefix, success_count)
        self.state["rehearsed_prefix"] = max(self.state.get("rehearsed_prefix", 0), success_count)
        self.state["failed_step"] = failed_step
        self.state["pending_steps"] = pending
        self.state["reviews"] = []
        self._event("test", success_count=success_count, failed_step=failed_step,
                    pending_steps=pending, details=details)
        self._save()
        return self.context()

    def record_rehearsal(self, success_count: int, pending_steps: list[int],
                         failed_step: int | None = None, details: str = "") -> dict:
        """Record dry probes, allowing continued authoring but retaining live gates.

        ``success_count`` here includes probes of skipped actions' targets. Those
        action numbers remain pending until a subsequent live ``record_test``.
        """
        candidate = self.state["candidate"]
        count = len(candidate["steps"]) if candidate else 0
        if type(success_count) is not int or not 0 <= success_count <= count or not candidate:
            raise ValueError("rehearsal success_count must be within the candidate")
        if any(type(n) is not int or not 1 <= n <= count for n in pending_steps):
            raise ValueError("pending side-effect step numbers must be within the candidate")
        if failed_step is not None and (type(failed_step) is not int or failed_step != success_count + 1
                                        or failed_step > count):
            raise ValueError("failed_step must follow the successful rehearsal prefix")
        prefix = self.state.get("rehearsed_prefix", 0)
        if success_count < prefix and failed_step is None:
            raise ValueError("a shorter rehearsal must identify the failure or invalidate evidence")
        pending = sorted(set(self.state["pending_steps"] + pending_steps))
        live_prefix = min(success_count, pending[0] - 1) if pending else success_count
        self.state["validated_prefix"] = max(self.state["validated_prefix"], live_prefix)
        self.state["rehearsed_prefix"] = max(prefix, success_count)
        self.state["failed_step"] = failed_step
        self.state["pending_steps"] = pending
        self.state["reviews"] = []
        self._event("rehearsal", success_count=success_count, failed_step=failed_step,
                    pending_steps=pending, details=details)
        self._save()
        return self.context()

    def invalidate_from(self, step: int, reason: str) -> dict:
        """Discard downstream validation before deliberately repairing earlier work."""
        count = len((self.state["candidate"] or {}).get("steps", []))
        if type(step) is not int or not 1 <= step <= count:
            raise ValueError("invalidate_from needs a one-based candidate step")
        if not str(reason).strip():
            raise ValueError("invalidating evidence requires a reason")
        self.state["validated_prefix"] = min(self.state["validated_prefix"], step - 1)
        self.state["rehearsed_prefix"] = min(self.state.get("rehearsed_prefix", 0), step - 1)
        self.state["failed_step"] = None
        self.state["pending_steps"] = [n for n in self.state["pending_steps"] if n < step]
        self.state["reviews"] = []
        self.state["events"] = [event for event in self.state["events"]
                                if event["kind"] not in {"test", "rehearsal"} or (
                                    event.get("success_count", 0) < step
                                    and (event.get("failed_step") or 0) < step
                                    and all(n < step for n in event.get("pending_steps", [])))]
        self.state["cases"] = [case for case in self.state["cases"] if case["step_count"] < step]
        self._event("invalidation", step=step, details=reason)
        self._save()
        return self.context()

    @staticmethod
    def sample_identity(inputs: dict) -> str:
        return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    def has_uncertain_effects(self) -> bool:
        return any(receipt.get("status") != "dispatch_returned"
                   for receipt in self.state.get("effects", {}).values())

    def effects_for_sample(self, inputs: dict) -> list[dict]:
        matched = []
        for receipt in self.state.get("effects", {}).values():
            dependencies = receipt.get("dependencies")
            if dependencies is None:
                # Older receipts lack dependency proof: never infer a safe new sample.
                matched.append(receipt)
                continue
            projected = {name: inputs.get(name, "") for name in dependencies}
            if receipt["sample"] == self.sample_identity(projected):
                matched.append(receipt)
        return _clone(matched)

    def effect_dependencies(self, inputs: dict, step_number: int) -> list[str]:
        """Include inputs referenced by the executed prefix and input templates.

        Unused form inputs must not supply an artificial new dispatch identity.
        Copied values are not sample identity: changing readback cannot authorize resend.
        """
        from automations import VAR
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key != "as":
                        yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)
        dependencies, copied = set(), set()
        for step in (self.state.get("candidate") or {}).get("steps", [])[:step_number]:
            referenced = {m[1] for text in strings(step) for m in VAR.finditer(text)}
            current = (referenced & inputs.keys()) - copied
            while True:
                nested = ({m[1] for name in current for m in VAR.finditer(str(inputs[name]))}
                          & inputs.keys()) - copied
                if nested <= current:
                    break
                current |= nested
            dependencies |= current
            if step.get("do") == "copy":
                copied.add(step["as"])
        return sorted(dependencies)

    def begin_effect(self, inputs: dict, link: str, step_number: int, step: dict,
                     execution: dict | None = None, recovery_contract=None, recovery_observer=None) -> str:
        """Persist uncertain intent before dispatch; it is never destination confirmation.

        Identity excludes candidate and step definition: repairing a dispatched slot
        cannot silently turn it into a new operation. Even interrupted intent holds.
        """
        if self.has_uncertain_effects():
            raise ValueError("unresolved consequential action intent: hold for destination reconciliation")
        dependencies = self.effect_dependencies(inputs, step_number)
        sample = self.sample_identity({name: inputs[name] for name in dependencies})
        key = self.sample_identity({"build": self.build_id, "sample": sample,
                                    "link": str(link or ""), "step": step_number})
        effects = self.state.setdefault("effects", {})
        if key in effects:
            raise ValueError("this operation already has durable intent; destination reconciliation is required before any retry")
        recovery = None
        if recovery_contract is not None:
            from automation_recovery import RecoveryContract
            if not isinstance(recovery_contract, RecoveryContract):
                raise ValueError("recovery requires a runtime-frozen contract")
            recovery = RecoveryContract.from_dict(recovery_contract.to_dict())
        observer = None
        if recovery_observer is not None:
            if recovery is None:
                raise ValueError("an observer requires a frozen recovery contract")
            observer = self._clean_recovery_observer(recovery_observer, recovery)
        effects[key] = {"sample": sample, "dependencies": dependencies, "link": str(link or ""), "step": step_number,
                        "definition": _clone(step), "status": "uncertain", "at": _now()}
        if execution is not None:
            effects[key]["execution"] = _clone(execution)
        if recovery is not None:
            effects[key]["recovery_contract"] = recovery.to_dict()
            effects[key]["recovery_fingerprint"] = recovery.fingerprint
        if observer is not None:
            effects[key]["recovery_observer"] = observer
            effects[key]["observer_fingerprint"] = self._recovery_observer_fingerprint(observer)
        self._save()
        return key

    def recovery_contract(self, key: str):
        """Load only the contract pinned before dispatch; never retrofit an intent."""
        from automation_recovery import RecoveryContract, RecoveryHeld
        receipt = self.state.get("effects", {}).get(key, {})
        contract = RecoveryContract.from_dict(receipt.get("recovery_contract"))
        if contract.fingerprint != receipt.get("recovery_fingerprint"):
            raise RecoveryHeld("recovery contract differs from the pre-dispatch fingerprint")
        return contract

    @staticmethod
    def _recovery_observer_fingerprint(observer: dict) -> str:
        return hashlib.sha256(json.dumps(observer, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _clean_recovery_observer(observer, contract):
        from automation_recovery import RecoveryHeld
        selectors_keys = {"collection", "row", "id", "identity", "fields", "total_count"}
        if (not isinstance(observer, dict) or set(observer) != {"destination_url", "selectors"}
                or observer["destination_url"] != contract.to_dict()["destination_url"]):
            raise RecoveryHeld("observer destination differs from the frozen contract")
        selectors = observer["selectors"]
        if not isinstance(selectors, dict) or set(selectors) != selectors_keys:
            raise RecoveryHeld("invalid frozen record selectors")
        fields = selectors["fields"]
        if (not isinstance(fields, dict) or not 1 <= len(fields) <= 32
                or set(fields) != set(contract.to_dict()["expected_fields"])):
            raise RecoveryHeld("observer fields differ from the frozen payload")
        for value in [*(selectors[key] for key in selectors_keys - {"fields"}), *fields.values()]:
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise RecoveryHeld("record selectors must be bounded nonempty strings")
        return _clone(observer)

    def recovery_observer(self, key: str) -> dict:
        from automation_recovery import RecoveryHeld
        contract = self.recovery_contract(key)
        receipt = self.state["effects"][key]
        observer = self._clean_recovery_observer(receipt.get("recovery_observer"), contract)
        if self._recovery_observer_fingerprint(observer) != receipt.get("observer_fingerprint"):
            raise RecoveryHeld("observer differs from the pre-dispatch fingerprint")
        return observer

    def record_recovery_evidence(self, key: str, observation: dict, runtime_source: str) -> dict:
        """Runtime observation only; confirmation never grants replay or restoration."""
        from automation_recovery import reconcile
        contract = self.recovery_contract(key)
        self.recovery_observer(key)  # both the contract and its reader must remain pinned
        evidence = reconcile(contract, observation, runtime_source=runtime_source).to_dict()
        self.state["effects"][key]["confirmation"] = _clone(evidence)
        self._event("reconciliation", receipt=key, record_id=evidence["record_id"],
                    details="Destination record observed; browser continuation remains held")
        self._save()
        return _clone(evidence)

    def effect_returned(self, key: str) -> None:
        self.state["effects"][key]["status"] = "dispatch_returned"
        self.state["effects"][key]["returned_at"] = _now()
        self._save()

    def record_case(self, inputs: dict, link: str, mode: str, resulting_url: str,
                    passed: bool, step_count: int, copied: dict,
                    outcome: Any = None, details: str = "", metadata: dict | None = None) -> dict:
        """Keep actual test observations separately from rolling tool events.

        A case describes a particular candidate and sample, not a claim that
        the complete goal was achieved. ``passed`` means its executor passed;
        ``outcome`` is the caller's observed read-back, never inferred here.
        Rehearsal cases never prove live side effects. Only ordinary automation
        inputs and copied values belong here, never credentials or passwords.
        """
        candidate = self.state["candidate"]
        if not candidate:
            raise ValueError("propose a candidate before recording a case")
        if mode not in {"live", "rehearsal"}:
            raise ValueError("case mode must be live or rehearsal")
        if type(passed) is not bool:
            raise ValueError("case passed must be a boolean")
        if type(step_count) is not int or not 1 <= step_count <= len(candidate["steps"]):
            raise ValueError("case step_count must be within the candidate")
        if not isinstance(inputs, dict) or not isinstance(copied, dict):
            raise ValueError("case inputs and copied values must be objects")
        case = {"at": _now(), "candidate_fingerprint": self.candidate_fingerprint(),
                "execution_fingerprint": self.execution_fingerprint(),
                "candidate_step_count": len(candidate["steps"]),
                "inputs": _clone(inputs), "link": str(link or ""), "mode": mode,
                "resulting_url": str(resulting_url or ""), "passed": passed,
                "step_count": step_count, "copied": _clone(copied),
                "outcome": _clone(outcome), "details": str(details)[:MAX_DETAILS],
                "metadata": _clone(metadata or {})}
        self.state["cases"] = (self.state["cases"] + [case])[-MAX_CASES:]
        # Review approval describes the evidence the reviewer actually saw.
        # Any later test observation requires a new review, including a failed loop.
        self.state["reviews"] = []
        self._save()
        return _clone(case)

    def candidate_fingerprint(self) -> str:
        """Identity of all canonical candidate fields, including input labels."""
        if not self.state["candidate"]:
            return ""
        encoded = json.dumps(self.state["candidate"], ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def execution_fingerprint(self) -> str:
        """All canonical fields except descriptive about text; inputs remain exact."""
        candidate = self.state["candidate"]
        if not candidate:
            return ""
        return hashlib.sha256(json.dumps({k: v for k, v in candidate.items() if k != "about"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def case_matches_execution(self, case: dict) -> bool:
        if case.get("candidate_fingerprint") == self.candidate_fingerprint():
            return True
        if case.get("execution_fingerprint"):
            return case["execution_fingerprint"] == self.execution_fingerprint()
        # Older journals lack an execution fingerprint. Recover only when the
        # original checkpoint description reconstructs the exact recorded hash.
        original_about = self.state.get("checkpoint", {}).get("build", {}).get("about")
        if not isinstance(original_about, str) or not self.state["candidate"]:
            return False
        candidate = {**self.state["candidate"], "about": original_about}
        fingerprint = hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return case.get("candidate_fingerprint") == fingerprint

    def record_review(self, kind: str, verdict: str, details: str = "", metadata: dict | None = None) -> dict:
        review = {"kind": str(kind)[:80], "verdict": str(verdict)[:80],
                  "details": str(details)[:MAX_DETAILS], "at": _now(),
                  "validated_prefix": self.state["validated_prefix"], "metadata": _clone(metadata or {})}
        self.state["reviews"] = (self.state["reviews"] + [review])[-20:]
        # Current approvals are cleared on repair/test. Preserve the review's
        # reason in the durable event trail rather than losing why it changed.
        self._event("review", details=f"{kind}: {verdict}\n{review['details']}",
                    candidate=self.candidate_fingerprint())
        self._save()
        return _clone(review)

    def save_checkpoint(self, metadata: dict) -> None:
        self.state["checkpoint"] = _clone(metadata)
        self._save()

    def record_discovery(self, tool: str, args: dict, result: str) -> None:
        """Persist read-only DOM probes, never action arguments or typed values.

        Each read has a bounded chronological record. Recovery keeps the latest
        observation for each selector, including failed probes, so repeated full
        page reads cannot erase the successful selectors discovered earlier.
        Page text remains untrusted and may be stale: re-read before acting.
        """
        if tool not in {"ghost_read", "ghost_records"}:
            return
        safe_args = {}
        if tool == "ghost_records":
            spec = _record_spec(args.get("spec") if isinstance(args, dict) else None)
            if spec is None:
                return
            safe_args = {"spec": spec}
        elif isinstance(args, dict):
            if isinstance(args.get("selector"), str):
                safe_args["selector"] = args["selector"][:2000]
            for key in ("max_chars", "maxChars"):
                if type(args.get(key)) is int:
                    safe_args[key] = args[key]
        observation = {"at": _now(), "tool": tool, "args": safe_args,
                       "result": str(result)[:MAX_DISCOVERY_RESULT]}
        fd = os.open(self.discovery_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            size = os.fstat(stream.fileno()).st_size
            if size and os.pread(stream.fileno(), 1, size - 1) != b"\n":
                stream.write("\n")  # isolate an interrupted append from this new observation
            stream.write(json.dumps(observation, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # The private chronological log retains the fuller bounded page result.
        observation["result"] = observation["result"][:2000]
        self.state["discovery"] = _retain_discovery(self.state["discovery"], observation)
        self.state["discovery_count"] += 1
        self._save()

    def _restore_discovery(self) -> None:
        """Recover a read appended just before a process died saving state.json."""
        if not self.discovery_path.exists():
            return
        recent, count = [], 0
        with self.discovery_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # an interrupted final append is not evidence
                if (not isinstance(entry, dict) or entry.get("tool") not in {"ghost_read", "ghost_records"}
                        or not isinstance(entry.get("args"), dict)
                        or not isinstance(entry.get("result"), str)):
                    continue
                if entry["tool"] == "ghost_records":
                    spec = _record_spec(entry["args"].get("spec"))
                    if spec is None:
                        continue
                    entry["args"] = {"spec": spec}
                entry["result"] = entry["result"][:2000]
                recent = _retain_discovery(recent, entry)
                count += 1
        self.state["discovery"], self.state["discovery_count"] = recent, count

    def set_plan(self, plan: list) -> None:
        if (not isinstance(plan, list) or not 1 <= len(plan) <= 120
                or any(not isinstance(step, str) or not step.strip() or len(step) > 500
                       for step in plan)):
            raise ValueError("plan needs one to 120 nonempty text steps, up to 500 characters each")
        self.state["plan"] = _clone(plan)
        self._save()

    def context(self, full_cases: bool = False) -> dict:
        """Compact recovery context, safe for the caller to mutate independently."""
        result = _clone({key: value for key, value in self.state.items() if key != "effects"})
        effects = self.state.get("effects", {})
        returned = [key for key, receipt in effects.items() if receipt.get("status") == "dispatch_returned"]
        uncertain = [key for key, receipt in effects.items() if receipt.get("status") != "dispatch_returned"]
        retained = returned
        if len(returned) > LOOP_CONTEXT_ITEMS:
            half = LOOP_CONTEXT_ITEMS // 2
            retained = returned[:half] + returned[-half:]
        selected = set(retained) | set(uncertain)
        omissions, status_counts = {}, {}
        for receipt in effects.values():
            status = str(receipt.get("status", "uncertain"))
            status_counts[status] = status_counts.get(status, 0) + 1
        result["effects"] = {key: _compact_loop_evidence(receipt, f"effects.{key}", omissions)
                             for key, receipt in effects.items() if key in selected}
        omitted = len(returned) - len(retained)
        result["effect_context"] = {
            "complete": not omitted and not omissions,
            "receipt_count": len(effects), "status_counts": status_counts,
            "retained_receipt_count": len(selected), "retained_uncertain_count": len(uncertain),
            "omitted_returned_count": omitted, "selection": "all uncertain; returned head and tail",
            "state_path": str(self.path), "omissions": omissions,
            "verified_append_count": sum(r.get("status") == "dispatch_returned"
                and bool(r.get("verified_append")) for r in effects.values()),
            "verified_append_notice": ("Runtime-pinned append payloads preserve historical successful writes "
                "even if invalidation discarded earlier cases. For an unchanged terminal keyed append, "
                "test_automation scope=revalidate freshly checks research and the exact original row "
                "with writes disabled, without a frozen recovery observer. This is not replay permission "
                "or current-candidate proof. Unknown, changed, missing and legacy outcomes still hold."),
            "notice": ("Returned receipts are sampled and long fields may be shortened. All uncertain intents are retained. "
                       "Complete durable receipts are in state_path; dispatch return never proves destination confirmation."),
        }
        result["candidate_fingerprint"] = self.candidate_fingerprint()
        for case in result["cases"]:
            case["current_candidate"] = case["candidate_fingerprint"] == result["candidate_fingerprint"]
            case["current_execution"] = self.case_matches_execution(case)
            if isinstance(case.get("outcome"), dict) and isinstance(case["outcome"].get("content"), str):
                case["outcome"]["content"] = case["outcome"]["content"][:2500]
            if case.get("metadata", {}).get("scope") in {"loop", "loop_item"}:
                omissions = {}
                for field in ("outcome", "metadata", "copied"):
                    case[field] = _compact_loop_evidence(case[field], field, omissions)
                if omissions:
                    case["context_evidence"] = {
                        "complete": False,
                        "notice": "Only sampled/shortened loop evidence is transmitted. Complete retained evidence is in state_path; omitted outcomes are not shown here.",
                        "omissions": omissions,
                    }
        if not full_cases:
            result["cases"] = result["cases"][-8:]
        result["case_count"] = len(self.state["cases"])
        result["events"] = result["events"][-8:]
        result["state_path"] = str(self.path)
        result["progress_path"] = str(self.progress_path)
        result["discovery_path"] = str(self.discovery_path)
        return result


def list_builds(root: Path | str | None = None) -> list[dict]:
    """Discover recoverable goals without requiring people to save internal UUIDs."""
    folder = Path(root) if root is not None else Path.home() / ".ghost" / "builds"
    if not folder.is_dir() or folder.is_symlink():
        return []
    records = []
    for directory in folder.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        path = directory / "state.json"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("version") != 1 or str(uuid.UUID(directory.name)) != state.get("build_id"):
                continue
            checkpoint = state.get("checkpoint") or {}
            name = (checkpoint.get("build") or {}).get("name")
            if not name:
                continue
            records.append({"id": state["build_id"], "name": name,
                            "status": checkpoint.get("status", "interrupted"),
                            "validated": state.get("validated_prefix", 0),
                            "steps": len((state.get("candidate") or {}).get("steps", [])),
                            "updated_at": state.get("updated_at", "")})
        except (ValueError, OSError, TypeError, AttributeError):
            continue
    return sorted(records, key=lambda r: r["updated_at"], reverse=True)[:50]
