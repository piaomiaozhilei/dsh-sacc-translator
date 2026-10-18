import {readdir, lstat, unlink, rmdir} from 'node:fs/promises';
import {resolve, relative, isAbsolute, join} from 'node:path';

// Only the plugin's dedicated data directory is eligible. Never traverse links.
export async function translationCache(root, clear=false) {
  root=resolve(root);
  if ((await lstat(root)).isSymbolicLink()) throw new Error('翻译缓存目录不能是链接');
  let bytes=0, files=0;
  async function visit(path) {
    const rel=relative(root,resolve(path));
    if (!rel || rel.startsWith('..') || isAbsolute(rel)) throw new Error('缓存路径越界');
    const info=await lstat(path);
    if (info.isSymbolicLink()) { if(clear)await unlink(path); return; }
    if (info.isDirectory()) {
      for(const name of await readdir(path)) await visit(join(path,name));
      if(clear)await rmdir(path);
    } else {
      bytes+=info.size; files++;
      if(clear)await unlink(path);
    }
  }
  for(const name of await readdir(root)) {
    if(name==='settings.json'||name==='settings.json.tmp')continue;
    await visit(join(root,name));
  }
  return {bytes,files,measuredAt:new Date().toISOString()};
}
