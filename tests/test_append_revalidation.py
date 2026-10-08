import asyncio
import copy
import pytest
from tests.test_build_runtime import runtime


@pytest.mark.parametrize('failure', [None, 'unknown', 'legacy', 'definition', 'payload', 'missing', 'consequential'])
def test_revised_navigation_verifies_existing_row_without_dispatch(tmp_path, failure):
    async def main():
        hub, browser, task = runtime(tmp_path)
        sheet = 'https://docs.google.com/spreadsheets/d/test/edit'
        append = {'do':'append','sheet':sheet,'tab':'Hoja 1',
                  'row':['{{company}}','{{notes}}','key:{{company}}'],'unique':'key:{{company}}'}
        item = {'name':'Research','inputs':[{'name':'company','label':'Company'}],
                'steps':[{'do':'open','url':'https://example.test/new'},
                         {'do':'copy','css':'main','as':'notes'},append]}
        j=task.build_journal
        for n in range(1,4):
            j.propose({**item,'steps':item['steps'][:n]})
            if n<3:j.record_rehearsal(n, pending_steps=[])
        pinned={'sheet':sheet,'tab':'Hoja 1','unique':'key:Microsoft',
                'row':['Microsoft','Exact notes','key:Microsoft']}
        receipt={'status':'dispatch_returned','definition':copy.deepcopy(append),'verified_append':pinned,
                 'dependencies':['company'],'sample':j.sample_identity({'company':'Microsoft'})}
        j.state['effects']['intent']=receipt
        if failure=='unknown':
            receipt['status']='uncertain';j.state['effects']['intent']=receipt
        if failure=='legacy':receipt.pop('verified_append')
        if failure=='definition':receipt['definition']['tab']='Another'
        calls=[]
        async def approve(*args):return True
        async def into(*args):pass
        async def run(task, steps, values, first, lines, dry):
            assert dry is True
            values['notes']='Changed notes' if failure=='payload' else 'Exact notes'
            lines.append('1. Open: ok')
            if failure=='consequential':lines.append('2. Send: skipped in this test')
        async def call(tool,args):
            calls.append((tool,args))
            assert tool=='ghost_sheet_append' and args['require_existing'] is True
            return (False, 'row missing') if failure=='missing' else (True,{'already':True,'added':False,'no_write':True,'row':2})
        hub.approve_urls=approve;hub.into_own_tab=into;hub.run_steps=run;hub.call=call
        before=copy.deepcopy(j.state['effects'])
        out=await hub.test_automation(task,{'automation':item,'inputs':{'company':'Microsoft'},'scope':'revalidate'})
        assert j.state['effects']==before
        if failure is None:
            assert 'executed freshly' in out
            assert len(calls)==1 and calls[0][1]['row']==pinned['row']
            assert j.state['validated_prefix']==3
            assert j.state['cases'][-1]['metadata']['no_write'] is True
            assert task.tested
        else:
            assert 'held' in out or 'hold for destination reconciliation' in out
            assert not j.state['cases'] and not task.tested
            assert len(calls)==(1 if failure=='missing' else 0)
    asyncio.run(main())

@pytest.mark.parametrize('ack', ['exact', 'different', 'duplicate'])
def test_only_exact_append_ack_pins_payload_before_future_revision(tmp_path, ack):
    async def main():
        hub, browser, task=runtime(tmp_path)
        step={'do':'append','sheet':'https://docs.google.com/spreadsheets/d/test/edit',
              'tab':'Hoja 1','row':['{{company}}','Full\nnotes','key:{{company}}'],'unique':'key:{{company}}'}
        j=task.build_journal
        item={'name':'Research','inputs':[{'name':'company','label':'Company'}],
              'steps':[{'do':'open','url':'https://example.test/'},step]}
        j.propose({**item,'steps':item['steps'][:1]});j.record_test(1);j.propose(item)
        task.build_execution={'company':'Microsoft'}
        async def tabs():return []
        async def prepare(task, step, values):return j.begin_effect({'company':'Microsoft'},'',2,step)
        async def call(tool,args):
            if tool=='ghost_tab_open':return True,{'id':123}
            if tool=='ghost_tab_close':return True,{}
            assert tool=='ghost_sheet_append'
            if ack=='duplicate':return True,{'already':True,'added':False,'row':2}
            return True,{'added':True,'row':2,'values':['Microsoft','Full notes' if ack=='exact' else 'Wrong','key:Microsoft']}
        hub.call=call;hub.open_tabs=tabs;hub.prepare_build_effect=prepare
        await hub.play_step(task,step,{'company':'Microsoft'})
        receipt=next(iter(j.state['effects'].values()))
        assert receipt['status']=='dispatch_returned'
        assert ('verified_append' in receipt)==(ack=='exact')
        if ack=='exact':assert receipt['verified_append']['row'][1]=='Full\nnotes'
    asyncio.run(main())


def completed_append(task, target='Home'):
    """A prior returned write plus full positive execution, before a new probe."""
    j = task.build_journal
    item = {'name': task.build['name'], 'inputs': [{'name': 'company', 'label': 'Company'}],
            'steps': [{'do': 'open', 'url': 'https://reports.example/'},
                      {'do': 'click', 'text': target},
                      {'do': 'append', 'sheet': 'https://docs.google.com/spreadsheets/d/test/edit',
                       'tab': 'Hoja 1', 'row': ['{{company}}'], 'unique': '{{company}}'}]}
    for n in range(1, 4):
        j.propose({**item, 'steps': item['steps'][:n]})
        j.record_test(n)
    intent = j.begin_effect({'company': 'Microsoft'}, '', 3, item['steps'][-1])
    j.effect_returned(intent)
    j.state['effects'][intent]['verified_append'] = {
        'sheet': item['steps'][-1]['sheet'], 'tab': 'Hoja 1',
        'unique': 'Microsoft', 'row': ['Microsoft']}
    j.record_case(inputs={'company': 'Microsoft'}, link='', mode='live',
                  resulting_url='https://reports.example/', passed=True, step_count=3,
                  copied={}, outcome={'url': 'https://reports.example/', 'content': 'Verified existing row'})
    return item


@pytest.mark.parametrize('target,expected,observed', [('Missing repository', 2, True),
                                                     ('Missing repository', 1, False),
                                                     ('Home', 2, False)])
def test_negative_probe_preserves_positive_evidence_and_never_appends(tmp_path, target, expected, observed):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task, target)
        j = task.build_journal
        before = copy.deepcopy(j.state)
        task.build_continuation = {'stale': True}
        result = await hub.test_automation(task, {'automation': item, 'inputs': {'company': 'Microsoft'},
                                                'scope': 'negative', 'expect_failure_step': expected})
        case = j.state['cases'][-1]
        assert case['metadata']['expected_stop'] is observed
        assert case['metadata']['no_write'] is True
        assert case['mode'] == 'rehearsal' and case['passed'] is False
        for key in ('effects', 'validated_prefix', 'rehearsed_prefix', 'failed_step', 'pending_steps'):
            assert j.state[key] == before[key]
        assert j.state['cases'][:-1] == before['cases']
        assert task.tested and task.build_execution is None and task.build_continuation is None
        assert not any(tool == 'ghost_sheet_append' for tool, _ in browser.calls)
        assert ('observed expected stop' in result) is observed
    asyncio.run(main())


@pytest.mark.parametrize('args', [{'mode': 'live'}, {'expect_failure_step': True},
                                 {'expect_failure_step': 3}, {'expect_failure_step': 0}])
def test_negative_invalid_requests_are_held_before_browser_actions(tmp_path, args):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task)
        out = await hub.test_automation(task, {'automation': item, 'inputs': {'company': 'Microsoft'},
                                              'scope': 'negative', 'expect_failure_step': 2, **args})
        assert 'held' in out and not browser.calls and not task.tested
    asyncio.run(main())


def test_readonly_rehearsal_rebuilds_after_returned_write_but_live_resend_still_holds(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task)
        j = task.build_journal
        before = copy.deepcopy(j.state['effects'])
        j.invalidate_from(1, 'Reviewer requires fresh navigation evidence')
        for n in range(1, 4):
            out = await hub.test_automation(task, {'automation': {**item, 'steps': item['steps'][:n]},
                                                  'inputs': {'company': 'Microsoft'}})
            assert 'passed' in out, out
            assert task.build_continuation is None and task.build_execution is None
        assert j.state['effects'] == before
        assert j.state['validated_prefix'] == 2 and j.state['pending_steps'] == [3]
        assert not task.tested
        calls = len(browser.calls)
        out = await hub.test_automation(task, {'automation': item, 'inputs': {'company': 'Microsoft'},
                                              'mode': 'live'})
        assert 'Hold for destination reconciliation' in out
        assert len(browser.calls) == calls
        assert not any(tool == 'ghost_sheet_append' for tool, _ in browser.calls)
        assert j.state['effects'] == before
    asyncio.run(main())


@pytest.mark.parametrize('scope', ['prefix', 'negative'])
def test_uncertain_receipt_blocks_even_readonly_probes(tmp_path, scope):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task)
        j = task.build_journal
        next(iter(j.state['effects'].values()))['status'] = 'uncertain'
        before = copy.deepcopy(j.state)
        out = await hub.test_automation(task, {'automation': item, 'inputs': {'company': 'Microsoft'},
                                              'scope': scope, 'expect_failure_step': 2})
        assert 'hold for destination reconciliation' in out
        assert not browser.calls and j.state == before
    asyncio.run(main())


@pytest.mark.parametrize('step', [{'do': 'type', 'text': 'Search reports', 'value': 'Microsoft'},
                                {'do': 'key', 'key': 'ENTER'}])
def test_returned_write_does_not_bypass_guard_for_type_or_key(tmp_path, step):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task)
        task.build_journal.invalidate_from(2, 'Changed prefix action')
        revised = {**item, 'steps': [item['steps'][0], step]}
        out = await hub.test_automation(task, {'automation': revised, 'inputs': {'company': 'Microsoft'}})
        assert 'Hold for destination reconciliation' in out
        assert not browser.calls
    asyncio.run(main())


def test_legacy_returned_append_still_holds_readonly_rebuild(tmp_path):
    async def main():
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task)
        j = task.build_journal
        next(iter(j.state['effects'].values())).pop('verified_append')
        j.invalidate_from(1, 'Rebuild navigation')
        out = await hub.test_automation(task, {'automation': {**item, 'steps': item['steps'][:1]},
                                              'inputs': {'company': 'Microsoft'}})
        assert 'Hold for destination reconciliation' in out and not browser.calls
    asyncio.run(main())


def test_live_guard_routes_verified_append_to_readonly_revalidation_after_renewal(tmp_path):
    async def main():
        from automation_build import BuildJournal
        hub, browser, task = runtime(tmp_path)
        item = completed_append(task)
        j = task.build_journal
        j.invalidate_from(1, 'Revision removes old case proof')
        for n in range(1, 4):
            j.propose({**item, 'steps': item['steps'][:n]})
            j.record_rehearsal(n, pending_steps=[3] if n == 3 else [])
        task.build_journal = BuildJournal.load(j.build_id, root=tmp_path / 'builds')
        task.build_continuation = None
        out = await hub.test_automation(task, {'automation': item, 'inputs': {'company': 'Microsoft'},
                                              'mode': 'live'})
        assert 'scope:"revalidate"' in out and 'does not need a frozen recovery observer' in out
        assert not browser.calls and not task.tested
        context = task.build_journal.context()
        assert context['effect_context']['verified_append_count'] == 1
        assert 'historical successful writes' in context['effect_context']['verified_append_notice']
        assert not context['cases']
    asyncio.run(main())
