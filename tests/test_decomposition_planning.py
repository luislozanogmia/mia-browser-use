"""Progressive planning guides must produce an executable plan, never an empty dispatch."""
import asyncio
import json
import pytest
from ghost_chat import ChatHub, parse_plan
from tests.test_chat import FakeSession


def test_progressive_guides_preserve_producers_and_final_integration():
    plan = {'reply': 'I will compare the evidence.', 'tasks': [
        {'title': 'Vendor A', 'goal': 'Read Vendor A pricing and keep dated source links.', 'kind': 'ask',
         'url': 'https://a.example/', 'done_when': 'Every requested price and limit has a source.'},
        {'title': 'Vendor B', 'goal': 'Read Vendor B pricing and keep dated source links.', 'kind': 'ask',
         'url': 'https://b.example/', 'done_when': 'Every requested price and limit has a source.'},
        {'title': 'Comparison', 'goal': 'Combine both results into the requested comparison; mark missing evidence.',
         'kind': 'ask', 'needs': [0, 1], 'done_when': 'Both vendors and every requested criterion are covered.'}]}
    session = FakeSession([{'load_skills': ['managing-bots']},
                           {'load_skills': ['decomposing-tasks']}, plan])
    async def no_browser(*args):
        raise AssertionError('Planning must not dispatch browser work')
    async def push(state): pass
    async def main():
        hub = ChatHub(no_browser, push, session=lambda *args: session)
        result = parse_plan(await hub._plan_with_claude('m', 'Compare both vendors'))
        assert len(result['tasks']) == 3
        assert result['tasks'][2]['needs'] == [0, 1]
        assert result['tasks'][0]['done_when'] == plan['tasks'][0]['done_when']
        assert session.closed and len(session.prompts) == 3
        assert 'PMI' in session.prompts[2] and 'McKinsey' in session.prompts[2]
        assert not hub.tasks
    asyncio.run(main())


def test_repeated_skill_requests_stop_without_dispatching_and_close_the_session():
    session = FakeSession([{'load_skills': ['decomposing-tasks']}] * 4)
    async def no_browser(*args): raise AssertionError('No plan exists')
    async def push(state): pass
    async def main():
        hub = ChatHub(no_browser, push, session=lambda *args: session)
        with pytest.raises(RuntimeError, match='no execution plan'):
            await hub._plan_with_claude('m', 'Large task')
        assert session.closed and not hub.tasks
    asyncio.run(main())
