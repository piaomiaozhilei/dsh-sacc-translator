"""Run every supported extension through a real release worker, with free model fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile
from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pptx.util import Inches
from PIL import Image, ImageDraw, ImageFont
import pymupdf

worker = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
root.mkdir(parents=True, exist_ok=False)
scratch = root/'tmp'; scratch.mkdir()
env = {**os.environ, 'TEMP':str(scratch), 'TMP':str(scratch), 'PYTHONUTF8':'1',
       'TRANSLATOR_EDITION':os.environ.get('TRANSLATOR_EDITION', 'cpu')}
office = worker.parent/'_internal/libreoffice/program/soffice.com'
def convert(path, ext, folder):
    folder.mkdir(exist_ok=True)
    profile = folder/'profile'
    result = subprocess.run([str(office), '-env:UserInstallation='+profile.as_uri(), '--headless',
        '--convert-to', ext, '--outdir', str(folder), str(path)], capture_output=True, env=env, timeout=120)
    output = folder/(path.stem+'.'+ext)
    assert result.returncode==0 and output.is_file(), (result.stdout, result.stderr)
    return output

doc = Document(); doc.add_paragraph('Hello world'); doc.add_table(1,2).cell(0,0).text='Hello world'
doc.save(root/'sample.docx')
book=Workbook(); book.active['A1']='Hello world'; book.active['B1']='=1+2'; book.save(root/'sample.xlsx')
deck=Presentation(); slide=deck.slides.add_slide(deck.slide_layouts[6])
slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text='Hello world'
deck.save(root/'sample.pptx')
for modern, legacy in [('docx','doc'),('xlsx','xls'),('pptx','ppt')]:
    convert(root/('sample.'+modern),legacy,root)
for ext in ['txt','md','csv','html','htm']:
    text = '<p>Hello world</p>' if ext in ('html','htm') else 'Hello world'
    (root/('sample.'+ext)).write_text(text,encoding='utf-8')
with pymupdf.open() as pdf:
    page=pdf.new_page(); page.insert_text((72,72),'Hello world'); pdf.save(root/'sample.pdf')
image=Image.new('RGB',(800,250),'white')
ImageDraw.Draw(image).text((40,70),'Hello world',fill='black',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',48))
for ext in ['png','jpg','jpeg','bmp','gif','webp']: image.save(root/('sample.'+ext))
outputs={}
for path in sorted(root.glob('sample.*')):
    with (root/(path.suffix[1:]+'.log')).open('w',encoding='utf-8') as log:
        child=subprocess.Popen([str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,
            text=True,encoding='utf-8',env=env)
        timer=threading.Timer(180,child.kill); timer.start()
        result=None; calls=0
        try:
            child.stdin.write(json.dumps({'type':'start','id':'release-'+path.suffix[1:],
                'upload_folder':str(root),'files':[{'id':path.name,'name':path.name}],
                'source':'en','target':'zh','preferGpu':True})+'\n'); child.stdin.flush()
            for line in child.stdout:
                event=json.loads(line)
                if event['type']=='translate':
                    calls+=1
                    child.stdin.write(json.dumps({'type':'translation','id':event['id'],
                        'result':{unit['id']:'你好世界' for unit in event['units']}})+'\n'); child.stdin.flush()
                if event['type']=='error': raise AssertionError(event)
                if event['type']=='runtime': print(json.dumps(event,ensure_ascii=False),flush=True)
                if event['type']=='complete': result=event['artifacts'][0]
            assert child.wait(timeout=10)==0 and result and calls>0, (path,result)
        finally:
            timer.cancel()
            if child.poll() is None: child.kill(); child.wait()
        assert result['status'] in ('completed','completed_with_warnings'),result
        output=root/result['output']['storage_key']; assert output.stat().st_size>0
        outputs[path.suffix]=output
        if path.suffix in ('.doc','.xls','.ppt'):
            ext={'.doc':'docx','.xls':'xlsx','.ppt':'pptx'}[path.suffix]
            output=convert(output,ext,root/('reopen-'+ext))
        ext=output.suffix
        if ext=='.docx': assert '你好世界' in Document(output).paragraphs[0].text
        elif ext=='.xlsx':
            reopened=load_workbook(output); assert reopened.active['A1'].value=='你好世界'
            assert reopened.active['B1'].value=='=1+2'; reopened.close()
        elif ext=='.pptx': assert '你好世界' in Presentation(output).slides[0].shapes[0].text
        elif ext=='.pdf':
            with pymupdf.open(output) as pdf: assert len(pdf)==1
        elif ext in ('.png','.jpg','.jpeg','.bmp','.gif','.webp'):
            with Image.open(output) as img: img.verify()
        else: assert '你好世界' in output.read_text(encoding='utf-8-sig')
        print(path.suffix+' PASS',flush=True)
assert len(outputs)==18, len(outputs)
print('All 18 extensions PASS',flush=True)
