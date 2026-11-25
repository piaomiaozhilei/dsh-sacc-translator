"""Verify OCR settings reach engines and parallel page lanes stay isolated."""
import importlib.util
from pathlib import Path
import sys
import os
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('worker', Path(__file__).resolve().parents[1] / 'runtime/worker.py')
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
params = []
sys.modules['rapidocr'] = SimpleNamespace(RapidOCR=lambda **kw: params.append(kw['params']) or SimpleNamespace())
sys.modules['onnxruntime'] = SimpleNamespace(get_available_providers=lambda: ['CPUExecutionProvider'])
w.emit = lambda value: None
w.ocr_engine(False, 'low_memory', 1)
assert params[-1]['EngineConfig.onnxruntime.use_cuda'] is False
assert params[-1]['EngineConfig.onnxruntime.intra_op_num_threads'] == 1
os.environ['TRANSLATOR_EDITION']='cuda'
sys.modules['onnxruntime'] = SimpleNamespace(get_available_providers=lambda: ['CUDAExecutionProvider'])
def gpu_engine(**kw):
    params.append(kw['params'])
    return SimpleNamespace(text_det=SimpleNamespace(session=SimpleNamespace(session=SimpleNamespace(get_providers=lambda:['CUDAExecutionProvider']))))
sys.modules['rapidocr'] = SimpleNamespace(RapidOCR=gpu_engine)
w.ocr_engine(True, 'low_memory', 2)
assert params[-1]['EngineConfig.onnxruntime.cuda_ep_cfg.arena_extend_strategy']=='kSameAsRequested'
assert params[-1]['EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search']=='HEURISTIC'
created = []
def engine(*args):
    created.append(args)
    return lambda image: SimpleNamespace(boxes=None)
w.ocr_engine = engine
w.prepare_artifacts = lambda *args: []
def process(*args, **kw):
    cb = kw['ocr_callback']
    assert kw['max_workers'] == 5 and kw['ocr_workers'] == 3
    assert cb.ocr_quality == 'high' and cb.ocr_target_longest == 2200
    cb(None, engine_key='lane1', engine_threads=2)
    cb(None, engine_key='lane2', engine_threads=2)
    cb(None, engine_key='lane1', engine_threads=2)
w.process_direct_artifacts = process
errors=[]
w.emit=lambda value: errors.append(value) if value['type']=='error' else None
w.work(dict(id='test',files=[],upload_folder='.',target='zh',concurrency=5,ocrWorkers=3,ocrQuality='high',preferGpu=False,gpuMemory='low_memory'))
assert not errors, errors
assert created == [(False,'low_memory',2)]*2, created
w.protocol.write('OCR settings and isolated page engines PASS\n')
