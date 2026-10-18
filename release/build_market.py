"""Build marketplace packages from the audited 0.1.16 runtime, without rebuilding it."""
import argparse, gzip, hashlib, io, json, tarfile, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    'dsh-sacc-translator-cpu-0.1.16.tgz': '1c0c02cf4d999a5541435249c6b753185330c772606b97f5f04d906952798328',
    'translator-gpu-runtime-0.1.16.zip': 'eeb1827eb327cb66648bfb05c63c73234b4ca3f9bcd5bc170b6ca2ac86ab5159',
}

def digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()

def add(archive, name, stream, size, mode=0o644):
    info = tarfile.TarInfo(name); info.size = size; info.mode = mode; info.mtime = 0
    archive.addfile(info, stream)

def build(inputs, output):
    assert json.loads((ROOT/"packages/cpu/package.json").read_text())["version"] == "0.1.17", "Use tag v0.1.17 for historical full packages; current npm entry packages use npm pack packages/cpu and packages/cuda."
    output.mkdir(parents=True, exist_ok=True)
    for name, expected in INPUTS.items():
        path = inputs / name
        if not path.exists():
            inputs.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve('https://github.com/piaomiaozhilei/dsh-sacc-translator/releases/download/v0.1.16/' + name, path)
        assert digest(path) == expected, 'Input archive checksum mismatch: ' + name
    assets = []
    for edition in ('gpu', 'cpu', 'cuda'):
        version = '0.1.16' if edition == 'gpu' else '0.1.17'
        target = output / f'dsh-sacc-translator-{edition}-{version}.tgz'
        with target.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=6) as gz, tarfile.open(fileobj=gz, mode='w|') as result:
            for path in sorted((ROOT/'packages'/edition).rglob('*')):
                if path.is_file():
                    relative = path.relative_to(ROOT/'packages'/edition).as_posix()
                    assert relative in ('package.json', 'cordis.patch.yml', 'manifest.json') or relative.startswith('lib/')
                    content = path.read_text(encoding='utf8').encode('utf8')
                    add(result, 'package/'+relative, io.BytesIO(content), len(content))
            if edition == 'gpu':
                manifest = json.loads((ROOT/'packages/gpu/manifest.json').read_text())
                with zipfile.ZipFile(inputs/'translator-gpu-runtime-0.1.16.zip') as source:
                    assert set(source.namelist()) == {e['filename'] for e in manifest['entries']}
                    for entry in manifest['entries']:
                        with source.open(entry['filename']) as stream:
                            assert hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256']
                        with source.open(entry['filename']) as stream:
                            add(result, 'package/runtime/'+entry['filename'], stream, source.getinfo(entry['filename']).file_size)
            else:
                with tarfile.open(inputs/'dsh-sacc-translator-cpu-0.1.16.tgz', 'r:gz') as source:
                    for member in source:
                        if member.isfile() and member.name.startswith('package/runtime/'):
                            assert '..' not in Path(member.name).parts and '__pycache__' not in member.name
                            with source.extractfile(member) as stream: add(result, member.name, stream, member.size, member.mode)
        assets.append(target)
        print(target.name, target.stat().st_size, digest(target), flush=True)
    (output/'SHA256SUMS.txt').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in assets), encoding='utf8', newline='\n')

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--inputs', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args(); build(args.inputs, args.output)
