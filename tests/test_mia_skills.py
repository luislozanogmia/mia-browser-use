import asyncio
import json

import ghost_chat
import mia_skills
from ghost_chat import ChatHub
from tests.test_chat import FakeSession


def test_both_skills_are_listed_with_where_they_are_and_what_they_are_for():
    skills = {s["name"]: s for s in mia_skills.index()}
    assert set(skills) >= {"building-automations", "managing-bots"}
    for skill in skills.values():
        assert skill["path"].endswith("/SKILL.md") and len(skill["description"]) > 60
    index = mia_skills.prompt_index()
    assert "building-automations" in index and skills["managing-bots"]["path"] in index and "load_skills" in index


def test_a_skill_loads_without_its_frontmatter_and_unknown_names_load_nothing():
    text = mia_skills.load("building-automations")
    assert text.startswith("# Building Play Automations") and "{{first_name}}" in text and "---" not in text[:5]
    assert mia_skills.load("../../etc/passwd") == "" and mia_skills.load("nope") == ""


def test_mia_reads_the_skill_she_asks_for_then_plans():
    plan = {"reply": "Recording it once.", "tasks": []}
    session = FakeSession([{"load_skills": ["building-automations", "made-up"]}, plan])
    systems = []

    def make(model, system, effort):
        systems.append(system)
        return session

    async def push(state):
        pass

    async def browser(tool, args):
        return True, {}

    async def main():
        hub = ChatHub(browser, push, session=make)
        answer = await hub._plan_with_claude("m", "make an automation")
        assert json.loads(answer) == plan
        assert "Skills:" in systems[0] and "managing-bots" in systems[0]
        assert "# Building Play Automations" in session.prompts[1] and "made-up" not in session.prompts[1]
        assert session.closed
    asyncio.run(main())


def test_a_plan_without_a_skill_request_is_used_as_it_is():
    assert ghost_chat.skill_request('{"reply": "Hi", "tasks": []}') == []
    assert ghost_chat.skill_request('{"load_skills": ["managing-bots"]}') == ["managing-bots"]
    assert ghost_chat.skill_request("not json") == []


def test_making_an_automation_reusable_comes_with_the_skill():
    systems = []

    def make(model, system, effort):
        systems.append(system)
        return FakeSession(['{"name": "x"}'])

    async def push(state):
        pass

    async def browser(tool, args):
        return True, {}

    async def main():
        await ChatHub(browser, push, session=make)._compose_with_claude("m", "steps")
        assert systems[0].startswith(ghost_chat.AUTOMATION_PROMPT) and "# Building Play Automations" in systems[0]
    asyncio.run(main())
