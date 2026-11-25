"""Rebuild the 0.2.3 runtime from pinned public bytes and the verified overlay."""
from pathlib import Path
import argparse, hashlib, io, json, tarfile, gzip, urllib.request
ROOT=Path(__file__).resolve().parents[1]
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def build(base,out):
    spec=json.loads((ROOT/'release/runtime-0.2.3.json').read_text())
    if not base.exists():
        base.parent.mkdir(parents=True,exist_ok=True);urllib.request.urlretrieve(spec['baseUrl'],base)
    assert sha(base)==spec['baseSha256'],'base checksum mismatch'
    out.mkdir(parents=True,exist_ok=True)
    target=out/'dsh-sacc-translator-runtime-0.2.3.tgz'
    with tarfile.open(base) as old,target.open('wb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as gz,tarfile.open(fileobj=gz,mode='w') as tar:
        def add(name,data):
            info=tarfile.TarInfo('package/'+name);info.size=len(data);info.mode=0o755 if name.endswith('.exe') else 0o644;tar.addfile(info,io.BytesIO(data))
        add('package.json',json.dumps({'name':'dsh-sacc-translator-runtime','version':'0.2.3','private':True,'exports':{'./package.json':'./package.json'}},sort_keys=True).encode())
        remaining=dict(spec['files'])
        def emit(name,data):
            digest=remaining.pop(name)
            assert hashlib.sha256(data).hexdigest()==digest,name
            assert '/nvidia/' not in '/'+name and 'providers_cuda' not in name.lower(),name
            add('runtime/'+name,data)
        # Preserve archive order: random seeks in gzip repeatedly decompress the base.
        for member in old:
            name=member.name.removeprefix('package/runtime/')
            if not member.isfile() or name not in remaining:continue
            overlay=ROOT/'release/runtime-overlay'/name
            emit(name,overlay.read_bytes() if overlay.exists() else old.extractfile(member).read())
        for name in sorted(remaining):emit(name,(ROOT/'release/runtime-overlay'/name).read_bytes())
    print(target,sha(target),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();build(a.base,a.out)
