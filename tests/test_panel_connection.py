"""Recovered connection evidence must not retain or resurrect an offline banner."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_production_chat_recovers_without_erasing_action_errors_or_late_status():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for production panel callback checks')
    source = (Path(__file__).resolve().parents[1] / 'extension/sidepanel.js').read_text()
    helpers = source[source.index('function setStatus('):source.index('chrome.runtime.onMessage.addListener')]
    program = r'''
const assert = require('node:assert/strict');
let connectionEpoch = 0;
const status = {textContent:'',classList:{toggle(){}}};
const timers = [], callbacks = [];
const setTimeout = fn => {timers.push(fn);return timers.length-1};
const clearTimeout = () => {};
const chrome = {runtime:{lastError:null,sendMessage(msg,fn){callbacks.push(fn)}}};
''' + helpers + r'''
(async () => {
  status.textContent = 'Mia Browser is not running. Reload the extension';
  const recovered = chat('sync'); callbacks[0]({ok:true}); await recovered;
  assert.equal(status.textContent, '');
  status.textContent = 'The selector was not found';
  const action = chat('sync'); callbacks[1]({ok:true}); await action;
  assert.equal(status.textContent, 'The selector was not found');
  // An older connection failure must not overwrite newer positive evidence.
  const old = chat('sync'), fresh = chat('sync');
  callbacks[3]({ok:true}); await fresh;
  callbacks[2]({ok:false}); await old;
  assert.equal(status.textContent, 'The selector was not found');
  const delayed = chat('play'); timers[4]();
  const timeout = await delayed;
  assert.equal(timeout.ok,false);
  assert.match(timeout.error,/acknowledgement/);
  assert.doesNotMatch(timeout.error,/not running/);
  status.textContent = 'Newer task result';
  callbacks[4]({ok:false,error:'Late failure'});
  assert.equal(status.textContent,'Newer task result');
  status.textContent = 'Not connected to the bridge'; connectionRecovered();
  assert.equal(status.textContent,'');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    subprocess.run([node, '-e', program], check=True, capture_output=True, text=True)
