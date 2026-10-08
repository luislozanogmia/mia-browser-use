const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('extension/background.js', 'utf8');
function harness(responses) {
  let clock = Date.now(), reads = 0, pastes = 0, payload = '';
  const waits = [], requests = [];
  const context = {
    TextDecoder, SHEET_RE: /https:\/\/docs\.google\.com\/spreadsheets\/d\/([^/]+)/,
    Date: class extends Date {static now() {return clock;}},
    setTimeout: (fn, ms) => {waits.push(ms); clock += ms; fn();},
    fetch: async (url, options) => {
      requests.push({url, options});
      const spec = responses[Math.min(reads++, responses.length - 1)];
      return new Response(spec.body || '', {status: spec.status || 200,
        headers: {'content-type': spec.type || 'text/csv', ...(spec.headers || {})}});
    },
    waitForTabLoad: async () => {},
    chrome: {tabs: {update: async () => {}}, scripting: {
      executeScript: async ({args}) => {pastes++; payload = args[0]; return [{result: {pasted: 'A2'}}];}
    }}
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('async function sheetCells('), source.indexOf('// -- Adding a row')) +
    source.slice(source.indexOf('function parseCsv('), source.indexOf('async function sheetColumns(')) +
    source.slice(source.indexOf('async function sheetAppend('), source.indexOf('async function withSheetCells(')), context);
  return {read: () => context.sheetRows('test', '3'),
    append: () => context.sheetAppend({sheet: 'https://docs.google.com/spreadsheets/d/test/edit#gid=3',
      tab_id: 9, row: ['value', 'key'], unique: 'key'}), waits, requests, reads: () => reads,
    pastes: () => pastes, payload: () => payload};
}
test('429 retries fresh credentialed CSV reads with bounded default backoff', async () => {
  const h = harness([{status:429}, {status:429}, {body:'A,B\nvalue,key'}]);
  assert.equal((await h.read())[1][1], 'key');
  assert.deepEqual(h.waits, [30000,30000]);
  assert.equal(h.reads(),3);
  assert.ok(h.requests.every(r => r.options.cache === 'no-store' && r.options.credentials === 'include'));
});
test('Retry-After seconds/date are honored; excessive waits and persistent limits hold', async () => {
  for (const headers of [{'retry-after':'40'}, {'retry-after':new Date(Date.now()+40000).toUTCString()}]) {
    const h = harness([{status:429,headers},{body:'A,B'}]);
    await h.read(); assert.ok(h.waits[0] >= 39000 && h.waits[0] <= 40000);
  }
  for (const spec of [{status:429,headers:{'retry-after':'120'}},{status:429}]) {
    const h = harness([spec]); await assert.rejects(h.read(), /HTTP 429/);
    assert.ok(h.reads() <= 3); assert.ok(h.waits.reduce((a,b)=>a+b,0) <= 60000);
  }
});
test('rate limit during post-paste verification retries reads and pastes exactly once', async () => {
  const h = harness([{body:'A,B'}, {status:429}, {body:'A,B\nvalue,key'}]);
  const result = await h.append(); assert.equal(result.added,true);
  assert.equal(h.pastes(),1); assert.equal(h.reads(),3);
  assert.equal(h.payload(), "'value\t'key");
  assert.deepEqual(h.waits,[500,2500,30000]);
});
test('exhausted post-paste verification remains uncertain without repeating the paste', async () => {
  const h = harness([{body:'A,B'},{status:429}]);
  await assert.rejects(h.append(), /HTTP 429/); assert.equal(h.pastes(),1); assert.equal(h.reads(),4);
});
test('access failure and sign-in HTML are not retried as rate limits', async () => {
  for (const spec of [{status:403},{type:'text/html',body:'sign in'}]) {
    const h = harness([spec]); await assert.rejects(h.read(), /SHEET_UNREADABLE/);
    assert.equal(h.reads(),1); assert.deepEqual(h.waits,[]);
  }
});
