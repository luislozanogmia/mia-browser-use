"""A discovered live failure: embedded URL variables stayed literal during Play."""
import asyncio
import pytest
from automations import clean, expand_url
from tests.test_automations import play_hub, PlayBrowser, finish
from tests.test_selector_target import task_for


def test_search_component_is_encoded_once_without_changing_the_query_structure():
    url = expand_url('https://news.google.com/search?q={{company}}%20when%3A7d&hl=en-US',
                     {'company': 'Müller & Co / 200% #1?{{literal}}'})
    assert url == ('https://news.google.com/search?q=M%C3%BCller%20%26%20Co%20%2F%20200%25'
                   '%20%231%3F%7B%7Bliteral%7D%7D%20when%3A7d&hl=en-US')
    original = 'https://example.test/a%20b?q=one%26two#section'
    assert expand_url('{{link}}', {'link': original}) == original
    with pytest.raises(RuntimeError, match='needs a web address'):
        expand_url('{{link}}', {'link': 'javascript:alert(1)'})


def test_first_and_later_open_steps_use_the_same_resolved_url_in_saved_play(tmp_path):
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser, tmp_path)
        item = hub.scripts.add(clean({'name': 'Search URL regression', 'inputs': [{'name': 'company'}],
                                     'steps': [{'do': 'open', 'url': 'https://reports.example/search?q={{company}}'},
                                               {'do': 'open', 'url': 'https://reports.example/news?q={{company}}'}]}))
        task = await hub.play(item, {'company': 'Müller & Co 200%'})
        await finish(task)
        assert task.status == 'done', task.result
        navigation = [a['url'] for c, a in browser.calls if c in {'ghost_tab_open', 'ghost_navigate'}]
        assert navigation
        assert all('{{' not in url and 'M%C3%BCller%20%26%20Co%20200%25' in url for url in navigation)
        assert any('/search?' in url for url in navigation)
        assert any('/news?' in url for url in navigation)
    asyncio.run(main())


def test_builder_bootstrap_and_replayed_step_agree(tmp_path):
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser, tmp_path)
        task = task_for(hub)
        task.build = {'name': 'Fresh URL test'}
        task.approved_urls.add('https://reports.example/search?q=A%20%26%20B')
        result = await hub.test_automation(task, {'automation': {
            'name': 'Fresh URL test', 'inputs': [{'name': 'company'}], 'steps': [
                {'do': 'open', 'url': 'https://reports.example/search?q={{company}}'}]},
            'inputs': {'company': 'A & B'}})
        assert 'Test passed' in result, result
        navigation = [a['url'] for c, a in browser.calls if c in {'ghost_tab_open', 'ghost_navigate'}]
        assert navigation and set(navigation) == {'https://reports.example/search?q=A%20%26%20B'}
    asyncio.run(main())


def test_declared_path_preserves_segments_while_default_and_query_keep_atom_encoding():
    template = 'https://github.com/{{repository}}/releases?q={{search}}'
    values = {'repository': 'pallets/flask', 'search': 'A/B?x=1#fragment'}
    assert expand_url(template, values, ['repository']) == (
        'https://github.com/pallets/flask/releases?q=A%2FB%3Fx%3D1%23fragment')
    assert expand_url(template, values) == (
        'https://github.com/pallets%2Fflask/releases?q=A%2FB%3Fx%3D1%23fragment')
    assert expand_url('https://example.test/{{path}}/details',
                      {'path': 'Müller & Co/a?b#c%{{literal}}'}, ['path']) == (
        'https://example.test/M%C3%BCller%20%26%20Co/a%3Fb%23c%25%7B%7Bliteral%7D%7D/details')


@pytest.mark.parametrize('value', ['', '/other', 'owner/', 'owner//repo', '../repo',
                                  'owner/../repo', 'owner/./repo', 'owner\\repo'])
def test_path_rejects_empty_or_traversal_segments(value):
    with pytest.raises(RuntimeError, match='without traversal'):
        expand_url('https://github.com/{{repository}}/releases', {'repository': value}, ['repository'])


@pytest.mark.parametrize('template,names', [
    ('https://{{repository}}/releases', ['repository']),
    ('https://example.test/releases?q={{repository}}', ['repository']),
    ('https://example.test/{{repository}}#{{repository}}', ['repository']),
    ('https://example.test/{{repository}}?q={{repository}}', ['repository']),
    ('{{repository}}', ['repository']),
    ('https://example.test/{{repository}}', ['undeclared']),
    ('https://example.test/{{repository}}', ['repository', 'repository']),
    ('https://example.test/{{repository}}', 'repository'),
])
def test_path_configuration_cannot_move_host_query_fragment_or_whole_url(template, names):
    from automations import clean_step
    step = {'do': 'open', 'url': template, 'path_inputs': names}
    assert clean_step(step) is None
    with pytest.raises(RuntimeError, match='path_inputs'):
        expand_url(template, {'repository': 'owner/repo'}, names)


@pytest.mark.parametrize('builder', [False, True])
def test_builder_and_saved_play_navigate_declared_path_exactly(tmp_path, builder):
    async def main():
        browser = PlayBrowser()
        hub = play_hub(browser, tmp_path)
        raw = {'name': 'Direct release path', 'inputs': [{'name': 'repository'}],
               'steps': [{'do': 'open', 'url': 'https://reports.example/{{repository}}/releases',
                          'path_inputs': ['repository']}]}
        item = clean(raw)
        assert item['steps'][0]['path_inputs'] == ['repository']
        if builder:
            task = task_for(hub)
            task.build = {'name': raw['name']}
            task.approved_urls.add('https://reports.example/pallets/flask/releases')
            out = await hub.test_automation(task, {'automation': item, 'inputs': {'repository': 'pallets/flask'}})
            assert 'Test passed' in out
        else:
            item = hub.scripts.add(item)
            task = await hub.play(item, {'repository': 'pallets/flask'})
            await finish(task)
            assert task.status == 'done', task.result
        urls = [a['url'] for c, a in browser.calls if c in {'ghost_tab_open', 'ghost_navigate'}]
        assert urls and set(urls) == {'https://reports.example/pallets/flask/releases'}
    asyncio.run(main())
