"""Exercise production gallery event handlers with a small DOM: Run is separate from Edit."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_run_inputs_do_not_expose_editor_and_submit_current_values():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the panel renderer')
    source = (ROOT / 'extension/sidepanel.js').read_text()
    renderer = source[source.index('const openPlays ='):source.index('function togglePlays(')]
    program = r'''
const assert = require('node:assert/strict');
function element(tag) {
  return {tag, children:[], events:{}, append(...items){this.children.push(...items)},
    replaceChildren(...items){this.children=items}, addEventListener(name,fn){this.events[name]=fn}};
}
const list=element('div');
const document={activeElement:null};
const $=()=>list;
function el(tag, props={}, ...children) {
 const n=Object.assign(element(tag),props); n.append(...children.filter(c=>c!==''&&c!=null));return n;
}
function all(n=list){return [n,...n.children.filter(c=>typeof c==='object').flatMap(all)]}
const calls=[];
const chat=(action,data)=>{calls.push({action,...data});return Promise.resolve({ok:true})};
const chrome={storage:{local:{set:()=>Promise.resolve()}},runtime:{}};
const when=()=> 'now';
const confirm=()=>true;
const prompt=()=> 'Extra';
const connected=true;
let state={automations:[{id:'article',name:'Article notes',inputs:[{name:'article_url',label:'Article URL'}],
 steps:['Open {{article_url}}','Copy h1'],uses:[['article_url'],[]]}]};
''' + renderer + r'''
renderPlays();
const click=n=>n.events.click({preventDefault(){}});
click(all().find(n=>n.ariaLabel==='Play Article notes'));
assert.ok(all().some(n=>n.tag==='form'));
assert.ok(!all().some(n=>n.ariaLabel==='Name'));
assert.ok(!all().some(n=>n.tag==='ol'));
assert.ok(!all().some(n=>n.textContent==='Delete'));
assert.ok(!all().some(n=>n.textContent==='Full access'));
const field=all().find(n=>n.tag==='input');
assert.equal(field.type,'url');assert.equal(field.placeholder,'https://…');
const form=all().find(n=>n.tag==='form');
form.events.submit({preventDefault(){}});
assert.equal(calls.length,0);
assert.ok(all().some(n=>n.role==='alert'));
const updated=all().find(n=>n.tag==='input');
updated.value='https://en.wikipedia.org/wiki/Ada_Lovelace';updated.events.input();
all().find(n=>n.tag==='form').events.submit({preventDefault(){}});
assert.equal(calls[0].action,'play');assert.equal(calls[0].inputs.article_url,updated.value);
assert.ok(!all().some(n=>n.role==='alert'));
click(all().find(n=>n.ariaLabel==='Edit Article notes'));
assert.ok(all().some(n=>n.ariaLabel==='Name'));
assert.ok(all().some(n=>n.tag==='ol'));
assert.ok(all().some(n=>n.textContent==='Delete'));
assert.ok(!all().some(n=>n.tag==='form'));
// Choices keep their stored value and sheet mapping; configuration is only in Edit.
state={automations:[{id:'choices',name:'Pick campaign',inputs:[{name:'campaign',label:'Campaign',choices:['Spring','Fall']}],steps:[]}]};
playInputs.set('choices',{campaign:'Fall'});
renderPlays();click(all().find(n=>n.ariaLabel==='Play Pick campaign'));
assert.ok(all().some(n=>n.ariaLabel==='Campaign'));
assert.ok(!all().some(n=>n.ariaLabel==='Column for Campaign'));
assert.ok(!all().some(n=>n.ariaLabel==='Add to Campaign'));
assert.equal(playInputs.get('choices').campaign,'Fall');
all().find(n=>n.tag==='form').events.submit({preventDefault(){}});
assert.equal(calls.at(-1).inputs.campaign,'Fall');
click(all().find(n=>n.ariaLabel==='Edit Pick campaign'));
assert.ok(all().some(n=>n.ariaLabel==='Column for Campaign'));
assert.ok(all().some(n=>n.ariaLabel==='Add to Campaign'));
// Stop retains the existing task cancellation command.
state.automations[0].running=true;state.automations[0].task='task-1';renderPlays();
click(all().find(n=>n.ariaLabel==='Stop Pick campaign'));
assert.equal(calls.at(-1).action,'stop');assert.equal(calls.at(-1).task,'task-1');
'''
    subprocess.run([node, '-e', program], check=True, capture_output=True, text=True)
