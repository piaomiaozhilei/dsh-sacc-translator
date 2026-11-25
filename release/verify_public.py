"""Reject missing modules, stale versions, unexpected payloads and runtime drift."""
from pathlib import Path
import hashlib,json,re,sys,tarfile
ROOT=Path(__file__).resolve().parents[1]
out=Path(sys.argv[1]);checks=[]
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
for edition in ['cpu','cuda']:
    name='dsh-sacc-translator-'+edition;archive=out/(name+'-0.2.3.tgz')
    with tarfile.open(archive) as tar:
        files={m.name:tar.extractfile(m).read() for m in tar if m.isfile()}
    manifest=json.loads(files['package/package.json'])
    assert manifest['name']==name and manifest['version']=='0.2.3'
    assert manifest['engines']['dsh']=='>=0.2.2-rc.3'
    assert '/v0.2.3/' in manifest['dependencies']['dsh-sacc-translator-runtime']
    assert ('dsh-sacc-translator-gpu' in manifest['dependencies'])==(edition=='cuda')
    assert files['package/lib/save-translation.exe'][:2]==b'MZ'
    for path,data in files.items():
        rel=path.removeprefix('package/')
        assert rel.startswith(('lib/','native/','assets/')) or rel in ['README.md','package.json','cordis.patch.yml','screenshots.json'],rel
        expected=(ROOT/'packages'/edition/rel).read_bytes()
        assert data==expected or (not rel.endswith(('.exe','.png')) and data.replace(b'\r\n',b'\n')==expected.replace(b'\r\n',b'\n')),rel
        if rel.endswith('.js'):
            for module in re.findall(r"from ['\"]\./([^'\"]+)['\"]",data.decode()):assert 'package/lib/'+module in files,module
    for source in (ROOT/'plugins/dsh-translator/src').glob('*.js'):
        if source.name!='client.js':assert files['package/lib/'+source.name].decode().replace('\r\n','\n')==source.read_text(encoding='utf-8')
    assert b"action:'saveArtifact'" in files['package/lib/client.js']
    assert b'await response.blob()' not in files['package/lib/client.js']
    checks.append(sha(archive)+'  '+archive.name)
runtime=out/'dsh-sacc-translator-runtime-0.2.3.tgz'
expected=json.loads((ROOT/'release/runtime-0.2.3.json').read_text())['files'];actual={}
with tarfile.open(runtime) as tar:
    for m in tar:
        if m.isfile() and m.name.startswith('package/runtime/'):
            actual[m.name.removeprefix('package/runtime/')]=hashlib.sha256(tar.extractfile(m).read()).hexdigest()
assert actual==expected,'runtime content mismatch'
checks.append(sha(runtime)+'  '+runtime.name)
(out/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n',encoding='utf-8')
print('PASS: CPU/CUDA 0.2.3, complete source modules, native save component, exact runtime inventory, no document/history payload')
