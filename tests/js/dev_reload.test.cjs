const { test } = require('node:test');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const { DevelopmentReloader, PanelQuiescence, PanelFreeze, FILES } = require('../../extension/dev_reload.js');
function harness(type='development', enabled=false) {
  let bytes='initial', time=0, safe=true, reloads=0, fail=false;
  const chrome={ management:{getSelf:async()=>({installType:type})}, storage:{local:{get:async()=>({developmentReload:enabled}),set:async value=>{enabled=value.developmentReload}}}, runtime:{getURL:f=>`chrome-extension://test/${f}`,reload:()=>reloads++}};
  const watcher=new DevelopmentReloader({chrome,crypto:webcrypto,now:()=>time,idle:()=>safe,quiesce:async()=>({valid:()=>true,release:()=>{}}),fetch:async(url, opts)=>{assert.equal(opts.cache,'no-store');assert.ok(FILES.some(f=>url.endsWith('/'+f)));if(fail)throw Error('read failed');return{ok:true,arrayBuffer:async()=>new TextEncoder().encode(bytes).buffer}}});
  return {watcher,edit:v=>bytes=v,advance:n=>time+=n,safe:v=>safe=v,fail:v=>fail=v,reloads:()=>reloads};
}
test('default off; normal and unknown install types cannot enable',async()=>{
 for(const type of ['normal','admin','unknown']) {const h=harness(type,true);await h.watcher.init();assert.equal(await h.watcher.setEnabled(true),false);await h.watcher.tick();assert.equal(h.reloads(),0)}
 const h=harness();await h.watcher.init();h.edit('change');h.advance(20000);await h.watcher.tick();assert.equal(h.reloads(),0);
});
test('explicit opt in, stable debounce and final idle gate',async()=>{
 const h=harness();await h.watcher.init();assert.equal(await h.watcher.setEnabled(true),true);h.edit('first');await h.watcher.tick();h.advance(9000);await h.watcher.tick();assert.equal(h.reloads(),0);h.edit('second');await h.watcher.tick();h.advance(11000);h.safe(false);await h.watcher.tick();assert.equal(h.reloads(),0);h.safe(true);await h.watcher.tick();assert.equal(h.reloads(),1);
});
test('failed reads discard pending change and restart debounce',async()=>{
 const h=harness('development',true);await h.watcher.init();h.edit('edited');await h.watcher.tick();h.advance(20000);h.fail(true);await h.watcher.tick();assert.equal(h.reloads(),0);assert.ok(h.watcher.status().error);h.fail(false);await h.watcher.tick();assert.equal(h.reloads(),0);h.advance(11000);await h.watcher.tick();assert.equal(h.reloads(),1);
});
test('reverted files cancel pending reload and disabling prevents reload',async()=>{
 const h=harness('development',true);await h.watcher.init();h.edit('changed');await h.watcher.tick();h.edit('initial');h.advance(20000);await h.watcher.tick();assert.equal(h.watcher.status().pending,false);h.edit('new');await h.watcher.tick();await h.watcher.setEnabled(false);h.advance(20000);await h.watcher.tick();assert.equal(h.reloads(),0);
});
test('quiescence rejects stale acknowledgements, new panels and disconnects',async()=>{
 let n=0;const q=new PanelQuiescence({nonce:()=>String(++n),timeout:1000});const sent=[];const a={postMessage:m=>sent.push(m)};q.add(a);const pending=q.acquire();q.acknowledge(a,{nonce:'old',clean:true});assert.equal(q.pending.waiting.size,1);q.add({postMessage:()=>{}});assert.equal(await pending,null);assert.equal(sent.at(-1).type,'reload-abort');
 const next=q.acquire();q.remove(a);assert.equal(await next,null);
});
test('dirty panel NACK aborts and unknown authoritative idle holds after ACK',async()=>{
 const q=new PanelQuiescence({nonce:()=> 'nonce',timeout:1000});const a={postMessage:()=>{}};q.add(a);let pending=q.acquire();q.acknowledge(a,{nonce:'nonce',clean:false});assert.equal(await pending,null);
 const h=harness('development',true);await h.watcher.init();h.edit('changed');await h.watcher.tick();h.advance(11000);let released=0;h.watcher.quiesce=async()=>{h.safe(false);return{valid:()=>true,release:()=>released++}};await h.watcher.tick();assert.equal(h.reloads(),0);assert.equal(released,1);
});
test('panel freeze preserves draft, blocks editing and restores focus on abort',()=>{
 const replies=[];let clean=false,focused=0;const doc={body:{inert:false},activeElement:{focus:()=>focused++}};
 const freeze=new PanelFreeze({document:doc,clean:()=>clean,ack:m=>replies.push(m)});
 freeze.receive({type:'reload-quiesce',nonce:'dirty'});assert.equal(replies.at(-1).clean,false);assert.equal(doc.body.inert,false);
 clean=true;freeze.receive({type:'reload-quiesce',nonce:'fresh'});assert.equal(doc.body.inert,true);assert.equal(replies.at(-1).clean,true);
 freeze.receive({type:'reload-abort',nonce:'old'});assert.equal(doc.body.inert,true);
 freeze.receive({type:'reload-abort',nonce:'fresh'});assert.equal(doc.body.inert,false);assert.equal(focused,1);
});
test('all panels must acknowledge same nonce; timeout aborts and unfreezes',async()=>{
 const q=new PanelQuiescence({nonce:()=> 'n',timeout:15});const messages=[];const a={postMessage:m=>messages.push(m)};const b={postMessage:m=>messages.push(m)};q.add(a);q.add(b);const pending=q.acquire();q.acknowledge(a,{nonce:'n',clean:true});assert.equal(q.pending.waiting.size,1);assert.equal(await pending,null);assert.equal(messages.filter(m=>m.type==='reload-abort').length,2);
});
