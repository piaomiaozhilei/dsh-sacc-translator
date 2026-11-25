"""Exercise the real worker and file pipeline with a deterministic model fixture."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import os
import threading

ROOT=Path(__file__).resolve().parents[1]
def check(command, extension):
    with tempfile.TemporaryDirectory(prefix='translator-smoke-', ignore_cleanup_errors=True) as tmp:
        folder=Path(tmp)
        if extension=='.docx':
            from docx import Document
            document=Document();document.add_paragraph('Hello world');document.save(folder/'input')
        elif extension=='.xlsx':
            from openpyxl import Workbook
            book=Workbook();book.active['A1']='Hello world';book.active['B1']='=1+2';book.save(folder/'input')
        elif extension=='.pdf':
            import fitz
            doc=fitz.open();page=doc.new_page();page.insert_text((72,72),'Hello world');doc.save(folder/'input');doc.close()
        elif extension=='.png':
            from PIL import Image,ImageDraw,ImageFont
            image=Image.new('RGB',(800,250),'white');ImageDraw.Draw(image).text((40,70),'Hello world',fill='black',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',48));image.save(folder/'input',format='PNG')
        else:(folder/'input').write_text('Hello world',encoding='utf-8')
        env=dict(os.environ,PYTHONUTF8='1',TRANSLATOR_DEBUG='1',TRANSLATOR_EDITION=os.environ.get('TRANSLATOR_EDITION','cpu'))
        with (folder/'stderr.log').open('w',encoding='utf-8') as errors:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True,encoding='utf-8',env=env)
            watchdog=threading.Timer(120,process.kill);watchdog.start()
            process.stdin.write(json.dumps({'type':'start','id':'smoke-test-1234','files':[{'id':'input','name':'sample'+extension}],'upload_folder':str(folder),'target':'zh','source':'auto'})+'\n');process.stdin.flush()
            complete=None
            for line in process.stdout:
                event=json.loads(line)
                if event['type']=='progress':print(extension,event['artifacts'][0].get('message'),flush=True)
                if event['type']=='translate':
                    process.stdin.write(json.dumps({'type':'translation','id':event['id'],'result':{unit['id']:'你好世界' for unit in event['units']}})+'\n');process.stdin.flush()
                if event['type']=='error':raise AssertionError(event)
                if event['type']=='runtime':print(json.dumps(event),flush=True)
                if event['type']=='complete':complete=event
            code=process.wait(timeout=15);watchdog.cancel()
            assert code==0,(folder/'stderr.log').read_text(encoding='utf-8')
        assert complete, 'Missing completion'
        artifact=complete['artifacts'][0]
        assert artifact['status'] in ('completed','completed_with_warnings'), artifact
        output=folder/artifact['output']['storage_key'];assert output.stat().st_size>0
        if extension=='.docx':assert Document(output).paragraphs[0].text=='你好世界'
        if extension=='.xlsx':
            from openpyxl import load_workbook
            book=load_workbook(output);assert book.active['A1'].value=='你好世界';assert book.active['B1'].value=='=1+2';book.close()
        if extension=='.pdf':
            import fitz
            with fitz.open(output) as doc:assert doc.page_count==1
        if extension=='.png':
            from PIL import Image
            with Image.open(output) as image:image.verify()
        print(extension,'PASS',flush=True)
if __name__=='__main__':
    if '--cuda' in sys.argv:
        os.environ['TRANSLATOR_EDITION']='cuda'
    arguments=[arg for arg in sys.argv[1:] if arg!='--cuda']
    command=[arguments[0]] if arguments else [sys.executable,str(ROOT/'runtime/worker.py')]
    for extension in ['.txt','.docx','.xlsx','.pdf','.png']:check(command,extension)
