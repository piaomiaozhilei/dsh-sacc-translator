"""Reproducible local-market packages. Run after the standalone worker build."""
from pathlib import Path
import base64
import hashlib
import json
import shutil
import tarfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.16'
BASE = 'http://127.0.0.1:31989'
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def build():
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    source = ROOT / '.build/worker-lean/translator-worker'
    staging = ROOT / '.build' / ('release-' + VERSION)
    if staging.exists():
        raise RuntimeError(f'Use a fresh release staging directory: {staging}')
    staging.mkdir(parents=True)
    gpu_files = [p for p in source.rglob('*') if p.is_file() and
                 ('nvidia' in p.relative_to(source).parts or 'providers_cuda' in p.name.lower())]
    gpu_archive = dist / f'translator-gpu-runtime-{VERSION}.zip'
    with zipfile.ZipFile(gpu_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in gpu_files:
            archive.write(file, file.relative_to(source).as_posix())
    with gpu_archive.open('rb') as stream:
        gpu_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    installer = (ROOT/'tools/Install-GpuRuntime.ps1').read_text(encoding='utf-8')
    (dist/f'Install-GpuRuntime-{VERSION}.ps1').write_text(installer.replace('__VERSION__', VERSION)
        .replace('__SHA256__', gpu_hash), encoding='utf-8-sig')
    write(dist/f'gpu-runtime-{VERSION}.json', {'version':VERSION,'file':gpu_archive.name,
        'sha256':gpu_hash,'compressedBytes':gpu_archive.stat().st_size,
        'installedBytes':sum(p.stat().st_size for p in gpu_files)})
    index = {'format': 1, 'title': {'en':'SACC Local Translation', 'zh':'SACC 本地翻译插件'}, 'plugins':[]}
    for edition in ('cpu','cuda'):
        name = 'dsh-sacc-translator-' + edition
        package = staging / name
        package.mkdir(parents=True, exist_ok=True)
        manifest = {'name':name, 'version':VERSION, 'description':f'SACC 原格式翻译 {edition.upper()}',
            'type':'module', 'main':'lib/host.js', 'translatorEdition':edition,
            'exports':{'.':'./lib/host.js','./client':'./lib/client.js','./package.json':'./package.json','./cordis.patch.yml':'./cordis.patch.yml'},
            'engines':{'dsh':'0.2.1-rc.2'},
            'dsh':{'bundle':{'patch':'./cordis.patch.yml'},'client':{'platform':'web','inject':[
                '@deepseek-ai/dsh-client-ui-sidebar','@deepseek-ai/dsh-client-ui-layout','@deepseek-ai/dsh-client-file-upload','@deepseek-ai/dsh-client-ui-model-selection','@deepseek-ai/dsh-client-ui-sidebar-documentpreview']}}}
        write(package/'package.json', manifest)
        (package/'lib').mkdir(exist_ok=True)
        shutil.copy2(ROOT/'src/cache.js', package/'lib/cache.js')
        shutil.copy2(ROOT/'src/host.js', package/'lib/host.js')
        shutil.copy2(ROOT/'src/prompt.js', package/'lib/prompt.js')
        (package/'lib/client.js').write_text((ROOT/'src/client.js').read_text(encoding='utf-8').replace('__STYLES__',json.dumps((ROOT/'src/panel.css').read_text(encoding='utf-8'),ensure_ascii=False)).replace('__PACKAGE__',name).replace('__EDITION__',edition.upper()),encoding='utf-8')
        # First enabled edition wins; disabling it allows the other edition to mount.
        guard = "[...ctx.loader.entries()].some((e) => ['dsh-sacc-translator-cpu', 'dsh-sacc-translator-cuda'].includes(e.options.name) && e.options.id !== 'sacc-translator-" + edition + "' && !e.disabled)"
        (package/'cordis.patch.yml').write_text(f"- insert:\n    - id: sacc-translator-{edition}\n      name: '{name}'\n      disabled: !!js \"{guard}\"\n",encoding='utf-8')
        def ignore(folder, names):
            return [n for n in names if n == 'nvidia' or 'providers_cuda' in n.lower() or 'tensorrt' in n.lower()]
        shutil.copytree(source, package/'runtime', dirs_exist_ok=True, ignore=ignore)
        target = dist / f'{name}-{VERSION}.tgz'
        def release_filter(info):
            return None if '__pycache__' in Path(info.name).parts or info.name.endswith(('.pyc', '.pyo', '.log')) else info
        with tarfile.open(target, 'w:gz', compresslevel=6) as archive:
            archive.add(package, arcname='package', filter=release_filter)
        sha512 = hashlib.sha512(); sha1 = hashlib.sha1()
        with target.open('rb') as stream:
            for chunk in iter(lambda:stream.read(8*1024*1024),b''): sha512.update(chunk); sha1.update(chunk)
        published = dict(manifest, dist={'tarball':BASE+'/'+target.name, 'shasum':sha1.hexdigest(),
            'integrity':'sha512-'+base64.b64encode(sha512.digest()).decode()})
        write(dist/(name+'.json'), {'name':name,'dist-tags':{'latest':VERSION},'versions':{VERSION:published}})
        index['plugins'].append({'name':name,'title':{'en':f'Translator {edition.upper()}','zh':f'翻译 {edition.upper()}'},
            'description':{'en':'Text and original-format documents with local OCR.','zh':'文本、原格式文档翻译及宿主语音输入。'},
            'metadata':{'keywords':['翻译','translation','OCR',edition]},
            'versions':[{'version':VERSION,'publishTimestamp':int(time.time()),'engines':manifest['engines'],
                'source':{'type':'npm','registry':BASE+'/'}}]})
        print(name, target.stat().st_size, flush=True)
    write(dist/'index.json',index)

if __name__ == '__main__': build()
