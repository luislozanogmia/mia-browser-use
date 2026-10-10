"""Execute the production read-only destination observer with a small DOM model."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
SPEC = {"collection": "#records", "row": ".record", "id": ".id", "identity": ".identity",
        "fields": {"name": ".name"}, "total_count": "#count"}


def observe(case=None, spec=None):
    if not NODE:
        pytest.skip("Node is needed to execute production page helpers")
    program = r"""
const data = CASE;
const Node = {ELEMENT_NODE:1, TEXT_NODE:3};
globalThis.__ghostBuild = 'test';
globalThis.location = {href:'http://localhost:9408/records'};
globalThis.window = {getComputedStyle: el => ({display:el.hidden?'none':'block',visibility:'visible',opacity:'1'})};
function element(tag, cls, value='', attrs={}) {
  const el = {nodeType:1,tagName:tag.toUpperCase(),cls,attrs,hidden:false,isContentEditable:false,
    childNodes:[],parentElement:null,getAttribute:k => el.attrs[k] ?? null,
    hasAttribute:k => Object.hasOwn(el.attrs,k),getClientRects:()=>el.hidden?[]:[{}],
    querySelectorAll:css => {
      const all=[]; const visit=n=>{for(const child of n.childNodes) if(child.nodeType===1){all.push(child);visit(child)}};visit(el);
      if(css==='*')return all;
      if(css==='[aria-busy="true"]')return all.filter(n=>n.attrs['aria-busy']==='true');
      if(css.startsWith('#')||css.startsWith('.'))return all.filter(n=>n.cls===css);
      throw Error('invalid selector');
    }};
  if(value)el.childNodes.push({nodeType:3,textContent:value});
  return el;
}
function add(parent, child){child.parentElement=parent;parent.childNodes.push(child);return child;}
const root=element('body','body');
const collection=add(root,element('div','#records'));
const count=add(root,element(data.countTag||'span','#count',data.count??'1'));
for(let i=0;i<(data.rows??1);i++){
  const r=add(collection,element('div','.record'));
  r.hidden=!!data.hiddenRow;
  if(data.position)r.attrs['aria-posinset']=data.position;
  const id=add(r,element('span','.id',data.id??'receipt-'+i));
  const identity=add(r,element('span','.identity','operation-123'));
  const name=add(r,element(data.fieldTag||'span','.name',data.name??'Avery QA'));
  name.isContentEditable=!!data.editable;
  if(data.hiddenField)name.hidden=true;
  if(data.secretChild)add(name,element('input','.secret','PRIVATE_SECRET'));
  if(data.ambiguous)add(r,element('span','.id','receipt-extra'));
  if(data.missingField)r.childNodes=r.childNodes.filter(n=>n!==name);
}
if(data.virtualized)collection.attrs['data-virtualized']='true';
if(data.size)collection.attrs['aria-rowcount']=data.size;
if(data.busy)collection.attrs['aria-busy']='true';
if(data.missingCollection)root.childNodes=root.childNodes.filter(n=>n!==collection);
if(data.missingCount)root.childNodes=root.childNodes.filter(n=>n!==count);
globalThis.document={readyState:data.loading?'loading':'complete',querySelectorAll:css=>root.querySelectorAll(css)};
SOURCE
try {process.stdout.write(JSON.stringify({value:globalThis.__ghostPage.records(data.actor||'mia',SPEC)}));}
catch(error){process.stdout.write(JSON.stringify({error:error.message}));}
""".replace("CASE", json.dumps(case or {})).replace("SOURCE", (ROOT / "extension/ghost_page.js").read_text()).replace("SPEC", json.dumps(spec or SPEC))
    result = subprocess.run([NODE, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_complete_actual_structured_output():
    assert observe() == {"value": {"destination_url": "http://localhost:9408/records", "complete": True,
                                 "records": [{"id": "receipt-0", "identity": "operation-123", "fields": {"name": "Avery QA"}}]}}


def test_empty_complete_collection_is_valid_baseline():
    assert observe({"rows": 0, "count": "0"})["value"]["records"] == []


@pytest.mark.parametrize("case", [
    {"count": "2"}, {"count": "1 record"}, {"count": "01"}, {"count": "-1"},
    {"count": "1001"}, {"rows": 2, "count": "2", "id": "same"}, {"id": ""},
    {"missingCollection": True}, {"missingCount": True}, {"missingField": True},
    {"name": "x" * 4097}, {"hiddenRow": True}, {"hiddenField": True}, {"editable": True},
    {"fieldTag": "input"}, {"fieldTag": "textarea"}, {"fieldTag": "select"},
    {"countTag": "input"}, {"secretChild": True}, {"virtualized": True},
    {"position": "2"}, {"size": "2"}, {"busy": True}, {"loading": True}, {"ambiguous": True},
])
def test_partial_ambiguous_or_sensitive_evidence_holds(case):
    result = observe(case)
    assert "value" not in result
    assert result["error"].startswith("RECOVERY_HOLD:")
    assert "PRIVATE_SECRET" not in result["error"]


def test_model_cannot_supply_complete_flag_in_selector_spec():
    assert "error" in observe(spec={**SPEC, "complete": True})


def test_actor_is_validated():
    assert observe({"actor": "invalid actor"})["error"].startswith("INVALID_ACTOR:")


def test_background_delegates_read_only_and_handles_unavailable_helper():
    source = (ROOT / "extension/background.js").read_text()
    assert 'case "ghost_records":' in source
    function = source[source.index("async function readRecords("):source.index("async function fetchPdf(")]
    program = """
const globalThis = {__ghostPage:{records:(actor,spec)=>({actor,spec})}};
const getActiveTabId = async args=>args.tab_id;
const actorOf = args=>args.actor_id;
const runInPage = async (id,fn,args)=>{const result=fn(...args);if(result.error)throw Error(result.error);return result.value;};
FUNCTION
(async()=>{
const result=await readRecords({tab_id:7,actor_id:'mia',spec:{collection:'#records'}});
globalThis.__ghostPage={};
let error='';try{await readRecords({tab_id:7,actor_id:'mia',spec:{}})}catch(e){error=e.message;}
process.stdout.write(JSON.stringify({result,error}));
})();
""".replace("FUNCTION", function)
    result = subprocess.run([NODE, "-e", program], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    assert value["result"] == {"actor": "mia", "spec": {"collection": "#records"}}
    assert value["error"] == "RECOVERY_HOLD: record helper unavailable"
