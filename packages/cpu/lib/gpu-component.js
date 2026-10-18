import { createRequire } from 'node:module';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, link, copyFile, rename, rm } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';

const sha256 = async path => {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
};

// The package manager installs the pinned component. No network, install scripts,
// credentials or user documents are involved in mounting its verified DLLs.
export async function mountGpuComponent(root, componentRoot) {
  componentRoot ??= dirname(createRequire(join(root, 'package.json')).resolve('dsh-sacc-translator-gpu/package.json'));
  const manifest = JSON.parse(await readFile(join(componentRoot, 'manifest.json'), 'utf8'));
  if (manifest.version !== '0.1.16' || manifest.entries.length !== 15) throw new Error('GPU 组件版本不匹配，请重新安装 CUDA 插件');
  for (const entry of manifest.entries) {
    if (!/^_internal\/(?:nvidia\/[a-z0-9_]+\/bin\/[a-z0-9_]+\.dll|onnxruntime\/capi\/onnxruntime_providers_cuda\.dll)$/i.test(entry.filename) || !/^[a-f0-9]{64}$/.test(entry.sha256)) throw new Error('GPU 组件清单无效');
    const source = join(componentRoot, 'runtime', entry.filename);
    const destination = join(root, 'runtime', entry.filename);
    if (await sha256(source) !== entry.sha256) throw new Error('GPU 组件校验失败，请重新安装 CUDA 插件');
    try { if (await sha256(destination) === entry.sha256) continue; } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await mkdir(dirname(destination), { recursive: true });
    const temporary = destination + '.' + randomUUID() + '.tmp';
    try {
      try { await link(source, temporary); }
      catch (error) { if (!['EXDEV', 'EPERM', 'EACCES', 'ENOTSUP'].includes(error.code)) throw error; await copyFile(source, temporary); }
      await rename(temporary, destination);
    } finally { await rm(temporary, { force: true }); }
  }
}
