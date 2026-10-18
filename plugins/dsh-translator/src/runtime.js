import { readdir, mkdir, link, copyFile, readFile, writeFile, rename, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { mountGpuComponent } from './gpu-component.js';

// Native Office/OCR DLL loaders cannot reliably use pnpm's deep virtual store.
// Keep an immutable, versioned runtime outside translation history/cache.
export async function prepareRuntime(root, home, edition, version, packageRoot = root) {
  const destination=join(home, version+'-'+edition);
  const ready=join(destination,'ready.json');
  try { if (JSON.parse(await readFile(ready,'utf8')).version===version) return join(destination,'runtime'); }
  catch(error) { if(error.code!=='ENOENT')throw error; }
  await mkdir(home,{recursive:true});
  const staging=join(home,'.prepare-'+randomUUID());
  async function copy(source, target) {
    await mkdir(target,{recursive:true});
    for(const entry of await readdir(source,{withFileTypes:true})) {
      const from=join(source,entry.name), to=join(target,entry.name);
      if(entry.isDirectory())await copy(from,to);
      else if(entry.isFile()) {
        try { await link(from,to); }
        catch(error) { if(!['EXDEV','EPERM','EACCES','ENOTSUP'].includes(error.code))throw error; await copyFile(from,to); }
      } else throw new Error('翻译运行时包含不支持的文件链接');
    }
  }
  try {
    await copy(join(root,'runtime'),join(staging,'runtime'));
    if(edition==='cuda') {
      const component=dirname(createRequire(join(packageRoot,'package.json')).resolve('dsh-sacc-translator-gpu/package.json'));
      await mountGpuComponent(staging,component);
    }
    await writeFile(join(staging,'ready.json'),JSON.stringify({version,edition}));
    for(let attempt=0; ; attempt++) {
      try { await rename(staging,destination); break; }
      catch(error) {
        if(!['EEXIST','ENOTEMPTY','EPERM','EACCES'].includes(error.code))throw error;
        // A second host may finish first; Windows scanners can also briefly lock DLLs.
        try { if(JSON.parse(await readFile(ready,'utf8')).version===version)break; }
        catch(readError) { if(readError.code!=='ENOENT')throw readError; }
        if(attempt>=9)throw error;
        await new Promise(resolve=>setTimeout(resolve,500));
      }
    }
    return join(destination,'runtime');
  } finally { await rm(staging,{recursive:true,force:true,maxRetries:3,retryDelay:200}); }
}
