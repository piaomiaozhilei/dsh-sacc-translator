"""Reconstruct an already-tested GPU asset from pinned official wheels; require byte identity."""
import hashlib,json,os,pathlib,subprocess,sys,urllib.request,zipfile
root=pathlib.Path(__file__).resolve().parent
manifest=json.loads((root/'gpu-manifest.json').read_text())
wheels=root/'wheels';wheels.mkdir(exist_ok=True)
subprocess.run([sys.executable,'-m','pip','download','--only-binary=:all:','--no-deps',
 '--platform','win_amd64','--python-version','314','--implementation','cp','--abi','cp314',
 '--index-url','https://pypi.org/simple','--dest',str(wheels),*manifest['packages']],check=True)
archives=[zipfile.ZipFile(p) for p in wheels.glob('*.whl')]
output=root/manifest['archive']
with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as result:
 for entry in manifest['entries']:
  name=entry['filename'].removeprefix('_internal/')
  candidates=[(a,n) for a in archives for n in a.namelist() if n==name]
  assert len(candidates)==1, 'Missing/ambiguous runtime file: '+name
  data=candidates[0][0].read(candidates[0][1])
  assert hashlib.sha256(data).hexdigest()==entry['sha256'], 'Dependency content differs: '+name
  info=zipfile.ZipInfo(entry['filename'],tuple(entry['date_time']))
  for key in ('compress_type','external_attr','internal_attr','create_system','create_version','extract_version','flag_bits'):setattr(info,key,entry[key])
  result.writestr(info,data,compress_type=entry['compress_type'],compresslevel=6)
for a in archives:a.close()
with output.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
assert actual==manifest['sha256'], 'Reconstructed ZIP differs; do not upload: '+actual
print('Exact archive SHA-256 matched',flush=True)
repo='piaomiaozhilei/dsh-sacc-translator'
headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
def get(path):
 with urllib.request.urlopen(urllib.request.Request('https://api.github.com/repos/'+repo+path,headers=headers),timeout=60) as r:return json.load(r)
releases=get('/releases'); release=next(r for r in releases if r['tag_name']=='v0.1.16')
assert release['draft'], 'Only upload to the existing draft'
existing=next((a for a in release['assets'] if a['name']==output.name),None)
if existing:
 assert existing.get('digest')=='sha256:'+actual
 print('Identical asset already uploaded')
else:
 import http.client,urllib.parse
 url=urllib.parse.urlsplit(release['upload_url'].split('{')[0]+'?name='+output.name)
 connection=http.client.HTTPSConnection(url.hostname,timeout=1200)
 connection.putrequest('POST',url.path+'?'+url.query)
 connection.putheader('Authorization','Bearer '+os.environ['GH_TOKEN'])
 connection.putheader('Accept','application/vnd.github+json')
 connection.putheader('Content-Type','application/octet-stream')
 connection.putheader('Content-Length',str(output.stat().st_size))
 connection.putheader('User-Agent','verified-gpu-release')
 connection.endheaders()
 with output.open('rb') as stream:
  while chunk:=stream.read(2*1024*1024):connection.send(chunk)
 response=connection.getresponse()
 payload=response.read()
 assert response.status==201, 'GitHub upload failed: HTTP '+str(response.status)
 asset=json.loads(payload)
 connection.close()
 assert asset.get('digest')=='sha256:'+actual
 print('Uploaded verified GPU component',flush=True)
