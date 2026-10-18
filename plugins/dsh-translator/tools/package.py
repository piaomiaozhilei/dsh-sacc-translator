"""Build the marketplace release from the SHA-256-pinned audited runtime."""
from pathlib import Path
import runpy
import sys
root = Path(__file__).resolve().parents[3]
sys.argv = [str(root / 'release/build_market.py'), '--inputs', str(root / '.build/inputs'), '--output', str(root / 'plugins/dsh-translator/dist')]
runpy.run_path(sys.argv[0], run_name='__main__')
