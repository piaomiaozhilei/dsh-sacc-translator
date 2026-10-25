import test from 'node:test';
import assert from 'node:assert/strict';
import {ScreenTranslator} from '../src/screen.js';

const segment=(id,text='Hello')=>({id,text,left:0,top:0,width:100,height:20,confidence:1});
test('screen translation retries only missing IDs and snapshots the host model',async()=>{
  const calls=[],sent=[];
  const screen=new ScreenTranslator({root:'.',prepare:async()=>'',selection:async id=>{assert.equal(id,'session');return {model:'selected'};},
    translate:async(job,units)=>{calls.push(units.map(u=>u.id));assert.equal(job.selection.model,'selected');return calls.length===1?{a:'甲'}:{b:'乙'};}});
  screen.configure({sessionId:'session',source:'en',target:'zh'});screen.send=message=>sent.push(message);
  await screen.onRecognized({id:1,segments:[segment('a'),segment('b')]});
  assert.deepEqual(calls,[['a','b'],['b']]);assert.equal(sent[0].type,'translated');
  assert.deepEqual(sent[0].segments.map(s=>s.translation),['甲','乙']);
});
test('closed screen aborts model work and never sends late translations',async()=>{
  let finish;const sent=[];
  const screen=new ScreenTranslator({selection:async()=>({}),translate:()=>new Promise(resolve=>{finish=resolve;})});
  screen.configure({});screen.send=message=>sent.push(message);
  const pending=screen.onRecognized({id:2,segments:[segment('a')]});
  await new Promise(resolve=>setImmediate(resolve));screen.disable();finish({a:'late'});
  await assert.rejects(pending,{name:'AbortError'});assert.equal(sent.length,0);
});
test('screen input limits reject duplicate IDs before contacting model',async()=>{
  const screen=new ScreenTranslator({selection:()=>{throw new Error('must not call');}});
  await assert.rejects(screen.onRecognized({id:3,segments:[segment('a'),segment('a')]}),/无效/);
});
