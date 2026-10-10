const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const source=readFileSync('extension/background.js','utf8');
const fetchPart=source.slice(source.indexOf('async function sheetCells('),source.indexOf('// -- Adding a row'));
const rowsPart=source.slice(source.indexOf('function parseCsv('),source.indexOf('async function sheetColumns('));
const readPart=source.slice(source.indexOf('async function withSheetCells('),source.indexOf('function waitForTabLoad('));
function harness({ok=true,type='text/csv',body=''}={}){
 const ctx={TextDecoder,SHEET_RE:/https:\/\/docs\.google\.com\/spreadsheets\/d\/([\w-]+)/,SHEET_CHARS:10000,fetch:async()=>new Response(body,{status:ok?200:403,headers:{'content-type':type}})};
 vm.createContext(ctx);vm.runInContext(fetchPart+rowsPart+readPart+';globalThis.api={sheetRows,withSheetCells};',ctx);return ctx.api;
}
test('successful empty CSV is an empty sheet, not an access failure',async()=>{
 const api=harness();assert.equal((await api.sheetRows('test','0')).length,0);
 const text=await api.withSheetCells({url:'https://docs.google.com/spreadsheets/d/test/edit'},'page',4000);
 assert.match(text,/Cells of the open sheet tab/);assert.doesNotMatch(text,/couldn't be fetched/);
});
test('denied and sign-in HTML exports remain unreadable',async()=>{
 for(const opts of [{ok:false},{type:'text/html',body:'sign in'}]){
  const api=harness(opts);await assert.rejects(api.sheetRows('test','0'),/SHEET_UNREADABLE/);
  assert.match(await api.withSheetCells({url:'https://docs.google.com/spreadsheets/d/test/edit'},'page',4000),/couldn't be fetched/);
 }
});
test('nonempty CSV still preserves quoted values and rows',async()=>{
 const rows=await harness({body:'Company,News\nMicrosoft,"Headline, with comma"\n'}).sheetRows('test','0');
 assert.deepEqual(JSON.parse(JSON.stringify(rows)),[['Company','News'],['Microsoft','Headline, with comma']]);
});
