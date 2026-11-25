import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {createDownloads} from '../src/download.js';

test('save manager launches picker with path only and tracks progress',async()=>{
  const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();let calls=0;
  const manager=createDownloads((exe,args,options)=>{calls++;assert.ok(exe.endsWith('save-translation.exe'));assert.deepEqual(args,['unopened-source.pdf','translated.pdf']);assert.equal(options.windowsHide,true);return child;});
  const {id}=manager.start('unopened-source.pdf','translated.pdf');assert.equal(manager.get(id).status,'choosing');assert.equal(manager.busy,true);
  assert.equal(manager.start('other.pdf','other.pdf').id,id);assert.equal(calls,1);
  child.stdout.write(JSON.stringify({status:'saving',bytes:100,total:200})+'\n');assert.equal(manager.get(id).bytes,100);
  child.stdout.write(JSON.stringify({status:'saved',path:'chosen.pdf'})+'\n');child.emit('close',0);
  assert.equal(manager.get(id).path,'chosen.pdf');assert.equal(manager.busy,false);child.stdout.end();
});
test('cancelled picker and launch failure release active save',()=>{
  for(const failure of [false,true]){
    const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();
    const manager=createDownloads(()=>child);const {id}=manager.start('never-opened.pdf','translated.pdf');
    if(failure)child.emit('error',Error('ENOENT'));
    else {child.stdout.write('{"status":"cancelled"}\n');child.emit('close',0);}
    assert.equal(manager.get(id).status,failure?'failed':'cancelled');assert.equal(manager.busy,false);child.stdout.end();
  }
});
