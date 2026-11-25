"""Language heuristics must never retry or block the packaged worker."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
formula = 'K=1(1 / α) + Σ (en / λn) + 1 / αe Kcal/h m² °c' * 2
for source, translated, warning in [
    (formula, formula, False),
    ('Les commandes seront installées dans les locaux techniques.', 'The controls shall be installed in the technical rooms.', True),
]:
    folder = Path(tempfile.mkdtemp(prefix='language-test-', dir=root / '.build'))
    (folder / 'input').write_text(source, encoding='utf-8')
    request = {'type': 'start', 'id': 'language-test', 'files': [{'id': 'input', 'name': 'sample.txt'}],
               'upload_folder': str(folder), 'source': 'fr', 'target': 'zh'}
    process = subprocess.Popen([sys.argv[1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')
    attempts = {}
    def send(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    send(request)
    completed = None
    try:
        for line in process.stdout:
            event = json.loads(line)
            if event['type'] == 'translate':
                result = {}
                for unit in event['units']:
                    key = unit['id']
                    attempts[key] = attempts.get(key, 0) + 1
                    result[key] = '示例' if unit.get('kind') == 'filename' else translated
                send({'type': 'translation', 'id': event['id'], 'result': result})
            elif event['type'] == 'complete':
                completed = event['artifacts'][0]
            elif event['type'] == 'error':
                raise AssertionError(event)
        assert process.wait(timeout=30) == 0, process.stderr.read()
        assert completed
        assert completed['status'] in ('completed', 'completed_with_warnings'), completed
        output = folder / completed['output']['storage_key']
        assert translated in output.read_text(encoding='utf-8')
        assert max(attempts.values()) == 1
        assert any('可能未完整译为目标语言' in w for w in completed.get('warnings', [])) == warning
        print('packaged nonblocking language validation PASS', warning)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
