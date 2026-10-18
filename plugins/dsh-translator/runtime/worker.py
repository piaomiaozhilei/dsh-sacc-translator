"""JSON-lines worker. Model credentials and network calls belong to the host."""
import contextlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import uuid
import time
from ocr_runtime import preload_cuda, create_engine, warmup_cuda, actual_providers, all_cuda

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from services.direct_translation import prepare_artifacts, process_direct_artifacts

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
protocol = sys.stdout
sys.stdout = sys.stderr  # third-party diagnostic prints must never corrupt the protocol
lock = threading.Lock()
pending = {}
rpc_condition = threading.Condition()
reader_started = False
rpc_error = None
cancelled = threading.Event()

def emit(value):
    with lock:
        protocol.write(json.dumps(value, ensure_ascii=False) + '\n')
        protocol.flush()

def receive_translations():
    global rpc_error
    try:
        while True:
            with rpc_condition:
                rpc_condition.wait_for(lambda: bool(pending))
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError('宿主连接已关闭')
            result = json.loads(line)
            with rpc_condition:
                if result.get('type') != 'translation':
                    raise RuntimeError('无效的模型响应类型')
                request = pending.pop(result.get('id'), None)
                if request is None:
                    raise RuntimeError('无效的模型响应标识')
                request['result'] = result
                request['ready'].set()
    except Exception as error:
        with rpc_condition:
            rpc_error = error
            for request in pending.values():
                request['result'] = {'error': str(error)}
                request['ready'].set()
            pending.clear()


def translate(units, **kwargs):
    global reader_started
    request_id = uuid.uuid4().hex
    request = {'ready': threading.Event()}
    with rpc_condition:
        if rpc_error is not None:
            raise rpc_error
        pending[request_id] = request
        # Start only once parsing has reached model requests. Between batches
        # wait on a Condition, never on stdin during native library initialization.
        if not reader_started:
            threading.Thread(target=receive_translations, daemon=True).start()
            reader_started = True
        rpc_condition.notify()
    emit({'type': 'translate', 'id': request_id, 'units': units, 'scheduled': True})
    request['ready'].wait()
    result = request['result']
    if result.get('error'):
        raise RuntimeError(result['error'])
    return result['result']

def ocr_engine(prefer_gpu=True, gpu_memory="performance", engine_threads=2):
    started = time.perf_counter()
    import onnxruntime as ort
    gpu = prefer_gpu and os.environ.get('TRANSLATOR_EDITION') == 'cuda'
    warning = None
    if gpu:
        try:
            if hasattr(ort, 'preload_dlls'):
                preload_cuda(ort)
            if 'CUDAExecutionProvider' not in ort.get_available_providers():
                raise RuntimeError('CUDAExecutionProvider 不可用')
        except Exception as error:
            gpu = False
            warning = str(error)
    def create(enabled):
        params = {'EngineConfig.onnxruntime.use_cuda': enabled,
            'EngineConfig.onnxruntime.intra_op_num_threads': engine_threads,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1}
        if enabled and gpu_memory == 'low_memory':
            params.update({'EngineConfig.onnxruntime.cuda_ep_cfg.arena_extend_strategy': 'kSameAsRequested',
                'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search': 'HEURISTIC'})
        return create_engine(params)
    try:
        engine = create(gpu)
        if gpu:
            warmup_cuda(engine)
    except Exception as error:
        if not gpu:
            raise
        warning = str(error)
        gpu = False
        engine = create(False)
    # Check actual sessions, not merely the advertised provider list.
    providers = actual_providers(engine)
    actual_gpu = gpu and all_cuda(providers)
    if gpu and not actual_gpu:
        warning = 'CUDA 未实际启用，已回退 CPU'
        engine = create(False)
        providers = actual_providers(engine)
    status = {'type': 'runtime', 'requested': 'cuda' if prefer_gpu and os.environ.get('TRANSLATOR_EDITION') == 'cuda' else 'cpu',
          'gpuMemory': gpu_memory, 'engineThreads': engine_threads,
          'actual': 'cuda' if actual_gpu else 'cpu', 'providers': providers, 'warning': warning,
          'verifiedInference': actual_gpu, 'initializationSeconds': round(time.perf_counter()-started, 3)}
    engine._translator_runtime = status
    emit(status)
    return engine

def work(request):
    try:
        folder = request['upload_folder']
        artifacts = prepare_artifacts(request['files'], request['id'], request['target'], folder)
        engines = {}
        engine_lock = threading.Lock()
        def ocr(image, engine_key='default', engine_threads=1, **kwargs):
            # Each parallel PDF lane owns its own non-thread-safe OCR engine.
            with engine_lock:
                if engine_key not in engines:
                    engines[engine_key] = ocr_engine(request.get('preferGpu', True), request.get('gpuMemory', 'performance'), engine_threads)
                engine = engines[engine_key]
            started = time.perf_counter()
            try:
                result = engine(image)
            except Exception as error:
                if engine._translator_runtime['actual'] != 'cuda':
                    raise
                with engine_lock:
                    engine = ocr_engine(False, request.get('gpuMemory', 'performance'), engine_threads)
                    engines[engine_key] = engine
                engine._translator_runtime.update(requested='cuda', warning=f'CUDA 实际推理失败，已回退 CPU：{error}')
                result = engine(image)
            status = {**engine._translator_runtime, 'providers': actual_providers(engine),
                      'lastOcrSeconds': round(time.perf_counter()-started, 3)}
            status['actual'] = 'cuda' if all_cuda(status['providers']) else 'cpu'
            ocr.runtime_status = status
            emit(status)
            if result.boxes is None:
                return []
            return [(box.tolist(), text, float(score)) for box, text, score in zip(result.boxes, result.txts, result.scores)]
        targets = {'ultra_low': 1200, 'low': 1400, 'medium': 1600, 'high': 2200}
        ocr.ocr_quality = request.get('ocrQuality', 'medium')
        ocr.ocr_target_longest = targets[ocr.ocr_quality]
        process_direct_artifacts(artifacts, folder, translate,
            lambda current: emit({'type': 'progress', 'artifacts': current}), cancelled.is_set,
            request.get('source', 'auto'), request['target'], max_workers=max(1, min(8, int(request.get('concurrency', 1)))), ocr_workers=max(1, min(3, int(request.get('ocrWorkers', 1)))), ocr_callback=ocr)
        emit({'type': 'complete', 'artifacts': artifacts})
    except Exception as error:
        emit({'type': 'error', 'error': str(error)})

def main():
    if os.environ.get('TRANSLATOR_DEBUG') == '1':
        import faulthandler
        faulthandler.dump_traceback_later(40)
    first = sys.stdin.readline()
    if not first:
        return
    request = json.loads(first)
    if request.get('type') == 'spreadsheet-preview':
        try:
            from spreadsheet_preview import prepare_preview
            emit({'type':'complete','path':prepare_preview(request['source'],request['destination'])})
        except Exception as error:
            emit({'type':'error','error':str(error)})
        return
    if request.get('type') == 'diagnose':
        ocr_engine()
        return
    # NumPy/ORT native initialization must happen on the main thread on Windows.
    work(request)

if __name__ == '__main__':
    main()
