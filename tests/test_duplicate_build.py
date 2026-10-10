import asyncio
from tests.test_build_runtime import runtime


def test_duplicate_probe_requires_verified_sample_and_records_only_append_check(tmp_path):
    async def main():
        hub,browser,task=runtime(tmp_path)
        hub.wait_for_approval=lambda *a: None
        item={'name':'Research','about':'Research a company','inputs':[{'name':'company','label':'Company'}],
              'steps':[{'do':'open','url':'https://example.test/'},
                       {'do':'append','sheet':'https://docs.google.com/spreadsheets/d/test/edit','tab':'Hoja 1',
                        'row':['{{company}}','researched:{{company}}'],'unique':'researched:{{company}}'}]}
        journal=task.build_journal
        journal.propose({**item,'steps':item['steps'][:1]});journal.record_test(1);journal.propose(item);journal.record_test(2)
        args={'automation':item,'inputs':{'company':'Microsoft'},'scope':'duplicate'}
        assert 'prior completed live proof' in await hub.test_automation(task,args)
        journal.record_case({'company':'Microsoft'},'','live','https://example.test/',True,2,{},
                            outcome={'url':'https://example.test/','content':'Verified row'})
        calls=[]
        async def call(tool,values):
            calls.append((tool,values))
            return True,{'no_write':True,'added':False,'already':True,'row':2,'existing':['Microsoft','researched:Microsoft']}
        hub.call=call
        out=await hub.test_automation(task,args)
        assert 'no paste or new row' in out
        assert len(calls)==1 and calls[0][0]=='ghost_sheet_append'
        assert calls[0][1]['require_existing'] is True
        assert calls[0][1]['row']==['Microsoft','researched:Microsoft']
        assert not journal.state['effects']
        assert journal.state['cases'][-1]['metadata']['executed_steps']==[2]
        assert journal.state['cases'][-1]['metadata']['retained_steps']==[1]
        assert 'prior completed live proof' in await hub.test_automation(task,{**args,'inputs':{'company':'NVIDIA'}})
    asyncio.run(main())
