import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {translationCache} from '../src/cache.js';

test('cache counts history/uploads/previews, preserves settings and other sessions',async()=>{
  const base=await mkdtemp(join(tmpdir(),'translation-cache-'));
  const root=join(base,'sacc-translator');
  await mkdir(join(root,'job','files'),{recursive:true});
  await mkdir(join(root,'previews'),{recursive:true});
  await mkdir(join(base,'other-session'));
  await writeFile(join(base,'other-session','file'),'outside');
  await writeFile(join(root,'settings.json'),'settings');
  await writeFile(join(root,'job','job.json'),'history');
  await writeFile(join(root,'job','files','upload'),'12345');
  await writeFile(join(root,'previews','preview'),'123');
  const size=await translationCache(root);
  assert.equal(size.bytes,15);assert.equal(size.files,3);
  assert.equal((await translationCache(root,true)).bytes,15);
  assert.equal((await translationCache(root)).bytes,0);
  assert.equal(await readFile(join(root,'settings.json'),'utf8'),'settings');
  assert.equal(await readFile(join(base,'other-session','file'),'utf8'),'outside');
});

test('cache never follows directory junctions',async()=>{
  const base=await mkdtemp(join(tmpdir(),'translation-cache-link-'));
  const root=join(base,'cache'), outside=join(base,'outside');
  await mkdir(root);await mkdir(outside);await writeFile(join(outside,'keep'),'keep');
  await symlink(outside,join(root,'link'),process.platform==='win32'?'junction':'dir');
  assert.equal((await translationCache(root)).bytes,0);
  await translationCache(root,true);
  assert.equal(await readFile(join(outside,'keep'),'utf8'),'keep');
});
