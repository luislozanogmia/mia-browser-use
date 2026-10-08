"""Multiple approval cards must leave the Stop control and composer usable."""
from pathlib import Path
import html
import json
import re
import shutil
import subprocess
import pytest


def test_multiple_approvals_keep_stop_and_composer_visible(tmp_path):
    chrome=shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:pytest.skip('Chrome required for actual layout check')
    root=Path(__file__).resolve().parents[1]
    markup=(root/'extension/sidepanel.html').read_text()
    markup=re.sub(r'<script\b[^>]*>.*?</script>','',markup,flags=re.S)
    markup=re.sub(r'<link\b[^>]*rel="stylesheet"[^>]*>','',markup)
    css=(root/'extension/sidepanel.css').read_text()
    script='''
const $=id=>document.getElementById(id);
$('tasks').hidden=false;$('tasksLabel').textContent="Mia's builds";
$('taskSum').textContent='0 of 2 finished';
$('approvals').innerHTML=Array.from({length:3},()=>`<div class="approval"><div class="head">Mia needs you</div><p>Open https://docs.google.com/spreadsheets/d/a-long-test-identifier/edit?gid=0#gid=0: approve this request.</p><div class="row"><button>Approve</button><button>Reject</button></div></div>`).join('');
const rect=id=>{const r=$(id).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height};};
const proof=document.createElement('pre');proof.id='proof';proof.textContent=JSON.stringify({stop:rect('stopAll'),input:rect('input'),approvals:rect('approvals'),scrolls:$('approvals').scrollHeight>$('approvals').clientHeight,height:innerHeight});document.body.append(proof);
'''
    fixture=tmp_path/'panel.html';fixture.write_text(markup.replace('</head>',f'<style>{css}</style></head>').replace('</body>',f'<script>{script}</script></body>'))
    done=subprocess.run([chrome,'--headless=new','--disable-gpu','--no-first-run','--disable-background-networking','--window-size=360,600',f'--user-data-dir={tmp_path/"profile"}','--dump-dom',fixture.as_uri()],capture_output=True,text=True,timeout=30,check=True)
    match=re.search(r'<pre id="proof">(.*?)</pre>',done.stdout,re.S);assert match
    result=json.loads(html.unescape(match[1]))
    for key in ['stop','input']:
        assert result[key]['height']>10
        assert 0<=result[key]['top']<result[key]['bottom']<=result['height'],result
    assert result['scrolls'],result
