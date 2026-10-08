"""Manual DOM discovery must survive model renewal without retaining fill args."""

import asyncio
import json
import os

import pytest

import ghost_chat
from automation_build import BuildJournal, MAX_DISCOVERY, list_builds
from ghost_chat import Agent, Task
from tests.test_automations import PlayBrowser, SCRIPT, builder_hub, play_hub


RECORD_SPEC = {"collection": "#records", "row": ".record", "id": ".id", "identity": ".identity",
               "fields": {"name": ".name"}, "total_count": "#total"}


def test_record_selectors_survive_reads_and_restart_with_bounded_context(tmp_path):
    journal = BuildJournal(root=tmp_path)
    journal.record_discovery("ghost_records", {"spec": RECORD_SPEC, "value": "do not retain"}, "Complete collection")
    for n in range(50):
        journal.record_discovery("ghost_read", {}, f"Page {n}")
    loaded = BuildJournal.load(journal.build_id, root=tmp_path)
    assert loaded.context()["discovery"][0]["args"] == {"spec": RECORD_SPEC}
    assert loaded.context()["discovery_count"] == 51
    assert "do not retain" not in loaded.discovery_path.read_text()
    for n in range(7):
        loaded.record_discovery("ghost_records", {"spec": {**RECORD_SPEC, "collection": f"#records{n}"}}, "Held")
    probes = [e for e in loaded.context()["discovery"] if e["tool"] == "ghost_records"]
    assert len(probes) == 4
    assert [e["args"]["spec"]["collection"] for e in probes] == [f"#records{n}" for n in range(3, 7)]
    loaded.record_discovery("ghost_records", {"spec": {**RECORD_SPEC, "fields": {"name": "x" * 501}}}, "Invalid")
    assert loaded.context()["discovery_count"] == 58
    restored = BuildJournal.load(journal.build_id, root=tmp_path)
    assert restored.context()["discovery"] == loaded.context()["discovery"]
    assert len(restored.discovery_path.read_text().splitlines()) == 58


def test_worker_renewal_keeps_record_probe_without_treating_it_as_confirmation(tmp_path):
    async def main():
        steps = [{"tool": "ghost_records", "args": {"spec": RECORD_SPEC}}]
        steps += [{"tool": "ghost_read", "args": {}} for _ in range(39)]
        hub = builder_hub(PlayBrowser(), [steps, [{"fail": "Intentional renewal check"}]], {}, tmp_path)
        task = Task(1, "Build records", "Discover records", "", "build", "parallel", "claude-opus-5-5", Agent(1, 91))
        task.build = {"name": "Records"}
        task.build_journal = BuildJournal(root=tmp_path / "builds")
        hub.tasks[task.id] = task
        async def act(task, step, allowed):
            return "Complete records observed" if step["tool"] == "ghost_records" else "Full page"
        hub.act = act
        await hub.work(task)
        prompt = hub.made[1]["session"].prompts[0]
        assert "#records" in prompt and "#total" in prompt and "Complete records observed" in prompt
        loaded = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "builds")
        assert loaded.context()["discovery_count"] == 40
        assert not loaded.state["effects"]
        assert loaded.state["validated_prefix"] == 0
    asyncio.run(main())


def test_large_record_probe_is_bounded_and_marks_omissions(tmp_path):
    async def main():
        hub = play_hub(PlayBrowser(), tmp_path)
        task = Task(1, "Build records", "Discover records", "", "build", "parallel", "", Agent(1, 91))
        records = [{"id": str(n), "identity": str(n), "fields": {"name": "x" * 4096}}
                   for n in range(1000)]
        async def call(task, tool, args):
            return {"destination_url": "https://records.example/", "complete": True, "records": records}
        hub.play_call = call
        out = await hub.act(task, {"tool": "ghost_records", "args": {"spec": RECORD_SPEC}}, ghost_chat.BUILD_TOOLS)
        assert "untrusted page data" in out
        data = json.loads(out.split("<<<page\n", 1)[1].rsplit("\npage>>>", 1)[0])
        assert data["display_complete"] is False
        assert len(data["observation"]["records"]) == 8
        assert data["omissions"]["observation.records"]["omitted_entries"] == 992
        assert len(out) < 12000
        assert records[-1]["fields"]["name"] == "x" * 4096
    asyncio.run(main())


def test_discovery_keeps_selector_findings_and_full_bounded_chronology(tmp_path):
    journal = BuildJournal(root=tmp_path)
    journal.record_discovery("ghost_read", {"selector": "#working", "value": "secret", "actor": 1},
                             "Found the correct receipt" + "x" * 14000)
    journal.record_discovery("ghost_read", {"selector": "#wrong"}, "Error: no matching element")
    for index in range(50):
        journal.record_discovery("ghost_read", {}, f"Full page {index}")
    journal.record_discovery("ghost_fill", {"selector": "#password", "value": "secret"}, "secret")
    loaded = BuildJournal.load(journal.build_id, root=tmp_path)
    context = loaded.context()
    assert context["discovery_count"] == 52
    assert len(context["discovery"]) == 3
    assert context["discovery"][0]["args"] == {"selector": "#working"}
    assert "no matching element" in context["discovery"][1]["result"]
    assert context["discovery"][2]["result"] == "Full page 49"
    evidence = [json.loads(line) for line in journal.discovery_path.read_text().splitlines()]
    assert len(evidence) == 52 and len(evidence[0]["result"]) == 12000
    assert "secret" not in journal.discovery_path.read_text()
    assert os.stat(journal.discovery_path).st_mode & 0o777 == 0o600
    assert "#working" in journal.progress_path.read_text()
    context["discovery"][0]["result"] = "mutated"
    assert loaded.context()["discovery"][0]["result"] != "mutated"


def test_discovery_recovery_is_bounded_and_refuses_symlink_log(tmp_path):
    journal = BuildJournal(root=tmp_path)
    for index in range(MAX_DISCOVERY + 5):
        journal.record_discovery("ghost_read", {"selector": f"#item-{index}"}, "x" * 5000)
    assert len(journal.context()["discovery"]) == MAX_DISCOVERY
    assert all(len(entry["result"]) == 2000 for entry in journal.context()["discovery"])
    journal.discovery_path.unlink()
    target = tmp_path / "preserved"
    target.write_text("keep")
    journal.discovery_path.symlink_to(target)
    with pytest.raises(OSError):
        journal.record_discovery("ghost_read", {}, "overwrite")
    assert target.read_text() == "keep"


def test_discovery_recovers_append_before_state_save_and_ignores_partial_tail(tmp_path, monkeypatch):
    journal = BuildJournal(root=tmp_path)
    def interrupted_save():
        raise OSError("Interrupted before atomic state save")
    monkeypatch.setattr(journal, "_save", interrupted_save)
    with pytest.raises(OSError):
        journal.record_discovery("ghost_read", {"selector": "#new"}, "New selector works")
    with journal.discovery_path.open("a") as stream:
        stream.write('{"tool": "ghost_read"')
    loaded = BuildJournal.load(journal.build_id, root=tmp_path)
    assert loaded.context()["discovery_count"] == 1
    assert loaded.context()["discovery"][0]["result"] == "New selector works"
    loaded.record_discovery("ghost_read", {"selector": "#second"}, "Second selector works")
    recovered = BuildJournal.load(journal.build_id, root=tmp_path)
    assert recovered.context()["discovery_count"] == 2
    assert recovered.context()["discovery"][-1]["result"] == "Second selector works"


def test_worker_renewal_and_fresh_load_keep_earlier_manual_dom_discovery(tmp_path):
    async def main():
        steps = [{"tool": "ghost_read", "args": {"selector": "#receipt"}}]
        steps += [{"tool": "ghost_read", "args": {}} for _ in range(39)]
        hub = builder_hub(PlayBrowser(), [steps, [{"fail": "Intentional stop after renewal"}]], {}, tmp_path)
        task = Task(1, "Build report", "Discover receipt", "", "build", "parallel",
                    "claude-opus-5-5", Agent(1, 91))
        task.build = {"name": "Receipt"}
        task.build_journal = BuildJournal(root=tmp_path / "builds")
        hub.tasks[task.id] = task

        async def act(task, step, allowed):
            return "Receipt selector works" if step["args"].get("selector") else "Latest full page"
        hub.act = act
        await hub.work(task)
        assert len(hub.made) == 2 and hub.made[0]["session"].closed
        renewal = hub.made[1]["session"].prompts[0]
        assert "Receipt selector works" in renewal and "#receipt" in renewal
        loaded = BuildJournal.load(task.build_journal.build_id, root=tmp_path / "builds")
        assert loaded.context()["discovery_count"] == 40
        assert loaded.context()["discovery"][0]["result"] == "Receipt selector works"
    asyncio.run(main())


@pytest.mark.parametrize("reference", ["name", "uuid", "uppercase_uuid"])
def test_resume_live_build_does_not_load_or_rewrite_its_journal(tmp_path, monkeypatch, reference):
    async def main():
        hub = play_hub(PlayBrowser(), tmp_path)
        task = Task(1, "Build report", "Report", "", "build", "parallel", "", Agent(1, 91))
        task.build_journal = BuildJournal(root=tmp_path / "builds")
        task.build_journal.save_checkpoint({"build": {"name": SCRIPT["name"]}, "status": "working"})
        hub.tasks[task.id] = task
        gate = asyncio.Event()
        task.job = asyncio.create_task(gate.wait())
        monkeypatch.setattr(ghost_chat, "list_builds", lambda: list_builds(task.build_journal.root))
        class Loader:
            @staticmethod
            def load(identifier):
                raise AssertionError("Loading would regenerate a live owner's progress")
        monkeypatch.setattr(ghost_chat, "BuildJournal", Loader)
        identifier = (SCRIPT["name"] if reference == "name" else task.build_journal.build_id)
        if reference == "uppercase_uuid":
            identifier = identifier.upper()
        before = task.build_journal.path.read_bytes()
        try:
            await hub.resume_build(identifier)
            assert "already running" in hub.messages[-1]["text"]
            assert task.build_journal.path.read_bytes() == before
            assert len(hub.tasks) == 1 and not task.job.done()
        finally:
            gate.set()
            await task.job
    asyncio.run(main())
