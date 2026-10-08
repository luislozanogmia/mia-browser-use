const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('extension/background.js', 'utf8');
function setup(before, after=before) {
  let reads=0;
  const context={SHEET_RE:/https:\/\/docs\.google\.com\/spreadsheets\/d\/([^/]+)/,
    sheetRows:async()=>{reads++;return reads===1 ? before : after;},
    chrome:new Proxy({}, {get(){throw new Error('Chrome write API reached');}})};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function sameKey('),source.indexOf('async function sheetRows('))+
    source.slice(source.indexOf('async function sheetAppend('),source.indexOf('async function withSheetCells(')),context);
  return {call:args=>context.sheetAppend({sheet:'https://docs.google.com/spreadsheets/d/test/edit',...args}),reads:()=>reads};
}
const row=['Microsoft','researched:Microsoft'];
test('existing-key verification and normal skip use the same match without Chrome writes', async()=>{
  const s=setup([['Company','Key'], row]);
  const verified=await s.call({row,unique:'researched:Microsoft',require_existing:true});
  assert.equal(verified.already,true);assert.equal(verified.no_write,true);assert.equal(verified.row,2);assert.equal(s.reads(),2);
  assert.equal((await setup([row]).call({row,unique:'researched:Microsoft'})).already,true);
});
test('missing, ambiguous, mismatched, absent key and changed sheet all hold before writes', async()=>{
  for(const [before,args,after] of [
    [[],{row,unique:'researched:Microsoft'}],
    [[row,row],{row,unique:'researched:Microsoft'}],
    [[['Other','researched:Microsoft']],{row,unique:'researched:Microsoft'}],
    [[row],{row}],
    [[row],{row,unique:'researched:Microsoft'},[row,['Changed','new key']]],
  ]) await assert.rejects(setup(before,after??before).call({...args,require_existing:true}),/DUPLICATE_CHECK_HELD/);
});
test('literal percent headlines do not break later duplicate-key lookup', async()=>{
  const data=[['Company','Headline','Key'],['Other','Growth 200% in AI','researched:Other'],
              ['Salesforce','Growth 200% in AI','researched:Salesforce']];
  const row=data[2];
  assert.equal((await setup(data).call({row,unique:'researched:Salesforce'})).row,3);
  assert.equal((await setup(data).call({row,unique:'researched:Salesforce',require_existing:true})).no_write,true);
  assert.equal((await setup([['HTTPS://WWW.EXAMPLE.COM/a%20b/?q=x']]).call({row:['ignored'],unique:'https://example.com/a b'})).already,true);
});
