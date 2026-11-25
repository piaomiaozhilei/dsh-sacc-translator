import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const source=(await readFile(new URL('../src/client.js',import.meta.url),'utf8')).replace('return {inject:', 'return {saveArtifact,inject:');
function load(window,fetch){
 let plugin;const React={Component:class {}};
 window.__ModuleLoader__={load:({factory})=>plugin=factory(()=>React)};
 vm.runInNewContext(source,{window,fetch,__STYLES__:'',setTimeout:fn=>fn()});return plugin.saveArtifact;
}
test('desktop requests save dialog without fetching any document bytes',async()=>{
 const actions=[],messages=[];let n=0;
 const run=load({dshDesktop:{}},async(url,init)=>{
  assert.equal(url,'/api/sacc-translator');const body=JSON.parse(init.body);actions.push(body.action);
  return {ok:true,json:async()=>body.action==='saveArtifact'?{id:'s'}:[{status:'choosing'},{status:'saving',bytes:1048576,total:2097152},{status:'saved',path:'chosen.pdf'}][n++]};
 });
 const result=await run({id:'j'},{artifact_id:'a',name:'test.pdf'},m=>messages.push(m));
 assert.equal(result.path,'chosen.pdf');assert.equal(actions[0],'saveArtifact');assert.ok(messages.some(m=>m.includes('1.0 / 2.0 MB')));
});
test('desktop cancelled picker returns cancelled',async()=>{
 const run=load({dshDesktop:{}},async(url,init)=>({ok:true,json:async()=>JSON.parse(init.body).action==='saveArtifact'?{id:'s'}:{status:'cancelled'}}));
 assert.equal((await run({id:'j'},{artifact_id:'a'})).status,'cancelled');
});
test('browser cancellation performs no fetch',async()=>{
 const run=load({showSaveFilePicker:async()=>{throw Object.assign(Error('cancel'),{name:'AbortError'});}},()=>{throw Error('must not read');});
 assert.equal((await run({id:'j'},{artifact_id:'a'})).status,'cancelled');
});
test('browser waits for chosen destination before streaming, never calls blob',async()=>{
 const events=[];const output={};
 const run=load({showSaveFilePicker:async()=>{events.push('pick');return {createWritable:async()=>{events.push('writable');return output;}};}},async()=>{events.push('fetch');return {ok:true,body:{pipeTo:async target=>{assert.equal(target,output);events.push('pipe');}}};});
 assert.equal((await run({id:'j'},{artifact_id:'a',name:'test.pdf'})).status,'saved');assert.deepEqual(events,['pick','fetch','writable','pipe']);
});
test('unsupported environment errors before reading',async()=>{
 const run=load({},()=>{throw Error('must not read');});await assert.rejects(run({id:'j'},{artifact_id:'a'}),/不支持选择保存位置/);
});
