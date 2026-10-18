from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files
project = Path(SPECPATH).resolve().parents[2]
sys.path.insert(0, str(project))
from pyinstaller_build import _rapidocr_files, _onnxruntime_binaries, _nvidia_runtime_binaries, ANALYSIS_EXCLUDES
datas, hidden = _rapidocr_files()
datas += collect_data_files('certifi')
binaries = _onnxruntime_binaries(True) + _nvidia_runtime_binaries()
a = Analysis([str(Path(SPECPATH) / 'worker.py')], pathex=[str(project)],
    binaries=binaries, datas=datas, hiddenimports=hidden + ['lxml.etree', 'pymupdf', 'cv2', 'PIL.Image',
    'numpy', 'onnxruntime', 'services.direct_translation', 'services.direct_translation_formats',
    'services.ocr_pipeline', 'services.office_conversion'],
    excludes=ANALYSIS_EXCLUDES + ['flask', 'sqlalchemy', 'torch', 'pandas', 'scipy', 'matplotlib', 'IPython'],
    hookspath=[str(project / 'pyinstaller_hooks')], noarchive=False)
a.binaries = [x for x in a.binaries if 'tensorrt' not in x[0].lower()]
# LibreOffice is a separate executable with its own DLL search directory. Add it
# after dependency analysis so its DLLs are not also copied into Python's root.
office = project / '.tools/libreoffice/runtime'
for file in office.rglob('*'):
    if not file.is_file():
        continue
    rel = file.relative_to(office).as_posix()
    if rel.startswith(('share/extensions/', 'share/gallery/')):
        continue
    if rel.startswith('program/resource/') and rel.split('/')[2] not in ('en-US', 'zh-CN'):
        continue
    if rel.startswith('share/registry/res/') and not file.name.endswith(('_en-US.xcd', '_zh-CN.xcd')):
        continue
    if rel.startswith('share/config/images_') and file.name != 'images_colibre.zip':
        continue
    a.datas.append(('libreoffice/' + rel, str(file), 'DATA'))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='translator-worker', console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='translator-worker', upx=False)
