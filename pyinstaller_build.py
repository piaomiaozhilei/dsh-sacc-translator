from __future__ import annotations

import importlib.util

import os

from pathlib import Path

from typing import Callable, Iterable

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

RAPIDOCR_UNUSED_BACKENDS = (
    "rapidocr.inference_engine.mnn",
    "rapidocr.inference_engine.openvino",
    "rapidocr.inference_engine.paddle",
    "rapidocr.inference_engine.pytorch",
    "rapidocr.inference_engine.tensorrt",
)

ANALYSIS_EXCLUDES = [
    "MNN",
    "easyocr",
    "openvino",
    "paddle",
    "rapidocr_onnxruntime",
    "tensorrt",
    "torch",
    "torchvision",
    "onnxruntime.backend",
    "onnxruntime.datasets",
    "onnxruntime.quantization",
    "onnxruntime.tools",
    "onnxruntime.transformers",
    *RAPIDOCR_UNUSED_BACKENDS,
]

def _rapidocr_module_filter(module_name: str) -> bool:
    return not any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in RAPIDOCR_UNUSED_BACKENDS
    )

def _rapidocr_files():
    datas = collect_data_files(
        "rapidocr",
        includes=["models/*", "*.yaml"],
    )
    hiddenimports = collect_submodules("rapidocr", filter=_rapidocr_module_filter)
    return datas, hiddenimports

def _onnxruntime_binaries(include_cuda: bool):
    binaries = []
    for source, destination in collect_dynamic_libs("onnxruntime"):
        filename = os.path.basename(source).lower()
        if "tensorrt" in filename:
            continue
        if not include_cuda and "providers_cuda" in filename:
            continue
        binaries.append((source, destination))
    return binaries

def _nvidia_runtime_binaries():
    """Collect CUDA/cuDNN DLLs required by ONNX Runtime at execution time.

    ORT's public preload list covers the directly linked libraries, but recent
    cuDNN 9 wheels load a few small sublibraries dynamically on the first Conv.
    Those files must be included too or session creation reports CUDA while the
    first real inference silently falls back to CPU.
    """
    try:
        import onnxruntime as ort
    except Exception as exc:  # pragma: no cover - executed by PyInstaller
        raise RuntimeError("The full edition requires onnxruntime-gpu") from exc

    try:
        from onnxruntime.capi import build_and_package_info

        cuda_version = str(build_and_package_info.cuda_version or "")
    except Exception as exc:  # pragma: no cover - executed by PyInstaller
        raise RuntimeError("Cannot determine the bundled ONNX Runtime CUDA version") from exc
    if not cuda_version.startswith("12."):
        raise RuntimeError(
            "The full edition must be built with the CUDA 12 ONNX Runtime wheel "
            f"for RTX 30-series driver compatibility; found CUDA {cuda_version or 'unknown'}."
        )

    get_paths: Callable[..., Iterable[tuple[str, ...]]] | None = getattr(
        ort, "_get_nvidia_dll_paths", None
    )
    if get_paths is None:
        raise RuntimeError(
            "This onnxruntime-gpu version does not expose its CUDA DLL preload list; "
            "pin a supported ONNX Runtime version before building."
        )

    nvidia_spec = importlib.util.find_spec("nvidia")
    roots = list(nvidia_spec.submodule_search_locations or []) if nvidia_spec else []
    if not roots:
        raise RuntimeError(
            "CUDA/cuDNN wheels are missing. Install onnxruntime-gpu[cuda,cudnn]."
        )

    site_packages_root = Path(roots[0]).resolve().parent
    binaries = []
    collected_sources = set()
    missing = []
    for relative_parts in get_paths(os.name == "nt", cuda=True, cudnn=True):
        source = site_packages_root.joinpath(*relative_parts)
        if not source.is_file():
            missing.append(str(source))
            continue
        destination = str(Path(*relative_parts).parent).replace("/", os.sep)
        binaries.append((str(source), destination))
        collected_sources.add(os.path.normcase(str(source.resolve())))
    if missing:
        raise RuntimeError("Required CUDA runtime DLLs are missing:\n" + "\n".join(missing))

    # cuDNN 9.25 dynamically requests cudnn_engines_tensor_ir64_9.dll while
    # executing Conv nodes, although ORT 1.24's preload list omits it. Include
    # every cuDNN sibling so optional CNN/ext engines cannot be missing either.
    cudnn_bin = site_packages_root / "nvidia" / "cudnn" / "bin"
    cudnn_pattern = "*.dll" if os.name == "nt" else "libcudnn*.so*"
    for source in sorted(cudnn_bin.glob(cudnn_pattern)):
        normalized = os.path.normcase(str(source.resolve()))
        if normalized in collected_sources:
            continue
        relative_parent = source.relative_to(site_packages_root).parent
        binaries.append((str(source), str(relative_parent).replace("/", os.sep)))
        collected_sources.add(normalized)
    return binaries
