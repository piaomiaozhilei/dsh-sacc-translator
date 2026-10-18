"""Small offline OCR runtime helpers shared with the SACC initialization policy."""
import ctypes
import importlib.util
import os
from pathlib import Path
import sys

_directory_handles = []
_dll_handles = []


def preload_cuda(ort):
    if os.name == 'nt' and not _directory_handles:
        if getattr(sys, 'frozen', False):
            roots = [Path(sys._MEIPASS) / 'nvidia']
            provider = Path(sys._MEIPASS) / 'onnxruntime/capi/onnxruntime_providers_cuda.dll'
            if not roots[0].is_dir() or not provider.is_file():
                raise RuntimeError('尚未安装可选 GPU 运行组件，已使用 CPU；请运行对应版本的 Install-GpuRuntime 安装脚本')
        else:
            spec = importlib.util.find_spec('nvidia')
            roots = [Path(p) for p in (spec.submodule_search_locations or [])] if spec else []
        for root in roots:
            for package in ('cuda_runtime', 'cublas', 'cufft', 'cudnn'):
                directory = root / package / 'bin'
                if not directory.is_dir():
                    continue
                _directory_handles.append(os.add_dll_directory(str(directory)))
                if package == 'cudnn':
                    for dll in directory.glob('*.dll'):
                        if dll.name.lower().startswith(('cudnn_cnn', 'cudnn_ext', 'cudnn_engines_tensor_ir')):
                            _dll_handles.append(ctypes.WinDLL(str(dll)))
    ort.preload_dlls(directory='')


def sessions(engine):
    for name in ('text_det', 'text_cls', 'text_rec'):
        wrapper = getattr(getattr(engine, name, None), 'session', None)
        session = getattr(wrapper, 'session', wrapper)
        if session is not None:
            yield name, session


def actual_providers(engine):
    return {name: session.get_providers() for name, session in sessions(engine)}


def all_cuda(providers):
    return len(providers) == 3 and all(value and value[0] == 'CUDAExecutionProvider' for value in providers.values())


def warmup_cuda(engine):
    import numpy as np
    sizes = {'text_det': (320, 320), 'text_cls': (48, 192), 'text_rec': (48, 320)}
    for name, session in sessions(engine):
        # Surface real inference failures instead of ORT silently replacing a session with CPU.
        session.disable_fallback()
        height, width = sizes[name]
        feed = {}
        for item in session.get_inputs():
            default = (1, 3, height, width)
            shape = [dim if isinstance(dim, int) and dim > 0 else default[index] for index, dim in enumerate(item.shape)]
            feed[item.name] = np.zeros(shape, dtype=np.float32)
        session.run(None, feed)


def create_engine(params):
    from rapidocr import RapidOCR
    from rapidocr.inference_engine.onnxruntime import main as backend
    original = backend.SessionOptions
    def options():
        value = original()
        value.add_session_config_entry('session.intra_op.allow_spinning', '0')
        value.add_session_config_entry('session.inter_op.allow_spinning', '0')
        return value
    backend.SessionOptions = options
    try:
        return RapidOCR(params=params)
    finally:
        backend.SessionOptions = original
