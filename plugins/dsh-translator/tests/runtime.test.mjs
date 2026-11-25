import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {prepareRuntime} from '../src/runtime.js';
for(const dependency of [false,true])test(`runtime resolves ${dependency?'npm dependency':'offline bundled'} payload`,async()=>{
  const temp=await mkdtemp(join(tmpdir(),'translator-runtime-fixture-'));
  try{
    const root=join(temp,'plugin');await mkdir(root);
    const manifest=dependency?{translatorRuntimePackage:'fixture-runtime'}:{};
    await writeFile(join(root,'package.json'),JSON.stringify(manifest));
    const source=dependency?join(root,'node_modules/fixture-runtime'):root;
    await mkdir(join(source,'runtime'),{recursive:true});
    if(dependency)await writeFile(join(source,'package.json'),'{"name":"fixture-runtime","exports":{"./package.json":"./package.json"}}');
    await writeFile(join(source,'runtime/payload'),'synthetic runtime');
    const result=await prepareRuntime(root,join(temp,'prepared'),'cpu','test');
    assert.equal(await readFile(join(result,'payload'),'utf8'),'synthetic runtime');
    assert.equal(await prepareRuntime(root,join(temp,'prepared'),'cpu','test'),result);
  }finally{await rm(temp,{recursive:true,force:true});}
});
