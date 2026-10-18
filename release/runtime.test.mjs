import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,readdir,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {prepareRuntime} from '../packages/cpu/lib/runtime.js';

test('short runtime is reusable; failed CUDA preparation never publishes a ready directory',async()=>{
  const base=await mkdtemp(join(tmpdir(),'translator-runtime-'));
  try {
    const source=join(base,'package'), home=join(base,'short');
    await mkdir(join(source,'runtime'),{recursive:true});
    await writeFile(join(source,'package.json'),'{}');
    await writeFile(join(source,'runtime','translator-worker.exe'),'synthetic worker');
    const result=await prepareRuntime(source,home,'cpu','fixture');
    assert.equal(await readFile(join(result,'translator-worker.exe'),'utf8'),'synthetic worker');
    assert.equal(await prepareRuntime(source,home,'cpu','fixture'),result);
    await assert.rejects(prepareRuntime(source,home,'cuda','fixture'),/Cannot find module/);
    assert.deepEqual(await readdir(home),['fixture-cpu']);
    assert.deepEqual(await readdir(join(source,'runtime')),['translator-worker.exe']);
  } finally { await rm(base,{recursive:true,force:true}); }
});
