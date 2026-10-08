"""Source attribution survives navigation, without leaking another tab's address."""
import asyncio
import pytest
import automations
from tests.test_automations import play_hub
from tests.test_selector_target import TargetBrowser, task_for


def test_url_copy_contract_and_variable_order():
    step={'do':'copy','source':'url','as':'news_url'}
    assert automations.clean_step(step)==step
    assert 'current page URL' in automations.describe_step(step)
    for extra in [{'css':'main'},{'text':'headline'},{'words':1},{'source':'html'},{'as':'bad name'}]:
        assert automations.clean_step({**step,**extra}) is None
    automations.validate_variables({'steps':[step,{'do':'open','url':'{{news_url}}'}]})
    with pytest.raises(ValueError):
        automations.validate_variables({'steps':[{'do':'open','url':'{{news_url}}'},step]})


def test_url_copy_uses_owned_tab_and_refreshes_after_navigation(tmp_path):
    async def main():
        hub=play_hub(TargetBrowser(),tmp_path);task=task_for(hub)
        current='https://news.google.com/search?q=Microsoft%20when%3A7d&hl=en-US'
        async def tabs(): return [{'id':999,'url':'https://unrelated.example/private'},{'id':task.tab_id,'url':current}]
        hub.open_tabs=tabs;values={}
        await hub.play_step(task,{'do':'copy','source':'url','as':'news_url'},values)
        assert values['news_url']==current
        current='https://www.linkedin.com/company/microsoft/about/?view=public'
        await hub.play_step(task,{'do':'copy','source':'url','as':'profile_url'},values)
        assert values['profile_url']==current
        assert 'Microsoft%20when%3A7d' in values['news_url']
        current='chrome://extensions'
        with pytest.raises(RuntimeError,match='no complete'):await hub.play_step(task,{'do':'copy','source':'url','as':'bad'},values)
        assert 'bad' not in values
    asyncio.run(main())


def test_expected_url_schema_and_dependencies_are_preserved():
    step={'do':'copy','source':'url','as':'release_url',
          'expected_url':'https://github.com/{{repository}}/releases/tag/{{version}}',
          'path_inputs':['repository']}
    assert automations.clean_step(step)==step
    prefix=[{'do':'copy','css':'h2','as':'version'},step]
    automations.validate_variables({'inputs':[{'name':'repository'}],'steps':prefix})
    with pytest.raises(ValueError,match='version'):
        automations.validate_variables({'inputs':[{'name':'repository'}],'steps':[step]})
    for extra in [{'expected_url':'javascript:alert(1)'}, {'path_inputs':['version','unknown']},
                  {'expected_url':'https://{{repository}}/details','path_inputs':['repository']}]:
        assert automations.clean_step({**step,**extra}) is None
    assert automations.clean_step({'do':'copy','source':'url','as':'url','path_inputs':['repository']}) is None


@pytest.mark.parametrize('destination', ['eventual', 'never', 'wrong_repository', 'other_tab_only'])
def test_expected_url_captures_only_observed_owned_target(tmp_path, monkeypatch, destination):
    import ghost_chat
    monkeypatch.setattr(ghost_chat,'URL_CAPTURE_TIMEOUT',0.03 if destination!='eventual' else 1)
    async def main():
        hub=play_hub(TargetBrowser(),tmp_path);task=task_for(hub)
        expected='https://github.com/pallets/flask/releases/tag/3.1.3'
        calls=[]
        async def tabs():
            calls.append(True)
            current='https://github.com/pallets/flask/releases'
            if destination=='eventual' and len(calls)>1:current=expected
            if destination=='wrong_repository':current='https://github.com/other/flask/releases/tag/3.1.3'
            return [{'id':999,'url':expected},{'id':task.tab_id,'url':current}]
        hub.open_tabs=tabs
        values={'repository':'pallets/flask','version':'3.1.3','release_url':'stale previous copy'}
        step={'do':'copy','source':'url','as':'release_url',
              'expected_url':'https://github.com/{{repository}}/releases/tag/{{version}}',
              'path_inputs':['repository']}
        if destination=='eventual':
            out=await hub.play_step(task,step,values)
            assert values['release_url']==expected and expected in out
            assert len(calls)==2
        else:
            with pytest.raises(RuntimeError,match='capture stopped before writing'):
                await hub.play_step(task,step,values)
            assert 'release_url' not in values
    asyncio.run(main())


@pytest.mark.parametrize('reaches_target', [False, True])
def test_saved_play_never_dispatches_append_before_expected_url(tmp_path, monkeypatch, reaches_target):
    import ghost_chat
    from tests.test_automations import PlayBrowser, finish
    monkeypatch.setattr(ghost_chat,'URL_CAPTURE_TIMEOUT',0.03 if not reaches_target else 1)
    async def main():
        browser=PlayBrowser();hub=play_hub(browser,tmp_path)
        expected='https://reports.example/pallets/flask/releases/tag/3.1.3'
        polls=0;writes=[]
        async def tabs():
            nonlocal polls
            polls+=1
            current=expected if reaches_target and polls>=2 else 'https://reports.example/pallets/flask/releases'
            return [{'id':91,'url':current}]
        hub.open_tabs=tabs
        original=hub.call
        async def call(tool,args):
            if tool=='ghost_sheet_append':
                writes.append(args)
                return True,{'added':True,'row':2,'values':args['row']}
            return await original(tool,args)
        async def approve(*args):return True
        hub.call=call;hub.wait_for_approval=approve
        item=hub.scripts.add(automations.clean({'name':'Guarded URL',
            'inputs':[{'name':'repository'},{'name':'version'}], 'steps':[
                {'do':'open','url':'https://reports.example/{{repository}}/releases','path_inputs':['repository']},
                {'do':'copy','source':'url','as':'release_url',
                 'expected_url':'https://reports.example/{{repository}}/releases/tag/{{version}}',
                 'path_inputs':['repository']},
                {'do':'append','sheet':'https://docs.google.com/spreadsheets/d/test/edit',
                 'tab':'Test','row':['{{release_url}}'],'unique':'{{release_url}}'}]}))
        task=await hub.play(item,{'repository':'pallets/flask','version':'3.1.3'})
        await finish(task)
        assert task.status==('done' if reaches_target else 'failed'),task.result
        assert len(writes)==int(reaches_target)
        if reaches_target:assert writes[0]['row']==[expected]
        else:assert 'capture stopped before writing' in task.result
    asyncio.run(main())
