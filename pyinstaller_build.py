"""Shared PyInstaller configuration for the SACC release editions."""

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


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "static"

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


def _existing_directory(source: Path, destination: str):
    if source.is_dir():
        return [(str(source), destination)]
    return []


def _static_data_files():
    """Collect production web assets without uploads or Three.js demos/docs."""
    datas = []
    datas.extend(_existing_directory(STATIC_ROOT / "css", "static/css"))
    datas.extend(_existing_directory(STATIC_ROOT / "vendor" / "katex-0.16.22", "static/vendor/katex-0.16.22"))

    agent_visibility_config = STATIC_ROOT / "agent_visibility.json"
    if not agent_visibility_config.is_file():
        raise FileNotFoundError(
            f"Required agent visibility config is missing: {agent_visibility_config}"
        )
    datas.append((str(agent_visibility_config), "static"))

    # Preserve every non-Three JavaScript asset. Three.js is handled below with
    # an explicit runtime allowlist because the vendored tree is mostly demos,
    # documentation, source maps, test fixtures, and large sample models.
    js_root = STATIC_ROOT / "js"
    for source in sorted(js_root.glob("*.js")):
        datas.append((str(source), "static/js"))
    for source in sorted(path for path in js_root.iterdir() if path.is_dir() and path.name != "three"):
        datas.append((str(source), f"static/js/{source.name}"))

    three_runtime_files = {
        js_root / "three" / "build" / "three.module.js": "static/js/three/build",
        js_root / "three" / "build" / "three.core.js": "static/js/three/build",
        js_root / "three" / "examples" / "jsm" / "controls" / "OrbitControls.js":
            "static/js/three/examples/jsm/controls",
        js_root / "three" / "examples" / "jsm" / "controls" / "TransformControls.js":
            "static/js/three/examples/jsm/controls",
        js_root / "three" / "examples" / "jsm" / "loaders" / "RGBELoader.js":
            "static/js/three/examples/jsm/loaders",
        js_root / "three" / "examples" / "jsm" / "environments" / "RoomEnvironment.js":
            "static/js/three/examples/jsm/environments",
        js_root / "three" / "examples" / "jsm" / "utils" / "BufferGeometryUtils.js":
            "static/js/three/examples/jsm/utils",
    }
    for source, destination in three_runtime_files.items():
        if not source.is_file():
            raise FileNotFoundError(f"Required Three.js runtime asset is missing: {source}")
        datas.append((str(source), destination))

    # Code modeling resolves user-generated Three.js addon imports at runtime.
    # Include the addon modules (including GLTFExporter and its dependencies),
    # while still excluding examples' models, screenshots, docs and test assets.
    datas.extend(_existing_directory(js_root / "three" / "examples" / "jsm", "static/js/three/examples/jsm"))

    hdr_background = STATIC_ROOT / "environment.hdr"
    if hdr_background.is_file():
        datas.append((str(hdr_background), "static"))

    # Runtime uploads are deliberately absent. routes.api creates the writable
    # directory on startup; user documents and generated previews are not part
    # of an application release.
    return datas


def _agent_data_files():
    """Collect resources for dynamically discovered agents."""
    datas = []
    agents_root = PROJECT_ROOT / "agents"
    if not agents_root.is_dir():
        return datas
    for agent_dir in sorted(path for path in agents_root.iterdir() if path.is_dir()):
        manifest = agent_dir / "manifest.json"
        if manifest.is_file():
            datas.append((str(manifest), f"agents/{agent_dir.name}"))
        for resource_dir_name in ("templates", "static"):
            resource_dir = agent_dir / resource_dir_name
            if resource_dir.is_dir():
                datas.append(
                    (str(resource_dir), f"agents/{agent_dir.name}/{resource_dir_name}")
                )
    return datas


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


def _datas_and_hiddenimports(include_cuda: bool):
    datas = _static_data_files()
    office_runtime = PROJECT_ROOT / ".tools" / "libreoffice" / "runtime"
    if not (office_runtime / "program" / "soffice.com").is_file():
        raise FileNotFoundError("Missing bundled Office converter: run scripts/prepare_office_runtime.ps1")
    datas.append((str(office_runtime), "libreoffice"))
    datas.append((str(PROJECT_ROOT / "templates"), "templates"))
    if (PROJECT_ROOT / "migrations").is_dir():
        datas.append((str(PROJECT_ROOT / "migrations"), "migrations"))
    if (PROJECT_ROOT / "agent_skills").is_dir():
        datas.append((str(PROJECT_ROOT / "agent_skills"), "agent_skills"))
    if (PROJECT_ROOT / "mcp_server.py").is_file():
        datas.append((str(PROJECT_ROOT / "mcp_server.py"), "."))
    datas.extend(_agent_data_files())

    rapidocr_datas, rapidocr_hiddenimports = _rapidocr_files()
    datas.extend(rapidocr_datas)

    vosk_models = PROJECT_ROOT / "vosk_models"
    if vosk_models.is_dir():
        datas.append((str(vosk_models), "vosk_models"))

    rapidocr_models = PROJECT_ROOT / "rapidocr_models"
    if rapidocr_models.is_dir():
        datas.append((str(rapidocr_models), "rapidocr_models"))

    hiddenimports = [
        "config",
        "database",
        "fitz",
        "flask",
        "flask_sqlalchemy",
        "flask_migrate",
        "jinja2",
        "lxml",
        "mss",
        "mss.tools",
        "numpy",
        "onnxruntime",
        "openpyxl",
        "PIL",
        "pptx",
        "pymupdf",
        "rapidocr",
        "tkinter",
        "vosk",
        "webview",
        "werkzeug",
        "xlrd",
        *collect_submodules("agents"),
        *collect_submodules("services.agent_runtime"),
        "models.agent_runtime",
        "routes.agent_runtime",
        "routes.agent_files",
        "send2trash",
        "reportlab.pdfbase.cidfonts",
        *rapidocr_hiddenimports,
    ]
    hiddenimports = list(dict.fromkeys(hiddenimports))

    binaries = collect_dynamic_libs("vosk")
    binaries.extend(_onnxruntime_binaries(include_cuda))
    if include_cuda:
        binaries.extend(_nvidia_runtime_binaries())
    return datas, binaries, hiddenimports


def build_application(spec_globals, *, edition: str, dist_name: str):
    """Instantiate the PyInstaller build graph for one release edition."""
    if edition not in {"standard", "full"}:
        raise ValueError(f"Unsupported release edition: {edition}")
    include_cuda = edition == "full"

    Analysis = spec_globals["Analysis"]
    PYZ = spec_globals["PYZ"]
    EXE = spec_globals["EXE"]
    COLLECT = spec_globals["COLLECT"]

    datas, binaries, hiddenimports = _datas_and_hiddenimports(include_cuda)
    analysis = Analysis(
        [str(PROJECT_ROOT / "app.py")],
        pathex=[str(PROJECT_ROOT)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[str(PROJECT_ROOT / "pyinstaller_hooks")],
        hooksconfig={},
        runtime_hooks=[],
        excludes=ANALYSIS_EXCLUDES,
        noarchive=False,
        optimize=0,
    )
    pyz = PYZ(analysis.pure)
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="sacc",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(PROJECT_ROOT / "favicon.ico"),
    )
    return COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=True,
        upx_exclude=[
            "onnxruntime*.dll",
            "nvidia\\*.dll",
            "libvosk.dll",
        ],
        name=dist_name,
    )
