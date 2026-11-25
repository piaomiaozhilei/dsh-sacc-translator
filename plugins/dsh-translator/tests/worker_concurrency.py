"""End-to-end barrier test: five requests must arrive before any reply is sent."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from docx import Document

root = Path(__file__).resolve().parents[1]
folder = root / '.build' / ('concurrency-check-' + uuid.uuid4().hex)
folder.mkdir(parents=True)
doc = Document()
for i in range(480):
    doc.add_paragraph(f'Paragraph {i}: This is a unique document segment for concurrent translation testing.')
doc.save(folder / 'input')
command = [sys.argv[1]] if len(sys.argv) > 1 else [sys.executable, str(root / 'runtime/worker.py')]
events = queue.Queue()
with (folder / 'stderr.log').open('w', encoding='utf-8') as errors:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8')
    threading.Thread(target=lambda: [events.put(json.loads(line)) for line in process.stdout], daemon=True).start()
    def send(message):
        process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        process.stdin.flush()
    def reply(event):
        send({'type':'translation', 'id':event['id'], 'result':{unit['id']:'译文 ' + unit['text'] for unit in event['units']}})
    try:
        send({'type':'start','id':'concurrency-test','files':[{'id':'input','name':'sample.docx'}],
              'upload_folder':str(folder),'source':'auto','target':'zh','concurrency':5})
        waiting = []
        requests = 0
        released = False
        saw_five = False
        complete = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            event = events.get(timeout=max(.1, deadline-time.monotonic()))
            if event['type'] == 'error':
                raise AssertionError(event)
            if event['type'] == 'progress':
                state = event['artifacts'][0].get('concurrency') or {}
                if state.get('active') == 5 and state.get('limit') == 5:
                    saw_five = True
            if event['type'] == 'translate':
                requests += 1
                if not released:
                    waiting.append(event)
                else:
                    reply(event)
            if not released and len(waiting) == 5 and saw_five:
                # Deliberately out of order to verify ID-based demultiplexing.
                for item in reversed(waiting):
                    reply(item)
                released = True
                print('PASS: five outstanding requests; progress reports actual 5/5; reverse-order replies', flush=True)
            if event['type'] == 'complete':
                complete = event
                break
        assert released, 'Worker never reached five concurrent requests'
        assert requests > 5, requests
        assert complete, 'Missing completion'
        assert process.wait(timeout=10) == 0
        artifact = complete['artifacts'][0]
        assert artifact['status'] in ('completed','completed_with_warnings'), artifact
        output = Document(folder / artifact['output']['storage_key'])
        assert len(output.paragraphs) == 480
        for i, paragraph in enumerate(output.paragraphs):
            assert paragraph.text.startswith(f'译文 Paragraph {i}:'), (i, paragraph.text)
        print(f'PASS: {requests} batches, all 480 paragraphs matched their original IDs', flush=True)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
