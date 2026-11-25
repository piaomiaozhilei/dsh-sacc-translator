"""Check numeric QA through the actual packaged worker protocol."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
for translated, missing in [('每200立方米砂进行检查，坡度为1%。', False), ('每2000立方米砂进行检查，坡度为1%。', True)]:
    folder = Path(tempfile.mkdtemp(prefix='parameter-test-', dir=root / '.build'))
    (folder / 'input').write_text('par 200 mètres cube de sables; la pente sera de1 %.', encoding='utf-8')
    request = {'type':'start', 'id':'parameter-test', 'files':[{'id':'input','name':'sample.txt'}],
               'upload_folder':str(folder), 'source':'auto', 'target':'zh'}
    process = subprocess.Popen([sys.argv[1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')
    process.stdin.write(json.dumps(request)+'\n'); process.stdin.flush()
    completed = None
    try:
        for line in process.stdout:
            event = json.loads(line)
            if event['type'] == 'translate':
                process.stdin.write(json.dumps({'type':'translation', 'id':event['id'],
                    'result':{u['id']: ('示例' if u.get('kind')=='filename' else translated) for u in event['units']}})+'\n')
                process.stdin.flush()
            if event['type'] == 'error':
                raise AssertionError(event)
            if event['type'] == 'complete':
                completed = event
        assert process.wait(timeout=30) == 0, process.stderr.read()
        assert completed
        warnings = completed['artifacts'][0].get('warnings', [])
        assert any(w.startswith('可能缺少参数:') for w in warnings) == missing, warnings
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    print('packaged parameter validation PASS', missing)
