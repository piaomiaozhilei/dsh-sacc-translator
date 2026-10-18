import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { mountGpuComponent } from '../packages/cuda/lib/gpu-component.js';

test('mounts fixed component, repeats safely, rejects corruption and path traversal', async () => {
  const base = await mkdtemp(join(tmpdir(), 'translator-component-'));
  try {
    const root=join(base,'plugin'), component=join(base,'component');
    const bytes=Buffer.from('synthetic DLL fixture');
    const entries=Array.from({length:15}, (_,i)=>({filename:`_internal/nvidia/cudnn/bin/test_${i}.dll`,sha256:createHash('sha256').update(bytes).digest('hex')}));
    for (const entry of entries) { const path=join(component,'runtime',entry.filename); await mkdir(join(path,'..'),{recursive:true}); await writeFile(path,bytes); }
    const save=()=>writeFile(join(component,'manifest.json'),JSON.stringify({version:'0.1.16',entries}));
    await save(); await mountGpuComponent(root,component); await mountGpuComponent(root,component);
    assert.deepEqual(await readFile(join(root,'runtime',entries[0].filename)),bytes);
    entries[0].sha256='0'.repeat(64); await save();
    await assert.rejects(mountGpuComponent(root,component),/校验失败/);
    entries[0].filename='../escape.dll'; await save();
    await assert.rejects(mountGpuComponent(root,component),/清单无效/);
  } finally { await rm(base,{recursive:true,force:true}); }
});
