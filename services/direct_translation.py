"""Original-format translation pipeline used by the translator agent.

The module deliberately contains no model client.  It extracts stable text units,
asks the caller to translate those units, validates the result and writes it back
to a copy of the source package/page.  File content is always data, never a
prompt or an instruction source.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import contextlib
from collections import Counter
from decimal import Decimal
import copy
from functools import lru_cache
import hashlib
from io import BytesIO, StringIO
import json
import math
import mimetypes
import os
import queue
import re
import shutil
import threading
import time
import traceback
import unicodedata
import uuid
import zipfile


from services.direct_translation_formats import EXTRA_EXTENSIONS, extract_extra, rebuild_extra, validate_extra

SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg"} | EXTRA_EXTENSIONS
ARTIFACT_DIRECTORY_NAME = "translation_artifacts"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PDF_EXTRACTION_CACHE_SCHEMA = 6
PDF_TRANSLATION_CHECKPOINT_SCHEMA = 3
PDF_OCR_CPU_BUDGET_RATIO = 0.50
PDF_OCR_PEAK_CPU_TARGET_PERCENT = 80
PDF_OCR_MAX_WORKERS = 3
DIRECT_TRANSLATION_BATCH_TIMEOUT_SECONDS = 600
DIRECT_TRANSLATION_HEARTBEAT_SECONDS = 2
OCR_QUALITY_TARGETS = {
    "ultra_low": 1200,
    "low": 1400,
    "medium": 1600,
    "high": 2200,
}

_OPENCV_THREAD_BUDGET_LOCK = threading.RLock()
_OPENCV_THREAD_BUDGET_USERS = 0
_OPENCV_ORIGINAL_THREADS = None
_OPENCV_ACTIVE_THREAD_BUDGETS = {}
_FONT_CACHE_LOCAL = threading.local()
_PDF_OCR_WORKER_LOCAL = threading.local()


class DirectTranslationPaused(RuntimeError):
    pass


def pdf_ocr_thread_plan(requested_workers=1, *, cpu_count=None, total_pages=None):
    """Return a bounded PDF OCR plan for the current machine.

    Page workers own independent ONNX sessions. Keeping the default at one
    avoids the nested-pool CPU saturation that made CPU-only PDF jobs appear to
    hang, while still allowing capable machines to opt into two or three page
    workers. The per-engine thread budget is divided between those workers so
    increasing page concurrency never multiplies the total CPU budget.
    """
    try:
        logical_cpus = max(1, int(cpu_count if cpu_count is not None else (os.cpu_count() or 1)))
    except (TypeError, ValueError):
        logical_cpus = 1
    hardware_cap = 3 if logical_cpus >= 12 else (2 if logical_cpus >= 6 else 1)
    max_workers = min(PDF_OCR_MAX_WORKERS, hardware_cap)
    try:
        requested = int(requested_workers or 1)
    except (TypeError, ValueError):
        requested = 1
    workers = max(1, min(requested, max_workers))
    if total_pages is not None:
        workers = min(workers, max(1, int(total_pages or 1)))
    cpu_budget = max(
        1,
        min(logical_cpus, int(math.floor(logical_cpus * PDF_OCR_CPU_BUDGET_RATIO))),
    )
    # Useful ONNX work is capped at half of the logical CPUs. The remaining
    # headroom absorbs rasterization, preprocessing, WebView work and short
    # native-library bursts so peaks stay below the 80% product target.
    desired_engine_threads = 1 if workers == 1 else 2
    engine_threads = min(desired_engine_threads, max(1, cpu_budget // workers))
    return {
        "logical_cpu_count": logical_cpus,
        "max_workers": max_workers,
        "worker_count": workers,
        "cpu_thread_budget": cpu_budget,
        "engine_threads": engine_threads,
        "allocated_threads": min(logical_cpus, engine_threads * workers),
        "peak_cpu_target_percent": PDF_OCR_PEAK_CPU_TARGET_PERCENT,
    }


def _use_windows_background_mode_for_current_thread():
    """Lower scheduler and I/O priority for every dedicated PDF OCR worker."""
    if os.name != "nt" or getattr(_PDF_OCR_WORKER_LOCAL, "background", False):
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        current_thread = kernel32.GetCurrentThread()
        # THREAD_MODE_BACKGROUND_BEGIN lowers both CPU scheduling and I/O
        # priority for this dedicated executor thread until it exits.
        if kernel32.SetThreadPriority(current_thread, 0x00010000):
            _PDF_OCR_WORKER_LOCAL.background = True
            return
        # Older Windows builds may reject background mode; below-normal still
        # ensures the desktop and WebView win scheduler contention.
        if kernel32.SetThreadPriority(current_thread, -1):
            _PDF_OCR_WORKER_LOCAL.background = True
    except Exception:
        pass


@contextlib.contextmanager
def _opencv_thread_budget(max_threads=1):
    """Temporarily prevent nested OpenCV pools from oversubscribing file OCR.

    ONNX already owns the useful page-level CPU parallelism. OpenCV otherwise
    creates another pool as large as the whole machine for every resize/contour
    phase, causing brief 100% spikes without improving measured throughput.
    The reference count keeps overlapping PDF jobs safe and restores the app's
    previous OpenCV setting after the last file OCR job finishes.
    """
    global _OPENCV_THREAD_BUDGET_USERS, _OPENCV_ORIGINAL_THREADS
    try:
        import cv2
    except Exception:
        yield
        return
    token = object()
    requested_threads = max(1, int(max_threads or 1))
    with _OPENCV_THREAD_BUDGET_LOCK:
        if _OPENCV_THREAD_BUDGET_USERS == 0:
            try:
                _OPENCV_ORIGINAL_THREADS = int(cv2.getNumThreads())
            except Exception:
                _OPENCV_ORIGINAL_THREADS = None
        _OPENCV_ACTIVE_THREAD_BUDGETS[token] = requested_threads
        _OPENCV_THREAD_BUDGET_USERS = len(_OPENCV_ACTIVE_THREAD_BUDGETS)
        try:
            # The strictest overlapping job wins. A concurrent OCR job asking
            # for one thread cannot be relaxed by a rebuild job allowing four.
            cv2.setNumThreads(min(_OPENCV_ACTIVE_THREAD_BUDGETS.values()))
        except Exception:
            pass
    try:
        yield
    finally:
        with _OPENCV_THREAD_BUDGET_LOCK:
            _OPENCV_ACTIVE_THREAD_BUDGETS.pop(token, None)
            _OPENCV_THREAD_BUDGET_USERS = len(_OPENCV_ACTIVE_THREAD_BUDGETS)
            if _OPENCV_ACTIVE_THREAD_BUDGETS:
                try:
                    cv2.setNumThreads(min(_OPENCV_ACTIVE_THREAD_BUDGETS.values()))
                except Exception:
                    pass
            else:
                original = _OPENCV_ORIGINAL_THREADS
                _OPENCV_ORIGINAL_THREADS = None
                if original:
                    try:
                        cv2.setNumThreads(max(1, int(original)))
                    except Exception:
                        pass


def _safe_name(value: str) -> str:
    name = os.path.basename(str(value or "file")).strip() or "file"
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)


def _translated_output_name(original_name: str, target_language: str, translated_stem: str, reserved_names) -> str:
    """Build a safe download name while keeping the source extension unchanged."""
    fallback_stem, ext = os.path.splitext(_safe_name(original_name))
    value = unicodedata.normalize("NFC", str(translated_stem or fallback_stem)).strip()
    value = next((line.strip() for line in value.splitlines() if line.strip()), fallback_stem)
    value = value.strip(' \t\r\n"\'“”‘’')
    if ext and value.casefold().endswith(ext.casefold()):
        value = value[:-len(ext)].rstrip()
    # Replace path separators before _safe_name so a model-produced slash does
    # not cause os.path.basename to silently discard the first half.
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value)
    value = _safe_name(value).strip(" .") or fallback_stem or "file"
    language = re.sub(r"[^A-Za-z0-9_-]+", "_", str(target_language or "target")).strip("_") or "target"
    # Stay comfortably below the common 255-character component limit after
    # adding the language marker, collision suffix and extension.
    value = value[:180].rstrip(" .") or "file"
    output_root = f"{value}_{language}_translated"
    output_name = _safe_name(f"{output_root}{ext.lower()}")
    suffix = 2
    reserved = reserved_names if reserved_names is not None else set()
    while output_name.casefold() in reserved:
        output_name = _safe_name(f"{output_root}_{suffix}{ext.lower()}")
        suffix += 1
    reserved.add(output_name.casefold())
    return output_name


def _storage_root(upload_folder: str) -> str:
    root = os.path.realpath(os.path.join(upload_folder, ARTIFACT_DIRECTORY_NAME))
    os.makedirs(root, exist_ok=True)
    return root


def _safe_join(root: str, *parts: str) -> str:
    candidate = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([candidate, os.path.realpath(root)]) != os.path.realpath(root):
        raise ValueError("无效的直出文件路径")
    return candidate


def validate_direct_files(files):
    unsupported = []
    for item in files or []:
        name = str((item or {}).get("name") or (item or {}).get("filename") or "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            unsupported.append(name or "未命名文件")
    return unsupported


def _validate_file_signature(path: str, ext: str) -> None:
    if ext in EXTRA_EXTENSIONS:
        validate_extra(path, ext)
        return
    with open(path, "rb") as handle:
        signature = handle.read(12)
    if ext == ".pdf" and not signature.startswith(b"%PDF-"):
        raise ValueError("文件扩展名为 PDF，但内容不是有效 PDF")
    if ext == ".png" and signature[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("文件扩展名为 PNG，但内容不是有效 PNG")
    if ext in {".jpg", ".jpeg"} and signature[:2] != b"\xff\xd8":
        raise ValueError("文件扩展名为 JPEG，但内容不是有效 JPEG")
    if ext in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(path, "r") as package:
                names = set(package.namelist())
                required = "word/document.xml" if ext == ".docx" else "xl/workbook.xml"
                if required not in names or package.testzip():
                    raise ValueError(f"{ext.upper().lstrip('.')} 文件包结构无效或 CRC 损坏")
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{ext.upper().lstrip('.')} 文件包已损坏") from exc


def prepare_artifacts(files, job_id: str, target_language: str, upload_folder: str):
    """Validate and snapshot uploads before the UI is allowed to clean them."""
    root = _storage_root(upload_folder)
    job_dir = _safe_join(root, job_id)
    os.makedirs(job_dir, exist_ok=True)
    artifacts = []
    reserved_output_names = set()

    for item in files or []:
        if not isinstance(item, dict):
            raise ValueError("文件信息格式无效")
        source_id = os.path.basename(str(item.get("id") or item.get("file_id") or ""))
        if not source_id or source_id != str(item.get("id") or item.get("file_id") or ""):
            raise ValueError("文件标识无效")
        source_path = _safe_join(upload_folder, source_id)
        if not os.path.isfile(source_path):
            raise FileNotFoundError(f"找不到上传文件: {item.get('name') or source_id}")
        source_size = os.path.getsize(source_path)
        if source_size <= 0:
            raise ValueError(f"文件为空，无法直出翻译: {item.get('name') or source_id}")

        original_name = _safe_name(item.get("name") or item.get("filename") or source_id)
        stem, ext = os.path.splitext(original_name)
        ext = ext.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"直出暂不支持: {original_name}")
        _validate_file_signature(source_path, ext)

        artifact_id = uuid.uuid4().hex
        artifact_dir = _safe_join(job_dir, artifact_id)
        os.makedirs(artifact_dir, exist_ok=True)
        source_storage_name = f"source{ext}"
        snapshot_path = _safe_join(artifact_dir, source_storage_name)
        shutil.copy2(source_path, snapshot_path)
        output_name = _safe_name(f"{stem}_{target_language}_translated{ext}")
        suffix = 2
        while output_name.lower() in reserved_output_names:
            output_name = _safe_name(f"{stem}_{target_language}_translated_{suffix}{ext}")
            suffix += 1
        reserved_output_names.add(output_name.lower())
        storage_rel = "/".join((ARTIFACT_DIRECTORY_NAME, job_id, artifact_id, source_storage_name))
        mime = item.get("type") or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        artifact = {
            "artifact_id": artifact_id,
            "job_id": job_id,
            "format": ext.lstrip("."),
            "status": "queued",
            "stage": "preflight",
            "progress": 0,
            "message": "等待文件预检",
            "warnings": [],
            "qa_summary": {},
            "source": {
                "name": original_name,
                "type": mime,
                "size": source_size,
                "url": f"/api/agents/translate/artifact/{artifact_id}/content?source=1",
                "storage_key": storage_rel,
            },
            "output": {
                "name": output_name,
                "type": mime,
            },
        }
        _write_manifest(upload_folder, artifact, {"translations": {}})
        artifacts.append(artifact)
    return artifacts


def public_artifacts(artifacts):
    result = copy.deepcopy(artifacts or [])
    for artifact in result:
        for section in (artifact.get("source"), artifact.get("output")):
            if isinstance(section, dict):
                section.pop("storage_key", None)
    return result


def artifact_file_path(upload_folder: str, artifact: dict, section: str) -> str:
    meta = (artifact or {}).get(section) or {}
    storage_key = str(meta.get("storage_key") or "").replace("/", os.sep)
    if not storage_key:
        raise FileNotFoundError("产物文件尚未生成")
    return _safe_join(upload_folder, storage_key)


def delete_artifact_files(upload_folder: str, artifacts) -> None:
    """Remove only the opaque job directories referenced by persisted artifacts."""
    job_ids = {
        str(item.get("job_id") or "")
        for item in (artifacts or [])
        if isinstance(item, dict) and item.get("job_id")
    }
    root = _storage_root(upload_folder)
    for job_id in job_ids:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", job_id):
            continue
        path = _safe_join(root, job_id)
        if os.path.isdir(path):
            shutil.rmtree(path)


def delete_direct_job(upload_folder: str, job_id: str) -> None:
    job_id = str(job_id or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", job_id):
        return
    path = _safe_join(_storage_root(upload_folder), job_id)
    if os.path.isdir(path):
        shutil.rmtree(path)


def _artifact_dir(upload_folder: str, artifact: dict) -> str:
    return _safe_join(_storage_root(upload_folder), artifact["job_id"], artifact["artifact_id"])


def _manifest_path(upload_folder: str, artifact: dict) -> str:
    return _safe_join(_artifact_dir(upload_folder, artifact), "manifest.json")


def _write_manifest(upload_folder: str, artifact: dict, state: dict):
    path = _manifest_path(upload_folder, artifact)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "artifact_id": artifact.get("artifact_id"),
        "job_id": artifact.get("job_id"),
        "format": artifact.get("format"),
        "source": artifact.get("source"),
        "output": artifact.get("output"),
        "translation_schema": PDF_TRANSLATION_CHECKPOINT_SCHEMA,
        **(state or {}),
    }
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temp_path, path)


def _read_manifest(upload_folder: str, artifact: dict) -> dict:
    try:
        with open(_manifest_path(upload_folder, artifact), "r", encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{threading.get_ident()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp_path, path)


def _ocr_quality_config(ocr_callback):
    quality = str(getattr(ocr_callback, "ocr_quality", "medium") or "medium").strip().lower()
    if quality not in OCR_QUALITY_TARGETS:
        quality = "medium"
    try:
        requested_target = int(getattr(ocr_callback, "ocr_target_longest", OCR_QUALITY_TARGETS[quality]))
    except (TypeError, ValueError):
        requested_target = OCR_QUALITY_TARGETS[quality]
    return quality, max(1200, min(2400, requested_target))


def _pdf_checkpoint_signature(source_path: str, source_language: str, ocr_target_longest=1600) -> str:
    """Fingerprint the immutable task snapshot and extraction configuration."""
    stat = os.stat(source_path)
    digest = hashlib.sha256()
    digest.update(f"pdf-extract-v{PDF_EXTRACTION_CACHE_SCHEMA}\0".encode("ascii"))
    digest.update(str(int(stat.st_size)).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(source_language or "auto").strip().lower().encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(int(ocr_target_longest or 1600)).encode("ascii"))
    # Reading 128 KiB is effectively constant-time even for a 1 GiB source and
    # protects against an accidental replacement of the task snapshot.
    with open(source_path, "rb") as handle:
        digest.update(handle.read(65536))
        if stat.st_size > 65536:
            handle.seek(max(0, stat.st_size - 65536))
            digest.update(handle.read(65536))
    return digest.hexdigest()


def _pdf_extraction_cache_path(upload_folder: str, artifact: dict) -> str:
    return _safe_join(_artifact_dir(upload_folder, artifact), "pdf_extraction.json")


def _pdf_page_checkpoint_dir(upload_folder: str, artifact: dict) -> str:
    return _safe_join(_artifact_dir(upload_folder, artifact), "pdf_ocr_pages")


def _read_pdf_extraction_cache(upload_folder, artifact, signature):
    try:
        with open(_pdf_extraction_cache_path(upload_folder, artifact), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != PDF_EXTRACTION_CACHE_SCHEMA or payload.get("signature") != signature:
            return None
        units = payload.get("units")
        source_meta = payload.get("source_meta")
        if not payload.get("completed") or not isinstance(units, list) or not isinstance(source_meta, dict):
            return None
        if any(not isinstance(unit, dict) or not unit.get("id") for unit in units):
            return None
        # JSON has no tuple type, while the PDF audit intentionally compares
        # exact `(width, height)` pairs produced by PyMuPDF.
        source_meta["page_sizes"] = [
            tuple(item) if isinstance(item, (list, tuple)) else item
            for item in source_meta.get("page_sizes") or []
        ]
        return units, source_meta
    except Exception:
        return None


def _write_pdf_extraction_cache(upload_folder, artifact, signature, units, source_meta):
    _write_json_atomic(_pdf_extraction_cache_path(upload_folder, artifact), {
        "schema": PDF_EXTRACTION_CACHE_SCHEMA,
        "signature": signature,
        "completed": True,
        "units": units,
        "source_meta": source_meta,
    })


def _pdf_page_checkpoint_path(checkpoint_dir, page_index):
    return os.path.join(checkpoint_dir, f"page_{int(page_index) + 1:06d}.json")


def _read_pdf_page_checkpoint(checkpoint_dir, page_index, signature):
    if not checkpoint_dir or not signature:
        return None
    try:
        with open(_pdf_page_checkpoint_path(checkpoint_dir, page_index), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != PDF_EXTRACTION_CACHE_SCHEMA
            or payload.get("signature") != signature
            or int(payload.get("page_index")) != int(page_index)
            or not isinstance(payload.get("units"), list)
        ):
            return None
        units = payload["units"]
        if any(
            not isinstance(unit, dict)
            or int(unit.get("page", -1)) != int(page_index)
            or not unit.get("id")
            for unit in units
        ):
            return None
        return units, float(payload.get("detection_zoom") or 1.0)
    except Exception:
        return None


def _write_pdf_page_checkpoint(checkpoint_dir, page_index, signature, units, detection_zoom):
    if not checkpoint_dir or not signature:
        return
    _write_json_atomic(_pdf_page_checkpoint_path(checkpoint_dir, page_index), {
        "schema": PDF_EXTRACTION_CACHE_SCHEMA,
        "signature": signature,
        "page_index": int(page_index),
        "detection_zoom": float(detection_zoom),
        "units": units,
    })


def _clear_pdf_page_checkpoints(checkpoint_dir):
    if checkpoint_dir and os.path.isdir(checkpoint_dir):
        shutil.rmtree(checkpoint_dir)


def _clear_pdf_extraction_checkpoints(upload_folder, artifact):
    try:
        cache_path = _pdf_extraction_cache_path(upload_folder, artifact)
        if os.path.isfile(cache_path):
            os.remove(cache_path)
    except OSError:
        pass
    try:
        _clear_pdf_page_checkpoints(_pdf_page_checkpoint_dir(upload_folder, artifact))
    except OSError:
        pass


def _set_artifact(artifact, stage, progress, message, status="running"):
    artifact["status"] = status
    artifact["stage"] = stage
    artifact["progress"] = max(0, min(100, int(progress or 0)))
    artifact["message"] = str(message or "")


def _check_pause(should_cancel, artifact=None):
    if should_cancel and should_cancel():
        if artifact is not None:
            _set_artifact(artifact, artifact.get("stage") or "translate", artifact.get("progress") or 0, "任务已暂停", "paused")
        raise DirectTranslationPaused("任务已暂停")


def _unit_id(prefix: str, location: str, text: str) -> str:
    digest = hashlib.sha1(f"{location}\0{text}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _is_translatable(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return any(ch.isalpha() or "\u3400" <= ch <= "\u9fff" for ch in value)


def _dedupe_key(unit: dict):
    text = re.sub(r"\s+", " ", str(unit.get("text") or "")).strip()
    if unit.get("kind") == "filename":
        return f"filename\0{text}"
    if len(text) >= 8:
        return text
    return f"{unit.get('context') or ''}\0{text}"


def _token_signature(text: str):
    # Compare quantities, not substrings: m must not consume the first letter
    # of mètres, and 200 must not be considered present inside 2000.
    value = unicodedata.normalize("NFKC", str(text or ""))
    aliases = [
        (r"mètres?\s+cubes?|cubic\s+met(?:er|re)s?|立方米|m\^?3", "m3"),
        (r"mètres?\s+carrés?|square\s+met(?:er|re)s?|平方米|m\^?2", "m2"),
        (r"millimètres?|millimet(?:er|re)s?|毫米|mm", "mm"),
        (r"centimètres?|centimet(?:er|re)s?|厘米|cm", "cm"),
        (r"kilomètres?|kilomet(?:er|re)s?|千米|公里|km", "km"),
        (r"mètres?|met(?:er|re)s?|米|m", "m"),
        (r"kilogrammes?|kilograms?|千克|公斤|kg", "kg"),
        (r"兆帕|MPa", "MPa"), (r"千帕|kPa", "kPa"),
        (r"摄氏度|°\s*C", "°C"), (r"heures?|hours?|小时|h", "h"),
        (r"pour\s*cent|percent|%", "%"),
    ]
    unit_pattern = "|".join(f"(?:{pattern})(?![A-Za-z])" for pattern, _ in aliases)
    # Stop at ordinary mixed-case words appended to an equipment identifier.
    # Keep technical uppercase segments (TD, ECL, BT) and numeric suffixes.
    pattern = re.compile(r"(?P<code>\b[A-Z]{1,8}\d[A-Z0-9]*(?:[./+-][A-Z0-9]+)*(?![A-Za-z0-9]))|"
                         r"(?P<number>[+-]?\d+(?:[.,]\d+)?)\s*(?P<unit>" + unit_pattern + r")?")
    tokens = []
    for match in pattern.finditer(value):
        if match['code']:
            code = re.sub(r'[-/](?:COFFRET|PANEL|CABINET|DISTRIBUTION)$', '', match['code'])
            tokens.append(code)
            continue
        number = format(Decimal(match['number'].replace(',', '.')).normalize(), 'f')
        unit = match['unit'] or ''
        canonical = next((name for regex, name in aliases if re.fullmatch(regex, unit, re.I)), '')
        tokens.append(number + canonical)
    return tokens


def _number_lists_for_comparison(source, translated):
    """Only disambiguate comma numbers with an explicit matching target list.

    Decimal comma and thousands punctuation are otherwise left intact. Units
    forbid this list interpretation: 1,5 MPa must still match 1.5 MPa, not 15.
    """
    target = unicodedata.normalize('NFKC', str(translated or ''))
    def replace(match):
        parts = re.split(r',\s*', match[0])
        following = source[match.end():match.end()+32]
        if len(parts) == 2:
            tokens = _token_signature(match[0] + following)
            decimal = format(Decimal(match[0].replace(' ', '').replace(',', '.')).normalize(), 'f')
            if not tokens or tokens[0] != decimal:
                return match[0]
        explicit = r'(?<![\d.])' + r'\s*(?:、|;|，|,\s+)\s*'.join(re.escape(p.strip()) for p in parts) + r'(?![\d.])'
        return '、'.join(parts) if re.search(explicit, target) else match[0]
    return re.sub(r'(?<![\w.])\d+(?:,\s*\d+)+(?!\d|\.\d)', replace, source)


def _validate_translation(source: str, translated: str):
    warnings = []
    if not str(translated or "").strip():
        return ["译文为空"]
    normalized_source = re.sub(r"\s+", " ", str(source or "")).strip()
    normalized_translation = re.sub(r"\s+", " ", str(translated or "")).strip()
    identifier_only = _token_signature(normalized_source) == [normalized_source] or bool(re.fullmatch(r'[\d\s.,/%+\-]+', normalized_source))
    if normalized_source and normalized_source == normalized_translation and not identifier_only:
        warnings.append("译文疑似整段照抄原文")
    source_han = re.findall(r"[\u3400-\u9fff]", normalized_source)
    translated_han = re.findall(r"[\u3400-\u9fff]", normalized_translation)
    if len(source_han) >= 2 and len(translated_han) >= max(2, int(len(source_han) * 0.35)):
        warnings.append("译文可能仍包含较多源语言文字")
    src_tokens = _token_signature(_number_lists_for_comparison(str(source or ''), translated))
    dst_tokens = Counter(_token_signature(translated))
    for token in src_tokens:
        if dst_tokens[token]:
            dst_tokens[token] -= 1
        else:
            warnings.append(f"可能缺少参数: {token}")
    return warnings[:12]


def _batches(units, max_units=60, max_chars=3000):
    batches, current, chars = [], [], 0
    for unit in units:
        is_ocr = unit.get("kind") == "ocr" or bool(unit.get("ocr"))
        # OCR tables contain many short, visually adjacent labels. Large
        # cross-page requests made some models preserve every ID yet rotate the
        # translations between neighboring rows/cells. Keep OCR requests small
        # and page-local so the source echo validation at the model boundary can
        # safely retry only the affected page fragment.
        unit_limit = min(max_units, 36) if is_ocr else max_units
        char_limit = min(max_chars, 2600) if is_ocr else max_chars
        size = len(str(unit.get("text") or ""))
        current_is_ocr = bool(current and (current[0].get("kind") == "ocr" or current[0].get("ocr")))
        crossed_ocr_page = bool(
            current
            and current_is_ocr
            and is_ocr
            and current[0].get("page") is not None
            and unit.get("page") is not None
            and int(current[0].get("page")) != int(unit.get("page"))
        )
        if current and (
            current_is_ocr != is_ocr
            or crossed_ocr_page
            or len(current) >= unit_limit
            or chars + size > char_limit
        ):
            batches.append(current)
            current, chars = [], 0
        current.append(unit)
        chars += size
    if current:
        batches.append(current)
    return batches


def _is_model_context_limit_error(error) -> bool:
    message = str(error or "").lower()
    if "application context" in message or "request context" in message:
        return False
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "max context length",
        "context window",
        "prompt is too long",
        "prompt too long",
        "input is too long",
        "too many tokens",
        "token limit exceeded",
        "上下文长度",
        "超出上下文",
        "输入内容过长",
    )
    return any(marker in message for marker in markers)


def _split_batch_by_size(batch):
    if len(batch) < 2:
        return list(batch), []
    total = sum(max(1, len(str(item.get("text") or ""))) for item in batch)
    target = max(1, total // 2)
    left, size = [], 0
    for item in batch:
        if left and size >= target:
            break
        left.append(item)
        size += max(1, len(str(item.get("text") or "")))
    if len(left) >= len(batch):
        midpoint = max(1, len(batch) // 2)
        left = list(batch[:midpoint])
    return left, list(batch[len(left):])


def _split_text_for_context(text: str, max_chars: int):
    value = str(text or "")
    if len(value) <= max_chars:
        return [value]
    chunks = []
    start = 0
    preferred_breaks = "\n。！？；.!?;"
    while start < len(value):
        hard_end = min(len(value), start + max_chars)
        if hard_end >= len(value):
            end = len(value)
        else:
            search_start = start + max(1, int(max_chars * 0.55))
            end = max((value.rfind(char, search_start, hard_end) for char in preferred_breaks), default=-1)
            if end >= search_start:
                end += 1
            else:
                whitespace = max(value.rfind(" ", search_start, hard_end), value.rfind("\t", search_start, hard_end))
                end = whitespace + 1 if whitespace >= search_start else hard_end
        piece = value[start:end]
        if piece:
            chunks.append(piece)
        start = end
    return chunks or [value]


class DirectTranslationBatchTimeout(TimeoutError):
    pass


def _call_translation_batch_with_watchdog(
    callback,
    timeout_seconds,
    should_cancel,
    artifact,
    request_delta=None,
):
    """Run one provider request with an absolute wall-clock deadline.

    `requests` read timeouts reset when a provider drips response bytes, so a
    malformed upstream can remain alive indefinitely. The daemon wrapper lets
    the managed batch slot retry after the absolute deadline. The late result
    from the abandoned request is ignored and can never overwrite a checkpoint.
    """
    result_queue = queue.Queue(maxsize=1)

    def invoke():
        if request_delta:
            request_delta(1)
        try:
            result_queue.put((True, callback()))
        except BaseException as exc:
            result_queue.put((False, exc))
        finally:
            if request_delta:
                request_delta(-1)

    worker = threading.Thread(target=invoke, name="direct-translation-api", daemon=True)
    worker.start()
    timeout_seconds = max(0.05, float(timeout_seconds or DIRECT_TRANSLATION_BATCH_TIMEOUT_SECONDS))
    deadline = time.monotonic() + timeout_seconds
    while True:
        _check_pause(should_cancel, artifact)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DirectTranslationBatchTimeout(
                f"AI 翻译批次超过 {int(round(timeout_seconds))} 秒仍无响应"
            )
        try:
            succeeded, payload = result_queue.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        if succeeded:
            return payload
        raise payload


def _target_language_issue(source, translated, target_language):
    """Conservative script check for prose, not identifiers or proper names.

    This is a coverage guard, not a general language detector: same-script
    language pairs still require semantic review.
    """
    target = str(target_language or '').lower().replace('_', '-').split('-')[0]
    scripts = {'zh': r'[\u3400-\u9fff]', 'ja': r'[\u3040-\u30ff\u3400-\u9fff]',
               'ko': r'[\uac00-\ud7af]', 'ar': r'[\u0600-\u06ff]', 'ru': r'[\u0400-\u04ff]'}
    script = scripts.get(target)
    if not script:
        return None
    if re.search(script, str(translated)):
        if not re.search(script, str(source)):
            normalized = re.sub(r'\s+', ' ', str(translated)).casefold()
            for sentence in re.split(r'(?<=[.!?。！？])\s+|\n+', str(source)):
                if len(re.findall(r'[^\W\d_]+', sentence)) >= 8 and re.sub(r'\s+', ' ', sentence).strip().casefold() in normalized:
                    return '译文仍保留完整源语言句子'
        return None
    words = re.findall(r'[^\W\d_]+', str(source), re.UNICODE)
    # Short labels, names, acronyms and equipment codes can legitimately survive.
    prose = [word for word in words if len(word) >= 3 and not word.isupper()]
    if len(prose) >= 6:
        return f'译文未使用目标语言 {target}'
    return None


def _translate_units(
    units,
    translate_batch,
    existing,
    max_workers,
    artifact,
    notify,
    should_cancel,
    upload_folder,
    batch_timeout_seconds=None,
    target_language=None,
):
    unique = []
    aliases = {}
    key_to_id = {}
    for unit in units:
        key = _dedupe_key(unit)
        canonical = key_to_id.get(key)
        if canonical:
            aliases[unit["id"]] = canonical
        else:
            key_to_id[key] = unit["id"]
            unique.append(unit)

    pending = [unit for unit in unique if not existing.get(unit["id"])]
    batches = _batches(pending)
    total = max(1, len(batches))
    # `max_workers` has already been resolved from the global Agent concurrency
    # setting by the caller.  Do not reuse the unrelated three-worker OCR CPU
    # cap here: these workers wait on remote model API requests.
    worker_limit = max(1, int(max_workers or 1))
    watchdog_seconds = max(
        0.05,
        float(batch_timeout_seconds or DIRECT_TRANSLATION_BATCH_TIMEOUT_SECONDS),
    )
    runtime_lock = threading.Lock()
    runtime = {
        "phase": "translate",
        "limit": worker_limit,
        "active": 0,
        "workers_active": 0,
        "active_batches": set(),
        "total_batches": len(batches),
        "completed_batches": 0,
        "watchdog_seconds": max(1, int(round(watchdog_seconds))),
        "watchdog_retries": 0,
        "last_retry_batch": 0,
        "last_retry_reason": "",
    }

    def request_delta(delta):
        with runtime_lock:
            runtime["active"] = max(0, int(runtime.get("active") or 0) + int(delta or 0))

    def runtime_snapshot():
        with runtime_lock:
            return {
                **runtime,
                "active_batches": sorted(runtime.get("active_batches") or []),
            }

    def publish_runtime(force_message=False):
        state = runtime_snapshot()
        artifact["concurrency"] = state
        if force_message or artifact.get("stage") == "translate":
            message = (
                f"AI 翻译批次 {int(state.get('completed_batches') or 0)}/{max(1, int(state.get('total_batches') or 0))}"
                f" · API 实际并发 {int(state.get('active') or 0)}/{int(state.get('limit') or 1)}"
            )
            if int(state.get("watchdog_retries") or 0):
                message += (
                    f" · 第 {int(state.get('last_retry_batch') or 0)} 批超时自动重试"
                    f"（累计 {int(state.get('watchdog_retries') or 0)} 次）"
                )
            _set_artifact(
                artifact,
                "translate",
                25 + int(45 * int(state.get("completed_batches") or 0) / total),
                message,
            )
        notify()

    def request_subset(subset, batch_number, depth=0):
        _check_pause(should_cancel, artifact)
        result = {}
        missing = list(subset)
        last_error = None
        for _attempt in range(3):
            if not missing:
                break
            try:
                current_missing = list(missing)
                returned = _call_translation_batch_with_watchdog(
                    lambda: translate_batch(current_missing) or {},
                    watchdog_seconds,
                    should_cancel,
                    artifact,
                    request_delta=request_delta,
                )
                last_error = None
            except DirectTranslationPaused:
                raise
            except Exception as exc:
                last_error = exc
                if isinstance(exc, DirectTranslationBatchTimeout):
                    with runtime_lock:
                        runtime["watchdog_retries"] += 1
                        runtime["last_retry_batch"] = int(batch_number)
                        runtime["last_retry_reason"] = str(exc)
                if _is_model_context_limit_error(exc) and depth < 12:
                    if len(missing) > 1:
                        left, right = _split_batch_by_size(missing)
                        split_result = request_subset(left, batch_number, depth + 1)
                        if right:
                            split_result.update(request_subset(right, batch_number, depth + 1))
                        result.update(split_result)
                        missing = []
                        break
                    source_item = missing[0]
                    source_text = str(source_item.get("text") or "")
                    part_size = max(400, min(2400, max(1, len(source_text) // 2)))
                    parts = _split_text_for_context(source_text, part_size)
                    if len(parts) > 1:
                        virtual_items = []
                        for part_index, part in enumerate(parts):
                            virtual_items.append({
                                **source_item,
                                "id": f"{source_item['id']}.part{part_index + 1:04d}",
                                "text": part,
                                "context": f"{source_item.get('context') or ''}；长文本分段 {part_index + 1}/{len(parts)}",
                            })
                        virtual_result = {}
                        for virtual_batch in _batches(virtual_items):
                            virtual_result.update(request_subset(virtual_batch, batch_number, depth + 1))
                        joiner = "\n" if "\n" in source_text else " "
                        result[source_item["id"]] = joiner.join(
                            virtual_result[item["id"]].strip() for item in virtual_items
                        ).strip()
                        missing = []
                        break
                continue
            for item in missing:
                value = str(returned.get(item["id"]) or "").strip()
                if value:
                    result[item["id"]] = value
            missing = [item for item in missing if item["id"] not in result]
        if missing and last_error:
            raise RuntimeError(f"模型批次定向重试失败: {last_error}") from last_error
        if missing:
            raise RuntimeError("模型未返回完整的稳定 ID: " + ", ".join(item["id"] for item in missing[:8]))
        return result

    def run_one(batch, batch_number):
        with runtime_lock:
            runtime["workers_active"] += 1
            runtime["active_batches"].add(int(batch_number))
        try:
            return request_subset(batch, batch_number)
        finally:
            with runtime_lock:
                runtime["workers_active"] = max(0, int(runtime["workers_active"]) - 1)
                runtime["active_batches"].discard(int(batch_number))

    completed = 0
    artifact["concurrency"] = runtime_snapshot()
    notify()
    if batches:
        with ThreadPoolExecutor(max_workers=worker_limit) as pool:
            future_to_batch = {
                pool.submit(run_one, batch, batch_index + 1): batch_index + 1
                for batch_index, batch in enumerate(batches)
            }
            outstanding = set(future_to_batch)
            last_heartbeat = 0.0
            while outstanding:
                _check_pause(should_cancel, artifact)
                heartbeat_interval = max(0.05, float(DIRECT_TRANSLATION_HEARTBEAT_SECONDS))
                done, outstanding = wait(
                    outstanding,
                    timeout=min(1.0, heartbeat_interval),
                    return_when=FIRST_COMPLETED,
                )
                now = time.monotonic()
                if not done and now - last_heartbeat >= heartbeat_interval:
                    publish_runtime()
                    last_heartbeat = now
                for future in done:
                    _check_pause(should_cancel, artifact)
                    values = future.result()
                    existing.update(values)
                    completed += 1
                    with runtime_lock:
                        runtime["completed_batches"] = completed
                    _write_manifest(upload_folder, artifact, {"translations": existing})
                    publish_runtime(force_message=True)
                    last_heartbeat = now

    for alias_id, canonical_id in aliases.items():
        if existing.get(canonical_id):
            existing[alias_id] = existing[canonical_id]
    return existing


def _split_for_nodes(text: str, weights):
    if not weights:
        return []
    if len(weights) == 1:
        return [text]
    total_weight = max(1, sum(max(1, int(value or 0)) for value in weights))
    pieces, start = [], 0
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            pieces.append(text[start:])
            break
        target = min(len(text), start + round(len(text) * max(1, weight) / total_weight))
        left = text.rfind(" ", start, target + 1)
        right = text.find(" ", target)
        if left > start and target - left <= 12:
            target = left + 1
        elif right != -1 and right - target <= 12:
            target = right + 1
        pieces.append(text[start:target])
        start = target
    while len(pieces) < len(weights):
        pieces.append("")
    return pieces


def _apply_node_translation(unit, translation):
    refs = unit.get("refs") or []
    node_refs = [ref for ref in refs if ref.get("kind") == "text"]
    attr_refs = [ref for ref in refs if ref.get("kind") == "attr"]
    if node_refs:
        pieces = _split_for_nodes(translation, [len(ref["original"] or "") for ref in node_refs])
        for ref, piece in zip(node_refs, pieces):
            node = ref["node"]
            node.text = piece
            if piece.startswith(" ") or piece.endswith(" "):
                node.set(_XML_SPACE, "preserve")
            elif _XML_SPACE in node.attrib:
                node.attrib.pop(_XML_SPACE, None)
    for ref in attr_refs:
        ref["node"].set(ref["attribute"], translation)


def _zip_audit(path: str):
    with zipfile.ZipFile(path, "r") as package:
        bad = package.testzip()
        return {
            "parts": len(package.namelist()),
            "bad_crc_part": bad or "",
            "part_names": set(package.namelist()),
        }


def _ooxml_structure_audit(path: str, package_kind: str):
    etree = _load_lxml()
    counts = {}
    docx_names = {
        "paragraphs": "p", "tables": "tbl", "rows": "tr", "cells": "tc",
        "merged_columns": "gridSpan", "merged_rows": "vMerge", "drawings": "drawing",
        "sections": "sectPr", "bookmarks": "bookmarkStart", "hyperlinks": "hyperlink",
    }
    xlsx_names = {
        "cells": "c", "formulas": "f", "merged_ranges": "mergeCell",
        "defined_names": "definedName", "styles": "xf", "drawings": "drawing",
    }
    wanted = docx_names if package_kind == "docx" else xlsx_names
    with zipfile.ZipFile(path, "r") as package:
        xml_names = [name for name in package.namelist() if name.endswith(".xml")]
        for key in wanted:
            counts[key] = 0
        for part_name in xml_names:
            try:
                root = etree.fromstring(package.read(part_name))
            except Exception:
                continue
            for key, local_name in wanted.items():
                counts[key] += len(root.xpath(f'.//*[local-name()="{local_name}"]'))
        if package_kind == "docx":
            counts["headers"] = sum(1 for name in xml_names if re.match(r"word/header\d*\.xml$", name))
            counts["footers"] = sum(1 for name in xml_names if re.match(r"word/footer\d*\.xml$", name))
        else:
            counts["worksheets"] = sum(1 for name in xml_names if name.startswith("xl/worksheets/"))
            counts["chart_parts"] = sum(1 for name in xml_names if name.startswith("xl/charts/"))
            counts["drawing_parts"] = sum(1 for name in xml_names if name.startswith("xl/drawings/"))
    return counts


def _write_zip_copy(source_path, output_path, replacements):
    with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(output_path, "w") as target:
        for info in source.infolist():
            payload = replacements.get(info.filename)
            if payload is None:
                payload = source.read(info.filename)
            target.writestr(info, payload)


def _load_lxml():
    try:
        from lxml import etree
        return etree
    except Exception as exc:
        raise RuntimeError(f"缺少 OOXML 处理依赖 lxml: {exc}")


def _xml_units_from_package(source_path: str, package_kind: str):
    etree = _load_lxml()
    trees, units = {}, []
    with zipfile.ZipFile(source_path, "r") as package:
        names = package.namelist()
        if package_kind == "docx":
            allowed = []
            for name in names:
                if not name.endswith(".xml"):
                    continue
                if name in {"word/document.xml", "word/footnotes.xml", "word/endnotes.xml", "word/comments.xml", "word/numbering.xml"}:
                    allowed.append(name)
                elif re.match(r"word/(?:header|footer)\d*\.xml$", name):
                    allowed.append(name)
                elif name.startswith(("word/diagrams/", "word/charts/", "word/glossary/")):
                    allowed.append(name)
        else:
            allowed = [
                name for name in names
                if name.endswith(".xml") and (
                    name in {"xl/sharedStrings.xml", "xl/workbook.xml", "docProps/core.xml"}
                    or name.startswith("xl/worksheets/")
                    or name.startswith("xl/comments")
                    or name.startswith("xl/drawings/")
                    or name.startswith("xl/charts/")
                    or name.startswith("xl/tables/")
                    or name.startswith("xl/pivotTables/")
                    or name.startswith("xl/pivotCache/")
                )
            ]

        for part_name in allowed:
            raw = package.read(part_name)
            try:
                root = etree.fromstring(raw)
            except Exception:
                continue
            trees[part_name] = {"root": root, "declaration": raw.lstrip().startswith(b"<?xml")}
            handled = set()
            if package_kind == "xlsx" and part_name == "xl/workbook.xml":
                for index, sheet in enumerate(root.xpath('.//*[local-name()="sheet"]')):
                    raw_value = str(sheet.get("name") or "")
                    value = raw_value.strip()
                    if not _is_translatable(value):
                        continue
                    location = f"{part_name}:sheet-name:{index}"
                    units.append({
                        "id": _unit_id("sheet", location, value),
                        "text": value,
                        "context": "Excel 工作表标签名；只翻译名称，不添加序号、说明或引号",
                        "kind": "sheet_name",
                        "sheet_name_original": value,
                        # Formula references must be rewritten against the exact
                        # stored name. Leading/trailing spaces are legal and
                        # significant in quoted Excel sheet references.
                        "sheet_name_original_raw": raw_value,
                        "refs": [{"kind": "attr", "node": sheet, "attribute": "name", "original": raw_value}],
                    })
            elif package_kind == "xlsx" and part_name == "docProps/core.xml":
                for index, title in enumerate(root.xpath('.//*[local-name()="title"]')):
                    value = str(title.text or "").strip()
                    if not _is_translatable(value):
                        continue
                    location = f"{part_name}:workbook-title:{index}"
                    units.append({
                        "id": _unit_id("booktitle", location, value),
                        "text": value,
                        "context": "Excel 工作簿文档标题属性",
                        "kind": "workbook_title",
                        "refs": [{"kind": "text", "node": title, "original": value}],
                    })
            if package_kind == "docx":
                containers = root.xpath('.//*[local-name()="p"]')
            elif part_name == "xl/sharedStrings.xml":
                containers = root.xpath('.//*[local-name()="si"]')
            elif part_name.startswith("xl/worksheets/"):
                containers = root.xpath('.//*[local-name()="is"]')
            elif part_name.startswith("xl/comments"):
                containers = root.xpath('.//*[local-name()="comment"]')
            else:
                containers = root.xpath('.//*[local-name()="p"]')

            for index, container in enumerate(containers):
                nodes = container.xpath('.//*[local-name()="t"]')
                nodes = [node for node in nodes if node.text is not None]
                if not nodes:
                    continue
                text = "".join(node.text or "" for node in nodes)
                if not _is_translatable(text):
                    continue
                for node in nodes:
                    handled.add(id(node))
                location = f"{part_name}:group:{index}"
                units.append({
                    "id": _unit_id("u", location, text),
                    "text": text,
                    "context": f"{package_kind.upper()} {part_name}",
                    "refs": [{"kind": "text", "node": node, "original": node.text or ""} for node in nodes],
                })

            for index, node in enumerate(root.xpath('.//*[local-name()="t"]')):
                if id(node) in handled or not _is_translatable(node.text or ""):
                    continue
                text = node.text or ""
                location = f"{part_name}:text:{index}"
                units.append({
                    "id": _unit_id("u", location, text),
                    "text": text,
                    "context": f"{package_kind.upper()} {part_name}",
                    "refs": [{"kind": "text", "node": node, "original": text}],
                })

            if package_kind == "xlsx" and part_name.startswith("xl/worksheets/"):
                # Formula text results use <c t="str"><f>...</f><v>cached text</v>.
                # Previewers commonly show this cache without recalculating, so
                # it must be translated along with literal cell text.
                for index, cell in enumerate(root.xpath('.//*[local-name()="c" and @t="str"]')):
                    if not cell.xpath('./*[local-name()="f"]'):
                        continue
                    values = cell.xpath('./*[local-name()="v"]')
                    if not values:
                        continue
                    value_node = values[0]
                    value = str(value_node.text or "")
                    if not _is_translatable(value):
                        continue
                    location = f"{part_name}:formula-cache:{index}"
                    units.append({
                        "id": _unit_id("formula-cache", location, value),
                        "text": value,
                        "context": (
                            f"XLSX {part_name} 公式文本缓存；翻译显示文本，"
                            "不要添加公式、单元格地址或说明"
                        ),
                        "kind": "formula_cached_text",
                        "refs": [{"kind": "text", "node": value_node, "original": value}],
                    })

            if package_kind == "docx" and part_name == "word/numbering.xml":
                for index, node in enumerate(root.xpath('.//*[local-name()="lvlText"]')):
                    attr = next((key for key in node.attrib if key.endswith("}val") or key == "val"), None)
                    value = node.get(attr) if attr else ""
                    if not attr or not _is_translatable(value):
                        continue
                    location = f"{part_name}:lvlText:{index}"
                    units.append({
                        "id": _unit_id("u", location, value),
                        "text": value,
                        "context": "DOCX numbering label",
                        "refs": [{"kind": "attr", "node": node, "attribute": attr, "original": value}],
                    })
    return etree, trees, units


def _serialize_trees(etree, trees):
    return {
        name: etree.tostring(
            data["root"],
            encoding="UTF-8",
            xml_declaration=bool(data.get("declaration")),
            standalone=None,
        )
        for name, data in trees.items()
    }


def _request_xlsx_full_recalculation(etree, trees):
    """Make Excel recalculate rewritten cross-sheet formulas on open."""
    workbook = trees.get("xl/workbook.xml")
    if not workbook:
        return False
    root = workbook["root"]
    calc_nodes = root.xpath('./*[local-name()="calcPr"]')
    if calc_nodes:
        calc_pr = calc_nodes[0]
    else:
        namespace = ""
        if isinstance(root.tag, str) and root.tag.startswith("{"):
            namespace = root.tag.split("}", 1)[0][1:]
        tag = f"{{{namespace}}}calcPr" if namespace else "calcPr"
        calc_pr = etree.SubElement(root, tag)
    calc_pr.set("calcMode", "auto")
    calc_pr.set("fullCalcOnLoad", "1")
    calc_pr.set("forceFullCalc", "1")
    return True


def _process_docx(source_path, output_path, translations):
    etree, trees, units = _xml_units_from_package(source_path, "docx")
    for unit in units:
        _apply_node_translation(unit, translations[unit["id"]])
    source_audit = _zip_audit(source_path)
    _write_zip_copy(source_path, output_path, _serialize_trees(etree, trees))
    output_audit = _zip_audit(output_path)
    warnings = []
    if source_audit["part_names"] != output_audit["part_names"]:
        warnings.append("OOXML 部件集合发生变化")
    if output_audit["bad_crc_part"]:
        raise RuntimeError(f"输出 DOCX CRC 异常: {output_audit['bad_crc_part']}")
    return units, {
        "source_parts": source_audit["parts"],
        "output_parts": output_audit["parts"],
        "translated_units": len(units),
        "structure_preserved": source_audit["part_names"] == output_audit["part_names"],
    }, warnings


def _column_number(cell_ref: str):
    letters = re.match(r"[A-Za-z]+", str(cell_ref or ""))
    value = 0
    for char in (letters.group(0).upper() if letters else ""):
        value = value * 26 + ord(char) - 64
    return value


def _display_width(value: str):
    width = 0.0
    for char in str(value or ""):
        if char == "\t":
            width += 4
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            width += 2
        elif char in "MW@%&":
            width += 1.35
        elif char in "il.,'`|":
            width += 0.55
        else:
            width += 1
    return width


def _autofit_xlsx(etree, trees):
    shared = []
    shared_tree = trees.get("xl/sharedStrings.xml")
    if shared_tree:
        for item in shared_tree["root"].xpath('.//*[local-name()="si"]'):
            shared.append("".join(node.text or "" for node in item.xpath('.//*[local-name()="t"]')))

    changed_rows = changed_cols = 0
    for part_name, tree_data in trees.items():
        if not part_name.startswith("xl/worksheets/"):
            continue
        root = tree_data["root"]
        ns_uri = root.nsmap.get(None) or "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet_data = next(iter(root.xpath('./*[local-name()="sheetData"]')), None)
        if sheet_data is None:
            continue
        col_widths, row_values = {}, {}
        for row in sheet_data.xpath('./*[local-name()="row"]'):
            row_index = int(row.get("r") or 0)
            values = []
            for cell in row.xpath('./*[local-name()="c"]'):
                if cell.xpath('./*[local-name()="f"]'):
                    continue
                ref = cell.get("r") or ""
                col = _column_number(ref)
                cell_type = cell.get("t") or ""
                value = ""
                if cell_type == "s":
                    value_node = next(iter(cell.xpath('./*[local-name()="v"]')), None)
                    try:
                        value = shared[int(value_node.text)] if value_node is not None else ""
                    except Exception:
                        value = ""
                elif cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.xpath('.//*[local-name()="t"]'))
                elif cell_type == "str":
                    continue
                if value and col:
                    measured = max((_display_width(line) for line in value.splitlines()), default=0)
                    col_widths[col] = max(col_widths.get(col, 0), measured)
                    values.append((col, value))
            if values:
                row_values[row_index] = (row, values)

        existing = {}
        cols_elements = root.xpath('./*[local-name()="cols"]')
        cols = cols_elements[0] if cols_elements else None
        if cols is not None:
            for col_node in cols.xpath('./*[local-name()="col"]'):
                start, end = int(col_node.get("min") or 1), int(col_node.get("max") or 1)
                width = float(col_node.get("width") or 0)
                for index in range(start, end + 1):
                    existing[index] = max(existing.get(index, 0), width)
        if col_widths and cols is None:
            cols = etree.Element(f"{{{ns_uri}}}cols")
            sheet_data.addprevious(cols)

        final_widths = {}
        for col_index, measured in col_widths.items():
            desired = min(60.0, max(10.0, measured + 2.2, existing.get(col_index, 0)))
            final_widths[col_index] = desired
            if desired > existing.get(col_index, 0) + 0.25:
                node = etree.SubElement(cols, f"{{{ns_uri}}}col")
                node.set("min", str(col_index))
                node.set("max", str(col_index))
                node.set("width", f"{desired:.2f}")
                node.set("customWidth", "1")
                changed_cols += 1

        for _row_index, (row, values) in row_values.items():
            max_lines = 1
            for col_index, value in values:
                width = max(8.0, final_widths.get(col_index, existing.get(col_index, 10.0)))
                lines = sum(max(1, math.ceil(_display_width(line) / width)) for line in value.splitlines() or [""])
                max_lines = max(max_lines, lines)
            desired_height = round(max(19.8, max_lines * 16.5 + 3.0), 1)
            current = float(row.get("ht") or 0)
            if desired_height > current + 0.5:
                row.set("ht", str(desired_height))
                row.set("customHeight", "1")
                changed_rows += 1
    return changed_rows, changed_cols


def _process_xlsx(source_path, output_path, translations):
    etree, trees, units = _xml_units_from_package(source_path, "xlsx")
    renamed_sheets, updated_sheet_references, _rename_map = _apply_xlsx_sheet_name_translations(
        trees, units, translations
    )
    for unit in units:
        if unit.get("kind") == "sheet_name":
            continue
        _apply_node_translation(unit, translations[unit["id"]])
    full_recalculation_requested = _request_xlsx_full_recalculation(etree, trees)
    changed_rows, changed_cols = _autofit_xlsx(etree, trees)
    source_audit = _zip_audit(source_path)
    _write_zip_copy(source_path, output_path, _serialize_trees(etree, trees))
    output_audit = _zip_audit(output_path)
    if output_audit["bad_crc_part"]:
        raise RuntimeError(f"输出 XLSX CRC 异常: {output_audit['bad_crc_part']}")
    warnings = []
    if source_audit["part_names"] != output_audit["part_names"]:
        warnings.append("XLSX 部件集合发生变化")
    return units, {
        "source_parts": source_audit["parts"],
        "output_parts": output_audit["parts"],
        "translated_units": len(units),
        "rows_autofitted": changed_rows,
        "columns_autofitted": changed_cols,
        "renamed_sheets": renamed_sheets,
        "updated_sheet_references": updated_sheet_references,
        "full_recalculation_requested": full_recalculation_requested,
        "structure_preserved": source_audit["part_names"] == output_audit["part_names"],
    }, warnings


@lru_cache(maxsize=32)
def _font_path(target_language="", bold=False, italic=False):
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    fonts = os.path.join(windir, "Fonts")
    if target_language == "zh":
        if bold:
            candidates = ["msyhbd.ttc", "msyh.ttc", "arialbd.ttf"]
        elif italic:
            candidates = ["simkai.ttf", "msyh.ttc", "ariali.ttf"]
        else:
            candidates = ["msyh.ttc", "arial.ttf", "segoeui.ttf"]
    elif bold and italic:
        candidates = ["arialbi.ttf", "arialbd.ttf", "arial.ttf", "msyhbd.ttc"]
    elif bold:
        candidates = ["arialbd.ttf", "segoeuib.ttf", "arial.ttf", "msyhbd.ttc"]
    elif italic:
        candidates = ["ariali.ttf", "segoeuii.ttf", "arial.ttf", "simkai.ttf"]
    else:
        candidates = ["arial.ttf", "segoeui.ttf", "msyh.ttc"]
    for name in candidates:
        path = os.path.join(fonts, name)
        if os.path.isfile(path):
            return path
    return ""


def _load_fitz():
    try:
        import pymupdf
        return pymupdf
    except Exception:
        try:
            import fitz
            return fitz
        except Exception as exc:
            raise RuntimeError(f"缺少 PDF 处理依赖 PyMuPDF: {exc}") from exc


def _fitz_color(value):
    try:
        integer = int(value or 0)
        return ((integer >> 16 & 255) / 255, (integer >> 8 & 255) / 255, (integer & 255) / 255)
    except Exception:
        return (0, 0, 0)


def _split_pdf_translation_into_slots(text, slots, target_font):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(slots) <= 1:
        return [value]
    tokens = re.findall(r"\S+\s*", value)
    normalized_capacities = [
        max(1.0, (float(slot["bbox"][2]) - float(slot["bbox"][0])) / max(1.0, float(slot.get("font_size") or 10)))
        for slot in slots
    ]
    try:
        widest_token = max((target_font.text_length(token, fontsize=1) for token in tokens), default=0.0)
    except Exception:
        widest_token = 0.0
    if len(tokens) < len(slots) or widest_token > max(normalized_capacities) * 1.15:
        tokens = list(value)
    if not tokens:
        return [""] * len(slots)
    try:
        weights = [max(0.01, float(target_font.text_length(token, fontsize=1))) for token in tokens]
    except Exception:
        weights = [max(0.01, float(len(token))) for token in tokens]
    output, cursor = [], 0
    for slot_index in range(len(slots) - 1):
        remaining_slots = len(slots) - slot_index
        remaining_tokens = len(tokens) - cursor
        if remaining_tokens <= 0:
            output.append("")
            continue
        if remaining_tokens <= remaining_slots:
            output.append(str(tokens[cursor]).strip())
            cursor += 1
            continue
        remaining_capacity = sum(normalized_capacities[slot_index:])
        remaining_weight = sum(weights[cursor:])
        target_weight = remaining_weight * normalized_capacities[slot_index] / max(0.01, remaining_capacity)
        end = cursor
        consumed = 0.0
        maximum_end = len(tokens) - (remaining_slots - 1)
        while end < maximum_end:
            next_weight = weights[end]
            if end > cursor and abs(consumed - target_weight) <= abs(consumed + next_weight - target_weight):
                break
            consumed += next_weight
            end += 1
        end = max(cursor + 1, end)
        output.append("".join(tokens[cursor:end]).strip())
        cursor = end
    output.append("".join(tokens[cursor:]).strip())
    return output


def _insert_pdf_fixed_slots(page, text, unit, font_variants, warnings, render_mode=0,
                            display_coordinates=False):
    """Strict native-PDF replacement: same slots, baselines, font sizes and lines."""
    slots = list(unit.get("line_slots") or [])
    default_variant = font_variants.get((False, False)) or next(iter(font_variants.values()), None)
    if not slots or not default_variant or not default_variant.get("font"):
        raise RuntimeError("PDF 原文字形缺少固定行槽信息，无法保证原格式替换")
    target_font = default_variant["font"]
    segments = _split_pdf_translation_into_slots(text, slots, target_font)
    fitz = _load_fitz()
    minimum_scale = 1.0
    for slot, segment in zip(slots, segments):
        if not segment:
            continue
        bbox = fitz.Rect(slot["bbox"])
        baseline = slot.get("baseline") or [bbox.x0, bbox.y1]
        fontsize = max(1.0, float(slot.get("font_size") or unit.get("font_size") or 10))
        flags = int(slot.get("flags") or 0)
        variant = font_variants.get((bool(flags & 16), bool(flags & 2))) or default_variant
        target_font = variant["font"]
        font_file = variant.get("path") or ""
        try:
            text_width = max(0.01, float(target_font.text_length(segment, fontsize=fontsize)))
        except Exception as exc:
            raise RuntimeError(f"无法测量 PDF 译文字宽: {exc}") from exc
        horizontal_scale = min(1.0, max(0.02, float(bbox.width) / text_width))
        metric_height = max(0.1, (float(target_font.ascender) - float(target_font.descender)) * fontsize)
        # Strict replacement never enlarges glyphs beyond the source point
        # size. A font with taller metrics may be compressed vertically, but a
        # shorter fallback font is not stretched and cannot become oversized.
        vertical_scale = min(1.0, max(0.50, float(slot.get("visual_height") or metric_height) / metric_height))
        rendered_width = text_width * horizontal_scale
        align = int(unit.get("align") or 0)
        if align == 2:
            x = float(bbox.x1) - rendered_width
        elif align == 1:
            x = (float(bbox.x0) + float(bbox.x1) - rendered_width) / 2.0
        else:
            x = float(bbox.x0)
        y = float(baseline[1])
        point = fitz.Point(x, y)
        kwargs = {
            "fontname": variant.get("alias") or ("directfont" if font_file else "helv"),
            "fontsize": fontsize,
            "color": _fitz_color(slot.get("color")),
            "morph": (point, fitz.Matrix(horizontal_scale, vertical_scale)),
            "render_mode": int(render_mode),
            "overlay": True,
        }
        if font_file:
            kwargs["fontfile"] = font_file
        if display_coordinates:
            with _pdf_display_coordinates(page) as (inverse, rotation):
                point = point * inverse
                scale = (vertical_scale, horizontal_scale) if rotation in (90, 270) else (horizontal_scale, vertical_scale)
                kwargs["morph"] = (point, fitz.Matrix(*scale))
                page.insert_text(point, segment, rotate=rotation, **kwargs)
        else:
            page.insert_text(point, segment, **kwargs)
        minimum_scale = min(minimum_scale, horizontal_scale)
    if minimum_scale < 0.55:
        warnings.append("部分 PDF 译文已在原文字行槽内水平压缩，排版未移动但建议复核可读性")
    return minimum_scale


def _pdf_ocr_zoom(page):
    """Render close to 300 DPI while capping pathological engineering pages."""
    width = max(1.0, float(page.rect.width))
    height = max(1.0, float(page.rect.height))
    zoom = max(2.5, min(4.2, 3200.0 / max(width, height)))
    pixel_budget = 32_000_000.0
    estimated = width * height * zoom * zoom
    if estimated > pixel_budget:
        zoom *= math.sqrt(pixel_budget / estimated)
    return round(max(1.5, zoom), 3)


def _rect_overlap_ratio(a, b):
    left = max(float(a[0]), float(b[0]))
    top = max(float(a[1]), float(b[1]))
    right = min(float(a[2]), float(b[2]))
    bottom = min(float(a[3]), float(b[3]))
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0:
        return 0.0
    area = max(1.0, (float(a[2]) - float(a[0])) * (float(a[3]) - float(a[1])))
    return intersection / area


def _pdf_native_text_is_usable(text_refs):
    """Return true when the page exposes a trustworthy native text layer.

    A native PDF page must not be sent through OCR merely because it also
    contains a logo, illustration or background image.  OCR is a fallback for
    pages without usable character objects (including outline-only text and
    obviously broken Unicode maps), not a second extraction pass.
    """
    text = "".join(str(item.get("text") or "") for item in text_refs)
    visible = [char for char in text if not char.isspace()]
    if not visible:
        return False
    broken = sum(
        1
        for char in visible
        if char == "\ufffd"
        or ord(char) == 0
        or 0xE000 <= ord(char) <= 0xF8FF
        or (unicodedata.category(char) == "Cc" and char not in "\t\r\n")
    )
    # A few private-use glyphs can be legitimate icon fonts.  A meaningful
    # proportion indicates a missing/invalid ToUnicode map and warrants OCR.
    return broken / max(1, len(visible)) < 0.08


def _bbox_union(rects):
    values = [list(rect) for rect in rects if rect and len(rect) >= 4]
    if not values:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(float(rect[0]) for rect in values),
        min(float(rect[1]) for rect in values),
        max(float(rect[2]) for rect in values),
        max(float(rect[3]) for rect in values),
    ]


def _weighted_pdf_font_size(span_refs, fallback=10.0):
    """Use the dominant glyph size instead of the largest span on the block."""
    weighted = []
    for ref in span_refs:
        size = max(1.0, float(ref.get("size") or fallback))
        weight = max(1, len(str(ref.get("text") or "").strip()))
        weighted.append((size, weight))
    if not weighted:
        return float(fallback)
    weighted.sort(key=lambda item: item[0])
    midpoint = sum(weight for _size, weight in weighted) / 2.0
    consumed = 0
    for size, weight in weighted:
        consumed += weight
        if consumed >= midpoint:
            return size
    return weighted[-1][0]


def _pdf_unit_typography(span_refs):
    line_map = {}
    for ref in span_refs:
        line_map.setdefault((ref["block_index"], ref["line_index"]), []).append(ref)
    line_keys = set(line_map)
    weighted_heights = []
    for ref in span_refs:
        bbox = ref.get("bbox") or [0, 0, 0, 0]
        height = max(1.0, float(bbox[3]) - float(bbox[1]))
        weighted_heights.extend([height] * max(1, len(str(ref.get("text") or "").strip())))
    weighted_heights.sort()
    visual_height = weighted_heights[len(weighted_heights) // 2] if weighted_heights else 0.0
    ordered = sorted(span_refs, key=lambda ref: (float(ref["bbox"][1]), float(ref["bbox"][0])))
    origins = [ref.get("origin") for ref in ordered if ref.get("origin") and len(ref["origin"]) >= 2]
    baseline = list(origins[0]) if len(line_keys) == 1 and origins else None
    line_slots = []
    for key, refs in sorted(line_map.items(), key=lambda item: (
        min(float(ref["bbox"][1]) for ref in item[1]),
        min(float(ref["bbox"][0]) for ref in item[1]),
    )):
        refs = sorted(refs, key=lambda ref: (float(ref["bbox"][0]), ref["span_index"]))
        bbox = _bbox_union(ref["bbox"] for ref in refs)
        slot_origins = [ref.get("origin") for ref in refs if ref.get("origin") and len(ref["origin"]) >= 2]
        slot_heights = sorted(max(1.0, float(ref["bbox"][3]) - float(ref["bbox"][1])) for ref in refs)
        dominant_ref = max(refs, key=lambda ref: max(1, len(str(ref.get("text") or "").strip())))
        line_slots.append({
            "bbox": bbox,
            "baseline": list(slot_origins[0]) if slot_origins else [bbox[0], bbox[3]],
            "font_size": _weighted_pdf_font_size(refs),
            "visual_height": slot_heights[len(slot_heights) // 2] if slot_heights else 0.0,
            "color": refs[0].get("color") or 0,
            "flags": int(dominant_ref.get("flags") or 0),
            "source_text": "".join(str(ref.get("text") or "") for ref in refs).strip(),
        })
    return {
        "line_count": max(1, len(line_keys)),
        "single_line": len(line_keys) == 1,
        "baseline": baseline,
        "source_visual_height": visual_height,
        "source_font_name": str(ordered[0].get("font") or "") if ordered else "",
        "line_slots": line_slots,
    }


def _pdf_span_text(span_refs):
    lines = {}
    for ref in span_refs:
        lines.setdefault((ref["block_index"], ref["line_index"]), []).append(ref)
    output = []
    for key in sorted(lines, key=lambda item: (
        min(float(ref["bbox"][1]) for ref in lines[item]),
        min(float(ref["bbox"][0]) for ref in lines[item]),
        item,
    )):
        spans = sorted(lines[key], key=lambda ref: (float(ref["bbox"][0]), ref["span_index"]))
        value = "".join(str(ref.get("text") or "") for ref in spans).strip()
        if value:
            output.append(value)
    return "\n".join(output).strip()


def _pdf_line_span_groups(span_refs):
    """Keep native PDF text attached to its original visual line and column.

    PDF producers frequently place every row of an invoice in one enormous
    text block.  Treating that block as one translation unit loses all column
    anchors when the translated paragraph is rendered.  PyMuPDF's line index
    is much more stable.  We preserve it and only split a line further when a
    real column gap (especially a label/amount gap) exists.
    """
    by_line = {}
    for ref in span_refs:
        by_line.setdefault((ref["block_index"], ref["line_index"]), []).append(ref)

    groups = []
    for _key, refs in sorted(by_line.items(), key=lambda item: (
        min(float(ref["bbox"][1]) for ref in item[1]),
        min(float(ref["bbox"][0]) for ref in item[1]),
        item[0],
    )):
        refs = sorted(refs, key=lambda ref: (float(ref["bbox"][0]), ref["span_index"]))
        if not any(str(ref.get("text") or "").strip() for ref in refs):
            continue
        current = []
        for ref in refs:
            if current:
                previous = current[-1]
                gap = float(ref["bbox"][0]) - float(previous["bbox"][2])
                size = max(float(ref.get("size") or 10), float(previous.get("size") or 10))
                previous_text = str(previous.get("text") or "").strip()
                current_text = str(ref.get("text") or "").strip()
                previous_numeric = bool(re.search(r"\d", previous_text)) and not any(
                    char.isalpha() for char in previous_text
                )
                current_numeric = bool(re.search(r"\d", current_text)) and not any(
                    char.isalpha() for char in current_text
                )
                label_amount_gap = (
                    previous_numeric != current_numeric
                    and gap > max(2.0, size * 0.35)
                )
                column_gap = gap > max(7.0, size * 1.55)
                if label_amount_gap or column_gap:
                    groups.append(current)
                    current = []
            current.append(ref)
        if current:
            groups.append(current)
    return groups


def _add_pdf_neighbor_context(page_units):
    ordered = sorted(page_units, key=lambda item: (
        float((item.get("content_bbox") or item.get("bbox") or [0, 0])[1]),
        float((item.get("content_bbox") or item.get("bbox") or [0, 0])[0]),
    ))
    for index, unit in enumerate(ordered):
        neighbors = []
        if index:
            neighbors.append("上文：" + re.sub(r"\s+", " ", str(ordered[index - 1].get("text") or "")).strip()[:180])
        if index + 1 < len(ordered):
            neighbors.append("下文：" + re.sub(r"\s+", " ", str(ordered[index + 1].get("text") or "")).strip()[:180])
        if neighbors:
            unit["context"] = f"{unit.get('context') or ''}；" + "；".join(neighbors)


def _pdf_table_cells(page):
    """Return detected cell rectangles without leaking PyMuPDF's advisory log."""
    try:
        with contextlib.redirect_stdout(StringIO()):
            finder = page.find_tables()
    except Exception:
        return []
    cells, seen = [], set()
    for table_index, table in enumerate(finder.tables or []):
        for row_index, row in enumerate(table.rows or []):
            for column_index, rect in enumerate(row.cells or []):
                if not rect:
                    continue
                bbox = tuple(round(float(value), 3) for value in rect)
                if bbox in seen:
                    continue
                seen.add(bbox)
                cells.append({
                    "table_index": table_index,
                    "row_index": row_index,
                    "column_index": column_index,
                    "bbox": list(bbox),
                })
    return cells


def _span_belongs_to_cell(span_bbox, cell_bbox):
    center_x = (float(span_bbox[0]) + float(span_bbox[2])) / 2.0
    center_y = (float(span_bbox[1]) + float(span_bbox[3])) / 2.0
    return (
        float(cell_bbox[0]) - 0.75 <= center_x <= float(cell_bbox[2]) + 0.75
        and float(cell_bbox[1]) - 0.75 <= center_y <= float(cell_bbox[3]) + 0.75
    )


def _assign_pdf_spans_to_cells(span_refs, cells):
    """Give each glyph span to the smallest matching table cell.

    Table detectors may return overlapping candidates for a large invoice table
    and a smaller totals table.  Greedy first-match ownership moves the totals
    into the large cell.  Specific (smallest-area) ownership is stable even
    when nested or phantom spanning cells are present.
    """
    ownership = {index: [] for index in range(len(cells))}
    for ref in span_refs:
        candidates = []
        for index, cell in enumerate(cells):
            if not _span_belongs_to_cell(ref["bbox"], cell["bbox"]):
                continue
            bbox = cell["bbox"]
            area = max(1.0, (float(bbox[2]) - float(bbox[0])) * (float(bbox[3]) - float(bbox[1])))
            overlap = _rect_overlap_ratio(ref["bbox"], bbox)
            candidates.append((area, -overlap, index))
        if candidates:
            _area, _negative_overlap, selected = min(candidates)
            ownership[selected].append(ref)
    return ownership


def _pdf_cell_span_groups(span_refs):
    """Split a detector cell into spatially connected source-text regions.

    Invoice table detection can create one phantom cell spanning a left-side
    amount-in-words note and a separate right-side totals table.  Keeping the
    original PDF block boundary first, then splitting disconnected columns,
    prevents those regions from sharing one placement anchor.
    """
    groups = []
    by_block = {}
    for ref in span_refs:
        by_block.setdefault(ref["block_index"], []).append(ref)
    for block_refs in by_block.values():
        if len(block_refs) <= 1:
            groups.append(block_refs)
            continue
        parent = list(range(len(block_refs)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def join(left, right):
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for left in range(len(block_refs)):
            a = block_refs[left]["bbox"]
            a_size = float(block_refs[left].get("size") or 10)
            for right in range(left + 1, len(block_refs)):
                b = block_refs[right]["bbox"]
                b_size = float(block_refs[right].get("size") or 10)
                size = max(a_size, b_size)
                horizontal_gap = max(0.0, max(float(a[0]), float(b[0])) - min(float(a[2]), float(b[2])))
                vertical_gap = max(0.0, max(float(a[1]), float(b[1])) - min(float(a[3]), float(b[3])))
                overlap_x = max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0])))
                overlap_y = max(0.0, min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1])))
                min_width = max(1.0, min(float(a[2]) - float(a[0]), float(b[2]) - float(b[0])))
                min_height = max(1.0, min(float(a[3]) - float(a[1]), float(b[3]) - float(b[1])))
                a_text = str(block_refs[left].get("text") or "").strip()
                b_text = str(block_refs[right].get("text") or "").strip()
                a_numeric = bool(re.search(r"\d", a_text)) and not any(char.isalpha() for char in a_text)
                b_numeric = bool(re.search(r"\d", b_text)) and not any(char.isalpha() for char in b_text)
                label_amount_pair = a_numeric != b_numeric and horizontal_gap > max(2.0, size * 0.35)
                same_line = (
                    not label_amount_pair
                    and overlap_y / min_height >= 0.45
                    and horizontal_gap <= max(24.0, size * 4.0)
                )
                same_column = overlap_x / min_width >= 0.18 and vertical_gap <= max(18.0, size * 2.8)
                if same_line or same_column:
                    join(left, right)
        components = {}
        for index, ref in enumerate(block_refs):
            components.setdefault(find(index), []).append(ref)
        groups.extend(components.values())
    return sorted(groups, key=lambda refs: (
        min(float(ref["bbox"][1]) for ref in refs),
        min(float(ref["bbox"][0]) for ref in refs),
    ))


def _pdf_cell_alignment(cell_bbox, content_bbox):
    cell_width = max(1.0, float(cell_bbox[2]) - float(cell_bbox[0]))
    left_gap = max(0.0, float(content_bbox[0]) - float(cell_bbox[0]))
    right_gap = max(0.0, float(cell_bbox[2]) - float(content_bbox[2]))
    content_width = max(1.0, float(content_bbox[2]) - float(content_bbox[0]))
    if content_width <= cell_width * 0.82 and right_gap <= max(2.0, left_gap * 0.35):
        return 2
    if content_width <= cell_width * 0.72 and abs(left_gap - right_gap) <= cell_width * 0.08:
        return 1
    return 0


def _ocr_polygon_font_size(polygon, bbox, text=""):
    """Estimate OCR font size from glyph-band thickness, not axis-aligned height.

    A slightly skewed long line can have an axis-aligned bounding box three or
    four times taller than its actual characters. Using that box height made
    scanned-PDF translations enormous. RapidOCR polygons are ordered around
    the line, so the two short side edges provide the stable text-band height.
    """
    fallback_height = max(1.0, float(bbox[3]) - float(bbox[1]))
    try:
        points = [[float(value) for value in point[:2]] for point in polygon]
        if len(points) >= 4:
            side_heights = [
                math.dist(points[0], points[3]),
                math.dist(points[1], points[2]),
            ]
            band_height = sum(side_heights) / len(side_heights)
        else:
            band_height = fallback_height
    except (TypeError, ValueError, IndexError):
        band_height = fallback_height
    band_height = min(fallback_height, max(1.0, band_height))
    # Latin OCR boxes generally measure the full ink band while the replacement
    # CJK font occupies more of its em square. A 0.62 conversion tracks the
    # source's visible cap/x-height much more closely than the former 0.78,
    # which made translated scan text roughly 20-30% too large.
    height_estimate = band_height * 0.62

    # Perspective correction can still make the short polygon edge too thick
    # on a long, skewed sentence. Independently infer the source em size from
    # the line width and glyph classes, then choose the conservative estimate.
    # This leaves bold all-caps headings governed by their real ink height while
    # bringing ordinary Latin body text back to its original 8-10pt scale.
    visual_units = 0.0
    for character in str(text or ""):
        if unicodedata.combining(character):
            continue
        if character.isspace():
            visual_units += 0.30
        elif "\u3400" <= character <= "\u9fff":
            visual_units += 1.0
        elif character in "MW@%&":
            visual_units += 0.88
        elif character in "ilI1|!.,:;'`":
            visual_units += 0.30
        elif character.isupper():
            visual_units += 0.68
        elif character.islower():
            visual_units += 0.52
        elif character.isdigit():
            visual_units += 0.56
        elif unicodedata.category(character).startswith("P"):
            visual_units += 0.32
        else:
            visual_units += 0.60
    if visual_units >= 2.5:
        try:
            points = [[float(value) for value in point[:2]] for point in polygon]
            if len(points) >= 4:
                line_width = (
                    math.dist(points[0], points[1]) + math.dist(points[3], points[2])
                ) / 2.0
            else:
                line_width = max(1.0, float(bbox[2]) - float(bbox[0]))
        except (TypeError, ValueError, IndexError):
            line_width = max(1.0, float(bbox[2]) - float(bbox[0]))
        width_estimate = line_width / visual_units
        # OCR polygons include a small horizontal safety margin at both ends;
        # using the raw width as an em measurement still overstates body text.
        height_estimate = min(height_estimate, width_estimate * 0.86)
    return max(4.25, height_estimate)


def _is_protected_ocr_mark(text):
    """Keep standalone certification/standard marks in their original artwork.

    Translating a logo to the same text is still destructive: it erases the
    designed glyphs and redraws a plain system-font imitation. Only complete,
    unambiguous visual marks are protected; surrounding descriptive text still
    becomes a normal translation unit.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if value.upper() in {
        "CE", "CCC", "ATEX", "IECEX", "ROHS", "UL", "CSA", "FCC",
        "RDC", "MDN", "UGEHK", "M2", "M²", "M3", "M³",
    } or value == "MA":
        return True
    if re.fullmatch(r"R[+-]\d{1,2}", value.upper()):
        return True
    return bool(re.fullmatch(
        r"(?:ISO|IEC|EN|NF)\s*\d{2,}(?:[-/:.]\d+)*(?:\s*[:/-]\s*\d{2,})?",
        value,
        flags=re.I,
    ))


def _normalize_ocr_page_font_sizes(units):
    """Suppress OCR box outliers without flattening genuine page headings."""
    values = sorted(
        float(item.get("font_size") or 0)
        for item in units or []
        if float(item.get("font_size") or 0) > 0
    )
    if len(values) < 4:
        return units
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0
    for item in units:
        size = float(item.get("font_size") or median)
        # Long OCR lines are body/table content and should not suddenly become
        # a display heading merely because a skewed polygon edge was noisy.
        multiplier = 1.65 if len(str(item.get("text") or "").strip()) >= 8 else 2.15
        item["font_size"] = max(4.25, min(size, max(7.5, median * multiplier)))
    return units


def _pdf_scan_preflight(doc):
    """Return lightweight page metadata when the whole PDF needs OCR.

    A fully scanned document can safely use the bounded parallel OCR path. A
    mixed/native PDF stays on the coordinate-preserving sequential path below,
    where table ownership and native glyph redaction are deliberately ordered.
    """
    pages = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        # `dict` extraction embeds full image bytes for scan pages. `words`
        # inspects only the text layer and is dramatically cheaper on hundreds
        # of image-only pages.
        words = page.get_text("words", sort=True) or []
        span_refs = [
            {"text": str(word[4] or ""), "bbox": list(word[:4])}
            for word in words
            if len(word) >= 5 and str(word[4] or "").strip()
        ]
        if _pdf_native_text_is_usable(span_refs):
            return None
        try:
            image_block_count = len(page.get_images(full=True))
        except Exception:
            image_block_count = 0
        try:
            # A normal scan page is already classified by its image XObject;
            # parsing every path object is useful only for outline/vector pages.
            drawing_count = 0 if image_block_count else len(page.get_drawings())
        except Exception:
            drawing_count = 0
        pages.append({
            "page_index": page_index,
            "width": float(page.rect.width),
            "height": float(page.rect.height),
            "rotation": int(page.rotation or 0),
            "image_blocks": image_block_count,
            "drawing_objects": drawing_count,
            # Rebuild quality remains independent from the cheaper OCR render.
            "rebuild_zoom": _pdf_ocr_zoom(page),
        })
    return pages


def _call_pooled_ocr(ocr_callback, image, engine_key, engine_threads):
    """Call the extended OCR API while supporting test/custom callbacks."""
    try:
        return ocr_callback(
            image,
            engine_key=engine_key,
            engine_threads=engine_threads,
        )
    except TypeError as exc:
        message = str(exc)
        if "engine_key" not in message and "engine_threads" not in message and "unexpected keyword" not in message:
            raise
    return ocr_callback(image)


def _ocr_failure_payload(page_index: int, exc: Exception, runtime_status=None) -> dict:
    """Build a structured OCR failure entry for the Settings error log.

    The message stays human-readable while ``details`` carries the complete
    traceback and ``engine`` records which provider/settings were active so a
    failing device can export the full diagnostic without losing context.
    """
    return {
        "page": int(page_index),
        "message": f"第 {page_index + 1} 页 OCR 失败: {exc}",
        "details": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "engine": dict(runtime_status or {}),
    }


def _extract_scanned_pdf_parallel(
    source_path,
    ocr_callback,
    should_cancel,
    artifact,
    page_progress_callback,
    page_infos,
    toc,
    requested_workers,
    checkpoint_dir=None,
    checkpoint_signature=None,
):
    """OCR a pure scanned PDF with a CPU-budgeted engine pool.

    Render directly at the selected 1400/1600/2200px OCR working side. The
    rebuild remains independent and high-resolution. Independent ONNX engines
    receive disjoint CPU budgets so page concurrency cannot oversubscribe CPU.
    """
    from services.ocr_pipeline import add_neighbor_context, recognize_ocr_regions

    fitz = _load_fitz()
    total_pages = len(page_infos)
    ocr_quality, ocr_target_longest = _ocr_quality_config(ocr_callback)
    thread_plan = pdf_ocr_thread_plan(requested_workers, total_pages=total_pages)
    worker_count = thread_plan["worker_count"]
    # Leave real headroom for the UI, PDF rasterization and OS. Allocating all
    # logical cores to ONNX made a warmed multi-page job pin the machine at
    # 100%, even though the extra contention added little useful throughput.
    cpu_thread_budget = thread_plan["cpu_thread_budget"]
    engine_threads = thread_plan["engine_threads"]
    allocated_ocr_threads = thread_plan["allocated_threads"]
    page_results = {}
    detection_zooms = {}
    preprocessing_summaries = {}
    for info in page_infos:
        page_index = int(info["page_index"])
        cached = _read_pdf_page_checkpoint(checkpoint_dir, page_index, checkpoint_signature)
        if cached is not None:
            page_results[page_index], detection_zooms[str(page_index)] = cached
    cached_pages = len(page_results)
    progress_state = {
        "current_page": 0,
        "completed_pages": cached_pages,
        "total_pages": total_pages,
        "native_pages": 0,
        "ocr_pages": total_pages,
        "ocr_completed_pages": cached_pages,
        "ocr_failed_pages": 0,
        "ocr_cached_pages": cached_pages,
        "phase": "inspect",
        "status": "starting",
        "ocr_workers": worker_count,
        "ocr_max_workers": thread_plan["max_workers"],
        "ocr_cpu_thread_budget": cpu_thread_budget,
        "ocr_allocated_threads": allocated_ocr_threads,
        "ocr_opencv_threads": 1,
        "ocr_thread_priority": "background" if os.name == "nt" else "normal",
        "ocr_peak_cpu_target_percent": PDF_OCR_PEAK_CPU_TARGET_PERCENT,
        "ocr_preprocess": "adaptive",
        "ocr_quality": ocr_quality,
        "ocr_target_longest": ocr_target_longest,
        "ocr_runtime": dict(getattr(ocr_callback, "runtime_status", {}) or {}),
    }

    def report_page_progress(**updates):
        progress_state.update(updates)
        runtime = getattr(ocr_callback, "runtime_status", None)
        if isinstance(runtime, dict):
            progress_state["ocr_runtime"] = dict(runtime)
        if page_progress_callback:
            page_progress_callback(dict(progress_state))

    report_page_progress()
    if total_pages:
        report_page_progress(
            current_page=min(total_pages, cached_pages + 1),
            phase="ocr",
            status="completed" if cached_pages >= total_pages else "processing",
        )

    slot_lock = threading.Lock()
    render_lock = threading.Lock()
    thread_slots = {}

    def worker_slot():
        thread_id = threading.get_ident()
        with slot_lock:
            if thread_id not in thread_slots:
                thread_slots[thread_id] = len(thread_slots) % max(1, worker_count)
            return thread_slots[thread_id]

    def extract_page(page_info):
        _use_windows_background_mode_for_current_thread()
        _check_pause(should_cancel, artifact)
        page_index = int(page_info["page_index"])
        # Rasterization is much shorter than OCR but can create its own CPU
        # burst. Feed pages to the parallel OCR workers through one renderer;
        # inference remains concurrent and dominates total wall time.
        with render_lock:
            # PyMuPDF document/page objects are never shared across threads.
            with fitz.open(source_path) as worker_doc:
                page = worker_doc[page_index]
                longest_points = max(1.0, float(page.rect.width), float(page.rect.height))
                detection_zoom = round(max(0.5, min(4.2, float(ocr_target_longest) / longest_points)), 4)
                pix = page.get_pixmap(matrix=fitz.Matrix(detection_zoom, detection_zoom), alpha=False)
                page_width = float(page.rect.width)
                page_height = float(page.rect.height)
        import numpy as np

        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        image = image[:, :, :3][:, :, ::-1].copy()
        slot = worker_slot()
        engine_key = f"direct-pdf-t{engine_threads}-slot{slot}"

        def pooled_ocr(image_value):
            return _call_pooled_ocr(
                ocr_callback,
                image_value,
                engine_key,
                engine_threads,
            )

        preprocessing = {}
        regions = recognize_ocr_regions(
            image,
            pooled_ocr,
            min_confidence=0.35,
            merge_dense=True,
            tile_large=False,
            target_longest=ocr_target_longest,
            # It is already rendered at the OCR working resolution; a second
            # resize would waste CPU and blur small glyphs.
            fit_longest=False,
            preprocess_document=True,
            preprocess_diagnostics=preprocessing,
        )
        scale_x = page_width / max(1, pix.width)
        scale_y = page_height / max(1, pix.height)
        page_regions = []
        for region in regions:
            copied = dict(region)
            copied["bbox"] = [
                float(region["bbox"][0]) * scale_x,
                float(region["bbox"][1]) * scale_y,
                float(region["bbox"][2]) * scale_x,
                float(region["bbox"][3]) * scale_y,
            ]
            copied["polygon"] = [
                [float(point[0]) * scale_x, float(point[1]) * scale_y]
                for point in region.get("polygon") or []
            ]
            page_regions.append(copied)
        page_regions = add_neighbor_context(page_regions, f"PDF 第 {page_index + 1} 页 OCR 区域")
        page_units = []
        for line_index, region in enumerate(page_regions):
            text = str(region.get("text") or "").strip()
            if not _is_translatable(text) or _is_protected_ocr_mark(text):
                continue
            rect = list(region["bbox"])
            location = f"pdfocr:{page_index}:{line_index}:{rect}"
            page_units.append({
                "id": _unit_id("o", location, text),
                "text": text,
                "context": (
                    f"{region.get('context') or f'PDF 第 {page_index + 1} 页 OCR 区域'}；"
                    f"识别置信度 {float(region.get('confidence') or 0):.2f}，疑似识别错误时保持参数并谨慎翻译"
                ),
                "page": page_index,
                "bbox": rect,
                "polygon": region.get("polygon") or [],
                "angle": float(region.get("angle") or 0),
                "font_size": _ocr_polygon_font_size(region.get("polygon") or [], rect, text),
                "color": 0,
                "ocr": True,
                "kind": "ocr",
                "confidence": float(region.get("confidence") or 0),
                "tiled_ocr": bool(region.get("tiled")),
            })
        _normalize_ocr_page_font_sizes(page_units)
        return page_index, page_units, detection_zoom, preprocessing

    missing_page_infos = [
        info for info in page_infos
        if int(info["page_index"]) not in page_results
    ]
    pause_requested = False
    with contextlib.ExitStack() as stack:
        stack.enter_context(_opencv_thread_budget(1))
        executor = stack.enter_context(ThreadPoolExecutor(
            max_workers=max(1, worker_count),
            thread_name_prefix="pdf-ocr",
        ))
        future_to_info = {executor.submit(extract_page, info): info for info in missing_page_infos}
        for future in as_completed(future_to_info):
            info = future_to_info[future]
            page_index = int(info["page_index"])
            terminal_page = False
            try:
                finished_index, page_units, detection_zoom, preprocessing = future.result()
                page_results[finished_index] = page_units
                detection_zooms[str(finished_index)] = detection_zoom
                preprocessing_summaries[str(finished_index)] = preprocessing
                _write_pdf_page_checkpoint(
                    checkpoint_dir,
                    finished_index,
                    checkpoint_signature,
                    page_units,
                    detection_zoom,
                )
                progress_state["ocr_completed_pages"] += 1
                terminal_page = True
            except DirectTranslationPaused:
                pause_requested = True
            except Exception as exc:
                page_results[page_index] = []
                progress_state["ocr_failed_pages"] += 1
                artifact.setdefault("warnings", []).append(f"第 {page_index + 1} 页 OCR 失败: {exc}")
                artifact.setdefault("ocr_failures", []).append(_ocr_failure_payload(
                    page_index,
                    exc,
                    getattr(ocr_callback, "runtime_status", None),
                ))
                terminal_page = True
            if terminal_page:
                progress_state["completed_pages"] += 1
                report_page_progress(
                    current_page=max(int(progress_state.get("current_page") or 0), page_index + 1),
                    phase="ocr",
                    status="completed" if progress_state["completed_pages"] >= total_pages else "processing",
                )
            if not pause_requested and should_cancel and should_cancel():
                pause_requested = True
            if pause_requested:
                for pending in future_to_info:
                    pending.cancel()
    if pause_requested:
        _set_artifact(
            artifact,
            artifact.get("stage") or "extract",
            artifact.get("progress") or 0,
            "任务已暂停，已保存完成的 OCR 页面",
            "paused",
        )
        raise DirectTranslationPaused("任务已暂停")

    units, page_refs = [], []
    for page_index in range(total_pages):
        refs = page_results.get(page_index, [])
        units.extend(refs)
        page_refs.append(refs)
    page_classifications = []
    for info in page_infos:
        is_complex = int(info.get("drawing_objects") or 0) >= 80 or float(info["width"]) >= float(info["height"]) * 1.45
        page_classifications.append({
            "page": int(info["page_index"]) + 1,
            "type": "complex" if is_complex else "scanned",
            "base_type": "scanned",
            "rotation": int(info.get("rotation") or 0),
            "image_blocks": int(info.get("image_blocks") or 0),
            "drawing_objects": int(info.get("drawing_objects") or 0),
            "native_text_usable": False,
            "extraction": "ocr",
        })
    metadata = {
        "page_count": total_pages,
        "page_sizes": [(round(float(info["width"]), 2), round(float(info["height"]), 2)) for info in page_infos],
        "toc": toc,
        "page_classifications": page_classifications,
        "ocr_zooms": {str(info["page_index"]): info["rebuild_zoom"] for info in page_infos},
        "ocr_detection_zooms": detection_zooms,
        "ocr_worker_count": worker_count,
        "ocr_max_workers": thread_plan["max_workers"],
        "ocr_engine_threads": engine_threads,
        "ocr_cpu_thread_budget": cpu_thread_budget,
        "ocr_allocated_threads": allocated_ocr_threads,
        "ocr_opencv_threads": 1,
        "ocr_thread_priority": "background" if os.name == "nt" else "normal",
        "ocr_peak_cpu_target_percent": PDF_OCR_PEAK_CPU_TARGET_PERCENT,
        "ocr_preprocess": "adaptive",
        "ocr_quality": ocr_quality,
        "ocr_target_longest": ocr_target_longest,
        "ocr_preprocess_pages": preprocessing_summaries,
        "ocr_runtime": dict(getattr(ocr_callback, "runtime_status", {}) or {}),
        "preserved_text_bboxes": {str(index): [] for index in range(total_pages)},
        "page_progress_summary": dict(progress_state),
    }
    return units, page_refs, metadata


def _extract_pdf(
    source_path, ocr_callback, should_cancel, artifact, source_language="auto",
    page_progress_callback=None, ocr_workers=1,
    page_checkpoint_dir=None, checkpoint_signature=None,
):
    from services.ocr_pipeline import add_neighbor_context, recognize_ocr_regions

    fitz = _load_fitz()
    units, page_refs, page_classifications = [], [], []
    preserved_text_bboxes = {}
    ocr_zooms = {}
    ocr_preprocess_pages = {}
    ocr_quality, ocr_target_longest = _ocr_quality_config(ocr_callback)
    doc = fitz.open(source_path)
    if doc.needs_pass:
        doc.close()
        raise RuntimeError("PDF 已加密，请先解除密码保护")
    scan_preflight = _pdf_scan_preflight(doc) if ocr_callback else None
    if scan_preflight is not None:
        toc = doc.get_toc(simple=True)
        doc.close()
        return _extract_scanned_pdf_parallel(
            source_path,
            ocr_callback,
            should_cancel,
            artifact,
            page_progress_callback,
            scan_preflight,
            toc,
            ocr_workers,
            page_checkpoint_dir,
            checkpoint_signature,
        )
    total_pages = int(doc.page_count)
    progress_state = {
        "current_page": 0,
        "completed_pages": 0,
        "total_pages": total_pages,
        "native_pages": 0,
        "ocr_pages": 0,
        "ocr_completed_pages": 0,
        "ocr_failed_pages": 0,
        "phase": "inspect",
        "status": "starting",
        "ocr_preprocess": "adaptive",
        "ocr_runtime": dict(getattr(ocr_callback, "runtime_status", {}) or {}),
    }

    def report_page_progress(**updates):
        progress_state.update(updates)
        runtime = getattr(ocr_callback, "runtime_status", None)
        if isinstance(runtime, dict):
            progress_state["ocr_runtime"] = dict(runtime)
        if page_progress_callback:
            page_progress_callback(dict(progress_state))

    report_page_progress()
    for page_index in range(doc.page_count):
        _check_pause(should_cancel, artifact)
        report_page_progress(
            current_page=page_index + 1,
            completed_pages=page_index,
            phase="inspect",
            status="processing",
        )
        page = doc[page_index]
        page_unit_start = len(units)
        blocks = page.get_text("dict", sort=True).get("blocks") or []
        image_block_count = sum(1 for block in blocks if block.get("type") == 1)
        text_refs = []
        span_refs = []
        for block_index, block in enumerate(blocks):
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block.get("lines") or []):
                for span_index, span in enumerate(line.get("spans") or []):
                    text = str(span.get("text") or "")
                    bbox = span.get("bbox")
                    if not text.strip() or not bbox:
                        continue
                    span_refs.append({
                        "key": (block_index, line_index, span_index),
                        "block_index": block_index,
                        "line_index": line_index,
                        "span_index": span_index,
                        "text": text,
                        "bbox": list(bbox),
                        "origin": list(span.get("origin") or []),
                        "size": float(span.get("size") or 10),
                        "color": span.get("color") or 0,
                        "font": span.get("font") or "",
                        "flags": int(span.get("flags") or 0),
                        "ascender": float(span.get("ascender") or 0),
                        "descender": float(span.get("descender") or 0),
                    })

        # Text extraction uses unrotated crop coordinates, while pixmaps and
        # OCR use display coordinates. Convert every native rectangle and
        # baseline before grouping lines, assigning cells or caching units.
        # Do not remove_rotation(): it can reset CropBox on landscape PDFs.
        display_matrix = page.rotation_matrix
        with _pdf_display_coordinates(page):
            detected_cells = _pdf_table_cells(page) if span_refs else []
        for ref in span_refs:
            ref["bbox"] = list(fitz.Rect(ref["bbox"]) * display_matrix)
            if ref["origin"]:
                ref["origin"] = list(fitz.Point(ref["origin"]) * display_matrix)
        for cell in detected_cells:
            cell["bbox"] = list(fitz.Rect(cell["bbox"]) * display_matrix)
        cell_ownership = _assign_pdf_spans_to_cells(span_refs, detected_cells)
        translated_span_keys = set()
        assigned_spans = {
            ref["key"] for owned_spans in cell_ownership.values() for ref in owned_spans
        }
        for cell_index, cell in enumerate(detected_cells):
            cell_spans = cell_ownership.get(cell_index) or []
            span_groups = _pdf_line_span_groups(cell_spans)
            for group_index, group_spans in enumerate(span_groups):
                text = _pdf_span_text(group_spans)
                if not _is_translatable(text):
                    continue
                font_size = _weighted_pdf_font_size(group_spans)
                content_bbox = _bbox_union(ref["bbox"] for ref in group_spans)
                location = (
                    f"pdfcell:{page_index}:{cell['table_index']}:"
                    f"{cell['row_index']}:{cell['column_index']}:{group_index}:"
                    f"{cell['bbox']}:{content_bbox}"
                )
                unit = {
                    "id": _unit_id("c", location, text),
                    "text": text,
                    "context": (
                        f"PDF 第 {page_index + 1} 页表格 {cell['table_index'] + 1}，"
                        f"第 {cell['row_index'] + 1} 行第 {cell['column_index'] + 1} 列"
                    ),
                    "page": page_index,
                    "bbox": list(cell["bbox"]),
                    "content_bbox": content_bbox,
                    "align": _pdf_cell_alignment(cell["bbox"], content_bbox),
                    "redact_bboxes": [list(ref["bbox"]) for ref in group_spans],
                    "font_size": font_size,
                    "original_font_size": font_size,
                    "color": group_spans[0].get("color") or 0,
                    "kind": "table_cell_line",
                    "table_index": cell["table_index"],
                    "row_index": cell["row_index"],
                    "column_index": cell["column_index"],
                    "cell_group_index": group_index,
                    "ocr": False,
                    **_pdf_unit_typography(group_spans),
                }
                units.append(unit)
                text_refs.append(unit)
                translated_span_keys.update(ref["key"] for ref in group_spans)

        block_indices = sorted({ref["block_index"] for ref in span_refs})
        for block_index in block_indices:
            remaining = [
                ref for ref in span_refs
                if ref["block_index"] == block_index and ref["key"] not in assigned_spans
            ]
            for line_group_index, group_spans in enumerate(_pdf_line_span_groups(remaining)):
                text = _pdf_span_text(group_spans)
                if not _is_translatable(text):
                    continue
                bbox = _bbox_union(ref["bbox"] for ref in group_spans)
                font_size = _weighted_pdf_font_size(group_spans)
                location = f"pdfline:{page_index}:{block_index}:{line_group_index}:{bbox}"
                unit = {
                    "id": _unit_id("p", location, text),
                    "text": text,
                    "context": f"PDF 第 {page_index + 1} 页原生文字行；只翻译当前行，不添加换行",
                    "page": page_index,
                    "bbox": bbox,
                    "content_bbox": bbox,
                    "redact_bboxes": [list(ref["bbox"]) for ref in group_spans],
                    "font_size": font_size,
                    "original_font_size": font_size,
                    "color": group_spans[0].get("color") or 0,
                    "kind": "pdf_native_line",
                    "ocr": False,
                    **_pdf_unit_typography(group_spans),
                }
                units.append(unit)
                text_refs.append(unit)
                translated_span_keys.update(ref["key"] for ref in group_spans)

        preserved_text_bboxes[str(page_index)] = [
            list(ref["bbox"]) for ref in span_refs if ref["key"] not in translated_span_keys
        ]

        _add_pdf_neighbor_context(text_refs)
        native_text_usable = _pdf_native_text_is_usable(span_refs)
        if text_refs and not native_text_usable:
            # Do not translate corrupt character mappings and then overlay OCR
            # on top of them.  The OCR pass becomes the sole source for page
            # text so extraction and rebuild coordinates cannot be duplicated.
            del units[page_unit_start:]
            text_refs = []
        try:
            drawing_count = len(page.get_drawings())
        except Exception:
            drawing_count = 0
        if native_text_usable:
            base_type = "text"
        else:
            base_type = "scanned"
        is_complex = drawing_count >= 80 or page.rect.width >= page.rect.height * 1.45
        page_classifications.append({
            "page": page_index + 1,
            "type": "complex" if is_complex else base_type,
            "base_type": base_type,
            "rotation": int(page.rotation or 0),
            "image_blocks": image_block_count,
            "drawing_objects": drawing_count,
            "native_text_usable": native_text_usable,
            "extraction": "native" if native_text_usable else "ocr",
        })
        # Never OCR a page with a reliable text layer.  In particular, the
        # presence of an image is not evidence that the page is scanned.
        should_ocr_page = bool(ocr_callback and not native_text_usable)
        if native_text_usable:
            progress_state["native_pages"] += 1
            report_page_progress(phase="native", status="processing")
        else:
            progress_state["ocr_pages"] += 1
            report_page_progress(phase="ocr", status="processing")
        if should_ocr_page:
            try:
                import numpy as np
                zoom = _pdf_ocr_zoom(page)
                ocr_zooms[str(page_index)] = zoom
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                image = image[:, :, :3][:, :, ::-1].copy()
                preprocessing = {}
                regions = recognize_ocr_regions(
                    image,
                    ocr_callback,
                    min_confidence=0.35,
                    merge_dense=True,
                    tile_large=base_type != "scanned",
                    target_longest=ocr_target_longest if base_type == "scanned" else max(1800, ocr_target_longest),
                    fit_longest=base_type == "scanned",
                    preprocess_document=True,
                    preprocess_diagnostics=preprocessing,
                )
                ocr_preprocess_pages[str(page_index)] = preprocessing
                scale_x = float(page.rect.width) / max(1, pix.width)
                scale_y = float(page.rect.height) / max(1, pix.height)
                page_regions = []
                digital_rects = [item["bbox"] for item in text_refs]
                for region in regions:
                    rect = [
                        float(region["bbox"][0]) * scale_x,
                        float(region["bbox"][1]) * scale_y,
                        float(region["bbox"][2]) * scale_x,
                        float(region["bbox"][3]) * scale_y,
                    ]
                    # On mixed pages OCR sees native PDF text as pixels too.  Do
                    # not create a second translation unit for the same area.
                    if any(_rect_overlap_ratio(rect, native_rect) >= 0.42 for native_rect in digital_rects):
                        continue
                    page_region = dict(region)
                    page_region["bbox"] = rect
                    page_region["polygon"] = [
                        [float(point[0]) * scale_x, float(point[1]) * scale_y]
                        for point in region.get("polygon") or []
                    ]
                    page_regions.append(page_region)
                page_regions = add_neighbor_context(page_regions, f"PDF 第 {page_index + 1} 页 OCR 区域")
                page_ocr_units = []
                for line_index, region in enumerate(page_regions):
                    text = str(region.get("text") or "").strip()
                    if not _is_translatable(text) or _is_protected_ocr_mark(text):
                        continue
                    rect = list(region["bbox"])
                    location = f"pdfocr:{page_index}:{line_index}:{rect}"
                    unit = {
                        "id": _unit_id("o", location, text),
                        "text": text,
                        "context": (
                            f"{region.get('context') or f'PDF 第 {page_index + 1} 页 OCR 区域'}；"
                            f"识别置信度 {float(region.get('confidence') or 0):.2f}，疑似识别错误时保持参数并谨慎翻译"
                        ),
                        "page": page_index,
                        "bbox": rect,
                        "polygon": region.get("polygon") or [],
                        "angle": float(region.get("angle") or 0),
                        "font_size": _ocr_polygon_font_size(region.get("polygon") or [], rect, text),
                        "color": 0,
                        "ocr": True,
                        "kind": "ocr",
                        "confidence": float(region.get("confidence") or 0),
                        "tiled_ocr": bool(region.get("tiled")),
                    }
                    page_ocr_units.append(unit)
                _normalize_ocr_page_font_sizes(page_ocr_units)
                units.extend(page_ocr_units)
                text_refs.extend(page_ocr_units)
                progress_state["ocr_completed_pages"] += 1
            except Exception as exc:
                progress_state["ocr_failed_pages"] += 1
                artifact.setdefault("warnings", []).append(f"第 {page_index + 1} 页 OCR 失败: {exc}")
                artifact.setdefault("ocr_failures", []).append(_ocr_failure_payload(
                    page_index,
                    exc,
                    getattr(ocr_callback, "runtime_status", None),
                ))
        page_refs.append(text_refs)
        report_page_progress(
            completed_pages=page_index + 1,
            phase="ocr" if not native_text_usable else "native",
            status="completed",
        )
    metadata = {
        "page_count": doc.page_count,
        "page_sizes": [(round(page.rect.width, 2), round(page.rect.height, 2)) for page in doc],
        "toc": doc.get_toc(simple=True),
        "page_classifications": page_classifications,
        "ocr_zooms": ocr_zooms,
        "ocr_preprocess": "adaptive",
        "ocr_quality": ocr_quality,
        "ocr_target_longest": ocr_target_longest,
        "ocr_preprocess_pages": ocr_preprocess_pages,
        "ocr_runtime": dict(getattr(ocr_callback, "runtime_status", {}) or {}),
        "preserved_text_bboxes": preserved_text_bboxes,
        "page_progress_summary": dict(progress_state),
    }
    doc.close()
    return units, page_refs, metadata


def _safe_xlsx_sheet_name(value, fallback, reserved):
    """Apply Excel's tab-name rules and keep names unique case-insensitively."""
    name = unicodedata.normalize("NFC", str(value or "")).strip().strip("'\"")
    name = re.sub(r"[\[\]:*?/\\]", "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = str(fallback or "Sheet").strip() or "Sheet"
    name = name[:31].rstrip() or "Sheet"
    candidate = name
    suffix = 2
    while candidate.casefold() in reserved:
        marker = f" ({suffix})"
        candidate = (name[:max(1, 31 - len(marker))].rstrip() + marker)[:31]
        suffix += 1
    reserved.add(candidate.casefold())
    return candidate


def _rewrite_xlsx_sheet_references(value, rename_map):
    """Rewrite quoted/unquoted and 3-D sheet references outside string literals."""
    text = str(value or "")
    if not text or "!" not in text or not rename_map:
        return text
    folded = {str(old).casefold(): str(new) for old, new in rename_map.items()}
    token_pattern = re.compile(
        r"(?P<token>'(?:[^']|'')+'|(?<![A-Za-z0-9_.\]])[A-Za-z0-9_.\u0080-\uffff]+(?::[A-Za-z0-9_.\u0080-\uffff]+)?)!"
    )

    def replace_token(match):
        raw = match.group("token")
        quoted = raw.startswith("'") and raw.endswith("'")
        token = raw[1:-1].replace("''", "'") if quoted else raw
        # External workbook references such as '[Book.xlsx]Sheet1' must not be
        # changed when a local sheet happens to share the same suffix.
        if "[" in token or "]" in token:
            return match.group(0)
        parts = token.split(":")
        translated = [folded.get(part.casefold(), part) for part in parts]
        if translated == parts:
            return match.group(0)
        escaped = ":".join(translated).replace("'", "''")
        return f"'{escaped}'!"

    # Formula string literals may legitimately contain the characters "Data!".
    # Split them out so only actual reference syntax is updated.
    chunks = re.split(r'("(?:[^"]|"")*")', text)
    for index in range(0, len(chunks), 2):
        chunks[index] = token_pattern.sub(replace_token, chunks[index])
    return "".join(chunks)


def _apply_xlsx_sheet_name_translations(trees, units, translations):
    workbook = trees.get("xl/workbook.xml")
    if not workbook:
        return 0, 0, {}
    all_sheets = workbook["root"].xpath('.//*[local-name()="sheet"]')
    translatable_units = [item for item in units if item.get("kind") == "sheet_name"]
    translatable_originals = {
        str(
            item.get("sheet_name_original_raw")
            if item.get("sheet_name_original_raw") is not None
            else (item.get("sheet_name_original") or item.get("text") or "")
        ).casefold()
        for item in translatable_units
    }
    reserved = {
        str(sheet.get("name") or "").casefold()
        for sheet in all_sheets
        if str(sheet.get("name") or "").casefold() not in translatable_originals
    }
    rename_map = {}
    for unit in translatable_units:
        original = str(
            unit.get("sheet_name_original_raw")
            if unit.get("sheet_name_original_raw") is not None
            else (unit.get("sheet_name_original") or unit.get("text") or "")
        )
        display_original = str(unit.get("sheet_name_original") or unit.get("text") or original).strip()
        translated = translations.get(unit["id"], display_original or original)
        safe_name = _safe_xlsx_sheet_name(translated, display_original or original, reserved)
        for ref in unit.get("refs") or []:
            if ref.get("kind") == "attr":
                ref["node"].set(ref["attribute"], safe_name)
        if safe_name != original:
            rename_map[original] = safe_name

    formula_nodes = {
        "f", "formula", "formula1", "formula2", "definedName",
        "calculatedColumnFormula", "totalsRowFormula",
    }
    reference_attributes = {"location", "refersTo", "formula", "source"}
    updated_references = 0
    if rename_map:
        for tree_data in trees.values():
            for node in tree_data["root"].iter():
                local_name = str(node.tag).rsplit("}", 1)[-1]
                if local_name in formula_nodes and node.text:
                    rewritten = _rewrite_xlsx_sheet_references(node.text, rename_map)
                    if rewritten != node.text:
                        node.text = rewritten
                        updated_references += 1
                for attribute, current in list(node.attrib.items()):
                    attribute_name = str(attribute).rsplit("}", 1)[-1]
                    if attribute_name not in reference_attributes or "!" not in str(current):
                        continue
                    rewritten = _rewrite_xlsx_sheet_references(current, rename_map)
                    if rewritten != current:
                        node.set(attribute, rewritten)
                        updated_references += 1
    return len(rename_map), updated_references, rename_map


@contextlib.contextmanager
def _pdf_display_coordinates(page):
    """Map rendered scan coordinates into the unrotated, visible crop.

    MuPDF renders /Rotate and clips CropBox against MediaBox. Insertion APIs
    expect unrotated coordinates; leaving /Rotate set also changes their crop
    translation for PDFs whose MediaBox is larger than the visible page.
    """
    fitz = _load_fitz()
    rotation = int(page.rotation or 0) % 360
    try:
        page.set_rotation(0)
        width, height = page.rect.width, page.rect.height
        inverse = {
            0: fitz.Matrix(1, 0, 0, 1, 0, 0),
            90: fitz.Matrix(0, -1, 1, 0, 0, height),
            180: fitz.Matrix(-1, 0, 0, -1, width, height),
            270: fitz.Matrix(0, 1, -1, 0, width, 0),
        }[rotation]
        yield inverse, rotation
    finally:
        page.set_rotation(rotation)


def _insert_pdf_ocr_patch(page, rect, payload):
    fitz = _load_fitz()
    with _pdf_display_coordinates(page) as (inverse, rotation):
        page.insert_image(
            fitz.Rect(rect) * inverse, stream=payload, rotate=rotation,
            keep_proportion=False, overlay=True,
        )


def _insert_pdf_search_text(page, rect, text, font_size, font_file, search_font=None,
                            display_coordinates=False):
    """Add an invisible searchable layer for raster OCR translation patches."""
    fitz = _load_fitz()
    target_rect = fitz.Rect(rect)
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or target_rect.is_empty:
        return False

    # insert_textbox() silently returns a negative spare-height value when the
    # target text does not fit. The old implementation ignored that return
    # value, so scanned output claimed to have a searchable target layer while
    # get_text() returned nothing. For an invisible OCR layer, preserve the
    # source anchor and horizontally scale one line into its source slot.
    requested_size = max(4.0, min(float(font_size or 10), max(4.0, target_rect.height * 0.92)))
    try:
        measuring_font = search_font or (
            fitz.Font(fontfile=font_file) if font_file else fitz.Font("helv")
        )
        text_width = max(0.01, float(measuring_font.text_length(value, fontsize=requested_size)))
        horizontal_scale = min(1.0, max(0.01, float(target_rect.width) / text_width))
        origin = fitz.Point(
            float(target_rect.x0),
            min(float(target_rect.y1) - 0.1, float(target_rect.y0) + requested_size * 0.88),
        )
        kwargs = {
            "fontsize": requested_size,
            "fontname": "directsearch" if font_file else "helv",
            "render_mode": 3,
            "overlay": True,
            "morph": (origin, fitz.Matrix(horizontal_scale, 1.0)),
        }
        if font_file:
            kwargs["fontfile"] = font_file
        if display_coordinates:
            with _pdf_display_coordinates(page) as (inverse, rotation):
                origin = origin * inverse
                scale = (fitz.Matrix(1.0, horizontal_scale) if rotation in (90, 270)
                         else fitz.Matrix(horizontal_scale, 1.0))
                kwargs.update(rotate=rotation, morph=(origin, scale))
                page.insert_text(origin, value, **kwargs)
        else:
            page.insert_text(origin, value, **kwargs)
    except (TypeError, ValueError, RuntimeError):
        return False
    return True


def _pdf_visual_zoom(page, dpi=200):
    zoom = float(dpi) / 72.0
    pixels = float(page.rect.width) * float(page.rect.height) * zoom * zoom
    pixel_budget = 24_000_000.0
    if pixels > pixel_budget:
        zoom *= math.sqrt(pixel_budget / pixels)
    return max(1.25, zoom)


def _pdf_native_pixel_units(page_units, scale_x, scale_y):
    visual_units, cleaning_units = [], []
    for unit in page_units:
        source_bbox = unit.get("content_bbox") or unit.get("bbox")
        if not source_bbox:
            continue
        copied = dict(unit)
        copied["bbox"] = [
            float(source_bbox[0]) * scale_x,
            float(source_bbox[1]) * scale_y,
            float(source_bbox[2]) * scale_x,
            float(source_bbox[3]) * scale_y,
        ]
        copied["font_size"] = max(5.0, float(unit.get("font_size") or 10) * (scale_x + scale_y) / 2.0)
        copied["pdf_native"] = True
        line_slots = unit.get("line_slots") or []
        copied["flags"] = int((line_slots[0] if line_slots else {}).get("flags") or 0)
        visual_units.append(copied)
        for redact_bbox in unit.get("redact_bboxes") or [source_bbox]:
            cleaning_units.append({
                "bbox": [
                    float(redact_bbox[0]) * scale_x,
                    float(redact_bbox[1]) * scale_y,
                    float(redact_bbox[2]) * scale_x,
                    float(redact_bbox[3]) * scale_y,
                ],
                "pdf_native": True,
            })
    return visual_units, cleaning_units


def _insert_pdf_visual_search_layer(page, units, translations, target_language):
    fitz = _load_fitz()
    font_file = _font_path(target_language)
    try:
        target_font = fitz.Font(fontfile=font_file) if font_file else fitz.Font("helv")
    except Exception:
        return 0
    variants = {
        (False, False): {
            "path": font_file,
            "font": target_font,
            "alias": "directsearch",
        }
    }
    inserted = 0
    for unit in units:
        value = re.sub(r"\s+", " ", str(translations.get(unit["id"]) or "")).strip()
        if not value:
            continue
        try:
            _insert_pdf_fixed_slots(page, value, unit, variants, [], render_mode=3)
            inserted += 1
        except Exception:
            continue
    return inserted


def _copy_pdf_links(source_pages, output_doc, warnings):
    for page_index, links in enumerate(source_pages):
        output_page = output_doc[page_index]
        for link in links:
            copied = {
                key: value for key, value in link.items()
                if key not in {"xref", "id"} and value is not None
            }
            try:
                output_page.insert_link(copied)
            except Exception:
                warnings.append(f"第 {page_index + 1} 页有一个链接无法恢复")


def _emit_pdf_rebuild_progress(
    progress_callback,
    phase,
    completed_pages,
    total_pages,
    percent=None,
    status="processing",
):
    if not progress_callback:
        return
    total = max(0, int(total_pages or 0))
    completed = max(0, min(total, int(completed_pages or 0))) if total else 0
    # Page work owns 0-98%; reserve 99% for compression/save and 100% for
    # reopening the result and checking page geometry/searchable text.
    calculated = int(round(98.0 * completed / total)) if total else 0
    progress_callback({
        "phase": str(phase or "pages"),
        "status": str(status or "processing"),
        "current_page": completed,
        "completed_pages": completed,
        "total_pages": total,
        "percent": max(0, min(100, int(calculated if percent is None else percent))),
    })


def _process_pdf_visual_background(
    source_path,
    output_path,
    translations,
    units,
    source_meta,
    target_language,
    progress_callback=None,
):
    """Rebuild native PDFs as cleaned visual pages plus searchable target text."""
    fitz = _load_fitz()
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"缺少 PDF 视觉重建依赖: {exc}") from exc

    source = fitz.open(source_path)
    output = fitz.open()
    total_pages = int(source.page_count)
    _emit_pdf_rebuild_progress(progress_callback, "pages", 0, total_pages, percent=0)
    warnings = []
    font_file = _font_path(target_language)
    by_page = {}
    for unit in units:
        by_page.setdefault(int(unit["page"]), []).append(unit)
    source_links = []
    fallback_background_units = 0
    overflow_units = 0
    reflowed_units = 0
    minimum_font_scale = 1.0
    minimum_horizontal_scale = 1.0
    searchable_units = 0
    visually_replaced_units = 0
    actual_dpis = []
    try:
        for page_index in range(source.page_count):
            page = source[page_index]
            source_links.append(page.get_links() or [])
            zoom = _pdf_visual_zoom(page, 200)
            actual_dpis.append(round(zoom * 72.0, 1))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True)
            pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            visual_page = Image.fromarray(pixels[:, :, :3], "RGB").convert("RGBA")
            scale_x = float(pix.width) / max(1.0, float(page.rect.width))
            scale_y = float(pix.height) / max(1.0, float(page.rect.height))
            page_units = [item for item in by_page.get(page_index, []) if not item.get("ocr")]
            changed_units = [
                item for item in page_units
                if re.sub(r"\s+", " ", str(translations.get(item["id"]) or "")).strip()
                != re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            ]
            visually_replaced_units += len(changed_units)
            visual_units, cleaning_units = _pdf_native_pixel_units(changed_units, scale_x, scale_y)
            unchanged_obstacles = [
                {
                    "bbox": [
                        float((item.get("content_bbox") or item["bbox"])[0]) * scale_x,
                        float((item.get("content_bbox") or item["bbox"])[1]) * scale_y,
                        float((item.get("content_bbox") or item["bbox"])[2]) * scale_x,
                        float((item.get("content_bbox") or item["bbox"])[3]) * scale_y,
                    ]
                }
                for item in page_units if item not in changed_units
            ]
            preserved_obstacles = [
                {
                    "bbox": [
                        float(bbox[0]) * scale_x,
                        float(bbox[1]) * scale_y,
                        float(bbox[2]) * scale_x,
                        float(bbox[3]) * scale_y,
                    ]
                }
                for bbox in (source_meta.get("preserved_text_bboxes") or {}).get(str(page_index), [])
            ]
            if visual_units:
                visual_page, page_warnings, stats = _render_translated_image(
                    visual_page,
                    translations,
                    visual_units,
                    target_language,
                    document_mode=False,
                    background_units=cleaning_units,
                    layout_obstacles=visual_units + preserved_obstacles + unchanged_obstacles,
                )
                warnings.extend(f"第 {page_index + 1} 页：{warning}" for warning in page_warnings)
                fallback_background_units += int(stats.get("fallback_background_units") or 0)
                overflow_units += int(stats.get("overflow_units") or 0)
                reflowed_units += int(stats.get("reflowed_units") or 0)
                minimum_font_scale = min(minimum_font_scale, float(stats.get("minimum_font_scale") or 1.0))
                minimum_horizontal_scale = min(
                    minimum_horizontal_scale,
                    float(stats.get("minimum_horizontal_scale") or 1.0),
                )

            buffer = BytesIO()
            visual_page.convert("RGB").save(buffer, "PNG", compress_level=6)
            output_page = output.new_page(width=float(page.rect.width), height=float(page.rect.height))
            output_page.insert_image(output_page.rect, stream=buffer.getvalue(), overlay=True)
            searchable_units += _insert_pdf_visual_search_layer(
                output_page, changed_units, translations, target_language,
            )
            _emit_pdf_rebuild_progress(
                progress_callback, "pages", page_index + 1, total_pages,
            )

        try:
            output.set_metadata(source.metadata or {})
        except Exception:
            pass
        toc = source_meta.get("toc") or []
        if toc:
            try:
                output.set_toc(toc)
            except Exception:
                warnings.append("原 PDF 书签无法完整恢复")
        _copy_pdf_links(source_links, output, warnings)
        _emit_pdf_rebuild_progress(
            progress_callback, "save", total_pages, total_pages, percent=99,
        )
        output.save(output_path, garbage=4, deflate=True, clean=True)
    finally:
        output.close()
        source.close()

    rebuilt = fitz.open(output_path)
    try:
        output_meta = {
            "page_count": rebuilt.page_count,
            "page_sizes": [(round(page.rect.width, 2), round(page.rect.height, 2)) for page in rebuilt],
        }
    finally:
        rebuilt.close()
    if output_meta["page_count"] != source_meta["page_count"] or output_meta["page_sizes"] != source_meta["page_sizes"]:
        raise RuntimeError("PDF 视觉重建后的页数或页面尺寸审计失败")
    _emit_pdf_rebuild_progress(
        progress_callback, "verify", total_pages, total_pages, percent=100, status="completed",
    )
    return {
        "page_count": source_meta["page_count"],
        "page_sizes_preserved": True,
        "translated_units": len(units),
        "visually_replaced_units": visually_replaced_units,
        "ocr_units": 0,
        "table_cells": len({
            (item.get("page"), item.get("table_index"), item.get("row_index"), item.get("column_index"))
            for item in units if str(item.get("kind") or "").startswith("table_cell")
        }),
        "oversized_native_units": 0,
        "visual_background_mode": True,
        "render_dpi": min(actual_dpis) if actual_dpis else 200,
        "vector_content_rasterized": True,
        "searchable_translation_units": searchable_units,
        "baseline_placed_units": sum(len(item.get("line_slots") or []) for item in units),
        "wrapped_native_units": reflowed_units,
        "minimum_font_scale": round(minimum_font_scale, 3),
        "minimum_horizontal_scale": round(minimum_horizontal_scale, 3),
        "overflow_units": overflow_units,
        "fallback_background_units": fallback_background_units,
        "signed_copy_warning": _pdf_has_signature(source_path),
        "page_classifications": source_meta.get("page_classifications") or [],
    }, list(dict.fromkeys(warnings))


def _render_pdf_ocr_page_impl(
    source_path,
    page_index,
    ocr_units,
    source_meta,
    translations,
    target_language,
):
    """Render and encode one OCR page without mutating the destination PDF.

    Each worker opens its own read-only PyMuPDF document.  PyMuPDF page objects
    are not shared across threads; only the much slower PIL/OpenCV background
    repair, text drawing and image encoding run concurrently.
    """
    fitz = _load_fitz()
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise RuntimeError(f"缺少 PDF 扫描页回填依赖: {exc}") from exc

    source = fitz.open(source_path)
    try:
        page = source[int(page_index)]
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        zoom = float(
            (source_meta.get("ocr_zooms") or {}).get(str(page_index))
            or _pdf_ocr_zoom(page)
        )
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        pil_page = Image.fromarray(pixels[:, :, :3], "RGB")
        scale_x = pix.width / max(1.0, page_width)
        scale_y = pix.height / max(1.0, page_height)
    finally:
        source.close()

    page_classification = next((
        item for item in (source_meta.get("page_classifications") or [])
        if int(item.get("page") or 0) == int(page_index) + 1
    ), {})
    is_scanned_page = page_classification.get("base_type") == "scanned"
    pixel_units = []
    for unit in ocr_units:
        copied = dict(unit)
        copied["bbox"] = [
            float(unit["bbox"][0]) * scale_x,
            float(unit["bbox"][1]) * scale_y,
            float(unit["bbox"][2]) * scale_x,
            float(unit["bbox"][3]) * scale_y,
        ]
        copied["polygon"] = [
            [float(point[0]) * scale_x, float(point[1]) * scale_y]
            for point in unit.get("polygon") or []
        ]
        copied["font_size"] = max(
            8.0,
            float(unit.get("font_size") or 10) * (scale_x + scale_y) / 2.0,
        )
        copied["scan_page"] = is_scanned_page
        pixel_units.append(copied)

    rendered_page, render_warnings, render_stats = _render_translated_image(
        pil_page.convert("RGBA"),
        translations,
        pixel_units,
        target_language,
        document_mode=False,
    )
    if render_stats.get("render_mode") == "document":
        patch_entries = [(render_stats.get("document_clean_bbox"), None)]
        localized_pages, document_pages = 0, 1
    elif is_scanned_page:
        patch_entries = [(_document_content_bounds(rendered_page, pixel_units), None)]
        localized_pages, document_pages = 1, 0
    else:
        patch_entries = [
            (pixel_unit.get("_render_bbox") or pixel_unit["bbox"], source_unit)
            for pixel_unit, source_unit in zip(pixel_units, ocr_units)
        ]
        localized_pages, document_pages = 1, 0

    patches = []
    for patch_box, source_unit in patch_entries:
        if not patch_box:
            continue
        x0 = max(0, int(math.floor(patch_box[0])) - 2)
        y0 = max(0, int(math.floor(patch_box[1])) - 2)
        x1 = min(rendered_page.width, int(math.ceil(patch_box[2])) + 2)
        y1 = min(rendered_page.height, int(math.ceil(patch_box[3])) + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        patch = rendered_page.crop((x0, y0, x1, y1)).convert("RGB")
        buffer = BytesIO()
        if is_scanned_page and source_unit is None:
            # The original scan remains underneath this visual replacement.
            # 4:2:2 at quality 90 keeps grayscale glyph edges effectively
            # unchanged while avoiding the 10x-15x file-size explosion caused
            # by a second full-page 4:4:4 JPEG layer.
            patch.save(buffer, "JPEG", quality=90, subsampling=1, optimize=False)
        else:
            patch.save(buffer, "PNG", optimize=True)
        patches.append({
            "rect": [x0 / scale_x, y0 / scale_y, x1 / scale_x, y1 / scale_y],
            "payload": buffer.getvalue(),
            "source_unit": source_unit,
        })

    return {
        "patches": patches,
        "search_all_units": any(source_unit is None for _box, source_unit in patch_entries),
        "warnings": render_warnings,
        "stats": render_stats,
        "localized_pages": localized_pages,
        "document_pages": document_pages,
    }


def _render_pdf_ocr_page(*args, **kwargs):
    # Two page workers plus a four-thread OpenCV inner budget use at most about
    # eight native compute threads instead of two nested machine-wide pools.
    inner_threads = max(1, min(4, (os.cpu_count() or 4) // 2))
    with _opencv_thread_budget(inner_threads):
        return _render_pdf_ocr_page_impl(*args, **kwargs)


def _process_pdf(
    source_path,
    output_path,
    translations,
    units,
    source_meta,
    target_language,
    progress_callback=None,
):
    if units and not any(item.get("ocr") for item in units):
        return _process_pdf_visual_background(
            source_path,
            output_path,
            translations,
            units,
            source_meta,
            target_language,
            progress_callback=progress_callback,
        )
    fitz = _load_fitz()
    doc = fitz.open(source_path)
    warnings = []
    font_file = _font_path(target_language)
    if not font_file:
        warnings.append("未找到可嵌入的系统字体，已使用 PDF 内置字体")
    try:
        search_font = fitz.Font(fontfile=font_file) if font_file else fitz.Font("helv")
    except Exception:
        search_font = None
    font_variants = {}
    for bold, italic, alias in [
        (False, False, "directfont"),
        (True, False, "directfont_bold"),
        (False, True, "directfont_italic"),
        (True, True, "directfont_bolditalic"),
    ]:
        variant_path = _font_path(target_language, bold=bold, italic=italic)
        try:
            variant_font = fitz.Font(fontfile=variant_path) if variant_path else fitz.Font("helv")
        except Exception:
            variant_path = font_file
            variant_font = fitz.Font(fontfile=font_file) if font_file else fitz.Font("helv")
        font_variants[(bold, italic)] = {"path": variant_path, "font": variant_font, "alias": alias}
    font_scales = []
    baseline_units = 0
    wrapped_native_units = 0
    horizontal_scales = []
    searchable_ocr_units = 0
    localized_ocr_pages = 0
    document_ocr_pages = 0
    ocr_minimum_font_scale = 1.0
    ocr_minimum_horizontal_scale = 1.0
    ocr_overflow_units = 0
    rotated_ocr_units = 0
    ocr_fallback_background_units = 0
    by_page = {}
    for unit in units:
        by_page.setdefault(unit["page"], []).append(unit)
    total_pages = int(doc.page_count)
    _emit_pdf_rebuild_progress(progress_callback, "pages", 0, total_pages, percent=0)
    ocr_page_indices = [
        page_index for page_index in range(total_pages)
        if any(item.get("ocr") for item in by_page.get(page_index, []))
    ]
    rebuild_worker_count = min(2, len(ocr_page_indices)) if (os.cpu_count() or 1) >= 6 else 1
    rebuild_executor = None
    ocr_futures = {}
    if rebuild_worker_count > 1:
        rebuild_executor = ThreadPoolExecutor(
            max_workers=rebuild_worker_count,
            thread_name_prefix="pdf-rebuild",
        )
        ocr_futures = {
            page_index: rebuild_executor.submit(
                _render_pdf_ocr_page,
                source_path,
                page_index,
                [item for item in by_page.get(page_index, []) if item.get("ocr")],
                source_meta,
                translations,
                target_language,
            )
            for page_index in ocr_page_indices
        }
    for page_index in range(total_pages):
        page_units = by_page.get(page_index, [])
        page = doc[page_index]
        digital_units = [item for item in page_units if not item.get("ocr")]
        ocr_units = [item for item in page_units if item.get("ocr")]

        # Render OCR areas before applying native-text redactions.  The image
        # renderer repairs only detected glyph pixels and preserves table lines
        # and complex backgrounds, then localized PNG patches are overlaid on
        # the original PDF so unaffected vectors stay vectors.
        if ocr_units and page_index in ocr_futures:
            try:
                rendered = ocr_futures[page_index].result()
            except Exception as exc:
                for future in ocr_futures.values():
                    future.cancel()
                if rebuild_executor is not None:
                    rebuild_executor.shutdown(wait=False, cancel_futures=True)
                    rebuild_executor = None
                raise RuntimeError(f"PDF 第 {page_index + 1} 页 OCR 译文重建失败: {exc}") from exc
            warnings.extend(rendered.get("warnings") or [])
            render_stats = rendered.get("stats") or {}
            ocr_minimum_font_scale = min(
                ocr_minimum_font_scale, float(render_stats.get("minimum_font_scale") or 1.0),
            )
            ocr_minimum_horizontal_scale = min(
                ocr_minimum_horizontal_scale,
                float(render_stats.get("minimum_horizontal_scale") or 1.0),
            )
            ocr_overflow_units += int(render_stats.get("overflow_units") or 0)
            rotated_ocr_units += int(render_stats.get("rotated_units") or 0)
            ocr_fallback_background_units += int(render_stats.get("fallback_background_units") or 0)
            localized_ocr_pages += int(rendered.get("localized_pages") or 0)
            document_ocr_pages += int(rendered.get("document_pages") or 0)
            for patch_data in rendered.get("patches") or []:
                source_unit = patch_data.get("source_unit")
                page_rect = fitz.Rect(patch_data["rect"])
                _insert_pdf_ocr_patch(page, page_rect, patch_data["payload"])
                if source_unit is None:
                    continue
                if not _insert_pdf_search_text(
                    page,
                    page_rect,
                    translations[source_unit["id"]],
                    source_unit.get("font_size") or 10,
                    font_file,
                    search_font=search_font,
                    display_coordinates=True,
                ):
                    warnings.append("当前 PDF 组件版本无法为部分扫描译文增加不可见搜索层")
                else:
                    searchable_ocr_units += 1
            if rendered.get("search_all_units"):
                for source_unit in ocr_units:
                    if not _insert_pdf_search_text(
                        page,
                        source_unit["bbox"],
                        translations[source_unit["id"]],
                        source_unit.get("font_size") or 10,
                        font_file,
                        search_font=search_font,
                        display_coordinates=True,
                    ):
                        warnings.append("当前 PDF 组件版本无法为部分扫描译文增加不可见搜索层")
                    else:
                        searchable_ocr_units += 1
            # The parallel worker fully handled the OCR page. Keep the native
            # text pass below, but skip the legacy sequential OCR branch.
            ocr_units = []

        if ocr_units:
            try:
                import numpy as np
                from PIL import Image

                zoom = float((source_meta.get("ocr_zooms") or {}).get(str(page_index)) or _pdf_ocr_zoom(page))
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                pil_page = Image.fromarray(pixels[:, :, :3], "RGB")
                scale_x = pix.width / max(1.0, float(page.rect.width))
                scale_y = pix.height / max(1.0, float(page.rect.height))
                page_classification = next((
                    item for item in (source_meta.get("page_classifications") or [])
                    if int(item.get("page") or 0) == page_index + 1
                ), {})
                is_scanned_page = page_classification.get("base_type") == "scanned"
                pixel_units = []
                for unit in ocr_units:
                    copied = dict(unit)
                    copied["bbox"] = [
                        float(unit["bbox"][0]) * scale_x,
                        float(unit["bbox"][1]) * scale_y,
                        float(unit["bbox"][2]) * scale_x,
                        float(unit["bbox"][3]) * scale_y,
                    ]
                    copied["polygon"] = [
                        [float(point[0]) * scale_x, float(point[1]) * scale_y]
                        for point in unit.get("polygon") or []
                    ]
                    copied["font_size"] = max(8.0, float(unit.get("font_size") or 10) * (scale_x + scale_y) / 2.0)
                    copied["scan_page"] = is_scanned_page
                    pixel_units.append(copied)
                # A scanned PDF page is a fixed-layout document. It must not
                # use the standalone-image "clean document" reflow: that mode
                # deliberately rebuilds paragraphs and destroys page/table
                # coordinates. Replace OCR lines at their source coordinates.
                use_document_mode = False
                rendered_page, render_warnings, render_stats = _render_translated_image(
                    pil_page.convert("RGBA"), translations, pixel_units, target_language,
                    document_mode=use_document_mode,
                )
                warnings.extend(render_warnings)
                ocr_minimum_font_scale = min(
                    ocr_minimum_font_scale, float(render_stats.get("minimum_font_scale") or 1.0),
                )
                ocr_minimum_horizontal_scale = min(
                    ocr_minimum_horizontal_scale,
                    float(render_stats.get("minimum_horizontal_scale") or 1.0),
                )
                ocr_overflow_units += int(render_stats.get("overflow_units") or 0)
                rotated_ocr_units += int(render_stats.get("rotated_units") or 0)
                ocr_fallback_background_units += int(render_stats.get("fallback_background_units") or 0)
                if render_stats.get("render_mode") == "document":
                    document_ocr_pages += 1
                    patch_entries = [(render_stats.get("document_clean_bbox"), None)]
                elif is_scanned_page:
                    localized_ocr_pages += 1
                    # Use one localized content patch instead of hundreds of
                    # per-line image objects. The rendered patch still contains
                    # the original scan; only OCR glyph regions are repaired.
                    patch_entries = [(_document_content_bounds(rendered_page, pixel_units), None)]
                else:
                    localized_ocr_pages += 1
                    patch_entries = [
                        (pixel_unit.get("_render_bbox") or pixel_unit["bbox"], source_unit)
                        for pixel_unit, source_unit in zip(pixel_units, ocr_units)
                    ]
                for patch_box, source_unit in patch_entries:
                    if not patch_box:
                        continue
                    x0 = max(0, int(math.floor(patch_box[0])) - 2)
                    y0 = max(0, int(math.floor(patch_box[1])) - 2)
                    x1 = min(rendered_page.width, int(math.ceil(patch_box[2])) + 2)
                    y1 = min(rendered_page.height, int(math.ceil(patch_box[3])) + 2)
                    if x1 <= x0 or y1 <= y0:
                        continue
                    patch = rendered_page.crop((x0, y0, x1, y1)).convert("RGB")
                    buffer = BytesIO()
                    if is_scanned_page and source_unit is None:
                        # Match the parallel page renderer: quality 90 with
                        # 4:2:2 chroma keeps scan text crisp without adding a
                        # second, enormous 4:4:4 page layer.
                        patch.save(buffer, "JPEG", quality=90, subsampling=1, optimize=False)
                    else:
                        patch.save(buffer, "PNG", optimize=True)
                    page_rect = fitz.Rect(x0 / scale_x, y0 / scale_y, x1 / scale_x, y1 / scale_y)
                    _insert_pdf_ocr_patch(page, page_rect, buffer.getvalue())
                    if source_unit is None:
                        continue
                    if not _insert_pdf_search_text(
                        page,
                        page_rect,
                        translations[source_unit["id"]],
                        source_unit.get("font_size") or 10,
                        font_file,
                        search_font=search_font,
                        display_coordinates=True,
                    ):
                        warnings.append("当前 PDF 组件版本无法为部分扫描译文增加不可见搜索层")
                    else:
                        searchable_ocr_units += 1
                if any(source_unit is None for _patch_box, source_unit in patch_entries):
                    for source_unit in ocr_units:
                        if not _insert_pdf_search_text(
                            page,
                            source_unit["bbox"],
                            translations[source_unit["id"]],
                            source_unit.get("font_size") or 10,
                            font_file,
                            search_font=search_font,
                            display_coordinates=True,
                        ):
                            warnings.append("当前 PDF 组件版本无法为部分扫描译文增加不可见搜索层")
                        else:
                            searchable_ocr_units += 1
            except Exception as exc:
                raise RuntimeError(f"PDF 第 {page_index + 1} 页 OCR 译文重建失败: {exc}") from exc

        if digital_units:
            with _pdf_display_coordinates(page) as (inverse, _rotation):
                for unit in digital_units:
                    # Delete glyphs only, retaining borders and shaded cells.
                    for redact_bbox in unit.get("redact_bboxes") or [unit["bbox"]]:
                        page.add_redact_annot(fitz.Rect(redact_bbox) * inverse, fill=None, cross_out=False)
                try:
                    page.apply_redactions(images=0, graphics=0, text=0)
                except TypeError:
                    page.apply_redactions(images=0)
        for unit in digital_units:
            translated_text = translations[unit["id"]]
            horizontal_scale = _insert_pdf_fixed_slots(
                page, translated_text, unit, font_variants, warnings,
                display_coordinates=True,
            )
            horizontal_scales.append(horizontal_scale)
            baseline_units += len(unit.get("line_slots") or [])
            # Native PDF point sizes are preserved exactly. Only the text matrix
            # may be horizontally compressed inside the original line slot.
            font_scales.append(1.0)
        _emit_pdf_rebuild_progress(
            progress_callback, "pages", page_index + 1, total_pages,
        )
    if rebuild_executor is not None:
        rebuild_executor.shutdown(wait=True)
    _emit_pdf_rebuild_progress(
        progress_callback, "save", total_pages, total_pages, percent=99,
    )
    doc.save(output_path, garbage=4, deflate=True, clean=True)
    output = fitz.open(output_path)
    output_meta = {
        "page_count": output.page_count,
        "page_sizes": [(round(page.rect.width, 2), round(page.rect.height, 2)) for page in output],
        "searchable_text_chars": sum(len(page.get_text() or "") for page in output),
    }
    output.close()
    if output_meta["page_count"] != source_meta["page_count"] or output_meta["page_sizes"] != source_meta["page_sizes"]:
        raise RuntimeError("PDF 页数或页面尺寸审计失败")
    _emit_pdf_rebuild_progress(
        progress_callback, "verify", total_pages, total_pages, percent=100, status="completed",
    )
    return {
        "page_count": source_meta["page_count"],
        "page_sizes_preserved": True,
        "translated_units": len(units),
        "ocr_units": sum(1 for item in units if item.get("ocr")),
        "tiled_ocr_units": sum(1 for item in units if item.get("tiled_ocr")),
        "searchable_translation_units": searchable_ocr_units,
        "searchable_text_chars": output_meta["searchable_text_chars"],
        "localized_ocr_pages": localized_ocr_pages,
        "document_ocr_pages": document_ocr_pages,
        "rebuild_worker_count": max(1, rebuild_worker_count),
        "table_cells": len({
            (item.get("page"), item.get("table_index"), item.get("row_index"), item.get("column_index"))
            for item in units if str(item.get("kind") or "").startswith("table_cell")
        }),
        "baseline_placed_units": baseline_units,
        "wrapped_native_units": wrapped_native_units,
        "minimum_font_scale": round(min(
            min(font_scales) if font_scales else 1.0,
            ocr_minimum_font_scale,
        ), 3),
        "minimum_horizontal_scale": round(min(
            min(horizontal_scales) if horizontal_scales else 1.0,
            ocr_minimum_horizontal_scale,
        ), 3),
        "ocr_overflow_units": ocr_overflow_units,
        "rotated_ocr_units": rotated_ocr_units,
        "fallback_background_units": ocr_fallback_background_units,
        "oversized_native_units": sum(
            1 for item in units
            if not item.get("ocr") and float(item.get("font_size") or 0) > float(item.get("original_font_size") or item.get("font_size") or 0) + 0.01
        ),
        "signed_copy_warning": _pdf_has_signature(source_path),
        "page_classifications": source_meta.get("page_classifications") or [],
    }, warnings


def _pdf_has_signature(path):
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
        return b"/Sig" in payload or b"/ByteRange" in payload
    except Exception:
        return False


def _extract_image(source_path, ocr_callback, source_language="auto"):
    from services.ocr_pipeline import add_neighbor_context, recognize_ocr_regions

    try:
        import numpy as np
        from PIL import Image, ImageOps
    except Exception as exc:
        raise RuntimeError(f"缺少图片处理依赖: {exc}")
    try:
        with Image.open(source_path) as source:
            # OCR must see the same visual orientation as the user.  JPEG raw
            # pixels are frequently sideways and corrected only by EXIF.
            display = ImageOps.exif_transpose(source)
            image = np.asarray(display.convert("RGB"), dtype=np.uint8)[:, :, ::-1].copy()
    except Exception as exc:
        raise RuntimeError(f"无法解码图片: {exc}") from exc
    _ocr_quality, ocr_target_longest = _ocr_quality_config(ocr_callback)
    # First run one complete page fitted to the selected working canvas.
    # Text-heavy document pages use this coherent
    # global pass instead of fragmenting paragraphs across high-resolution tiles.
    global_regions = recognize_ocr_regions(
        image,
        ocr_callback,
        min_confidence=0.35,
        merge_dense=True,
        tile_large=False,
        target_longest=ocr_target_longest,
        fit_longest=True,
    )
    if _is_text_document_image(display.convert("RGBA"), global_regions):
        regions = global_regions
    else:
        regions = recognize_ocr_regions(
            image,
            ocr_callback,
            min_confidence=0.35,
            merge_dense=True,
            tile_large=True,
            target_longest=ocr_target_longest,
        )
    regions = add_neighbor_context(regions, "图片 OCR 文字区域")
    units = []
    for index, region in enumerate(regions):
        text = str(region.get("text") or "").strip()
        if not _is_translatable(text) or _is_protected_ocr_mark(text):
            continue
        rect = list(region["bbox"])
        score = float(region.get("confidence") or 0)
        units.append({
            "id": _unit_id("i", f"image:{index}:{rect}", text),
            "text": text,
            "context": (
                f"{region.get('context') or '图片 OCR 文字区域'}；识别置信度 {score:.2f}，"
                "疑似识别错误时保持参数并谨慎翻译"
            ),
            "bbox": rect,
            "polygon": region.get("polygon") or [],
            "angle": float(region.get("angle") or 0),
            "font_size": _ocr_polygon_font_size(region.get("polygon") or [], rect, text),
            "confidence": score,
            "tiled_ocr": bool(region.get("tiled")),
            "kind": "ocr",
        })
    _normalize_ocr_page_font_sizes(units)
    return units


def _pil_text_wrap(draw, text, font, max_width):
    lines = []
    for paragraph in str(text or "").splitlines() or [""]:
        words = paragraph.split(" ") if " " in paragraph else list(paragraph)
        current = ""
        separator = " " if " " in paragraph else ""
        for word in words:
            # OCR labels often contain long codes/URLs without spaces.  Split a
            # token only when the complete token cannot fit even on an empty row.
            if draw.textbbox((0, 0), word, font=font)[2] > max_width:
                pieces, fragment = [], ""
                for character in word:
                    candidate_fragment = fragment + character
                    if fragment and draw.textbbox((0, 0), candidate_fragment, font=font)[2] > max_width:
                        pieces.append(fragment)
                        fragment = character
                    else:
                        fragment = candidate_fragment
                if fragment:
                    pieces.append(fragment)
                words_to_add = pieces
            else:
                words_to_add = [word]
            for word_piece in words_to_add:
                candidate = word_piece if not current else current + separator + word_piece
                bbox = draw.textbbox((0, 0), candidate, font=font)
                if current and bbox[2] - bbox[0] > max_width:
                    lines.append(current)
                    current = word_piece
                else:
                    current = candidate
        lines.append(current)
    return lines or [""]


def _unit_polygon(unit, width, height):
    points = unit.get("polygon") or []
    if len(points) < 4:
        x0, y0, x1, y1 = unit["bbox"]
        points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return [
        [min(width - 1, max(0, int(round(point[0])))), min(height - 1, max(0, int(round(point[1]))))]
        for point in points[:4]
    ]


def _scan_structural_rules(rgb, crop_box, polygon, background):
    """Detect rules beyond the glyph band so capital stems are never rules."""
    import cv2
    import numpy as np

    x0, y0, x1, y1 = crop_box
    band_height = max(1.0, min(
        math.dist(polygon[0], polygon[3]),
        math.dist(polygon[1], polygon[2]),
    ))
    # A crop containing only a line of text makes I / T / H stems look like
    # full-height table borders. Require evidence longer than a whole glyph,
    # measured in surrounding, unmodified pixels at the same render scale.
    vertical_length = max(24, int(math.ceil(band_height * 2.0)))
    horizontal_length = max(32, int(math.ceil(band_height * 3.0)))
    cx0, cy0 = max(0, x0 - horizontal_length), max(0, y0 - vertical_length)
    cx1, cy1 = min(rgb.shape[1], x1 + horizontal_length), min(rgb.shape[0], y1 + vertical_length)
    context = rgb[cy0:cy1, cx0:cx1]
    difference = np.max(np.abs(context.astype(np.int16) - background.astype(np.int16)), axis=2)
    ink = np.where(difference >= 34, 255, 0).astype(np.uint8)
    background_luma = float(np.dot(background, [0.299, 0.587, 0.114]))
    if background_luma >= 150:
        # Gray scan shadows differ from white paper too, but are not ink.
        gray = cv2.cvtColor(context, cv2.COLOR_RGB2GRAY)
        ink[gray > min(150, background_luma - 34)] = 0
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (horizontal_length, 1)),
        borderType=cv2.BORDER_CONSTANT, borderValue=0,
    )
    vertical = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, vertical_length)),
        borderType=cv2.BORDER_CONSTANT, borderValue=0,
    )
    # Solid shadow/image areas may be dark enough to pass the ink threshold.
    # Rules must also be thin across their direction of travel.
    max_thickness = max(4, int(math.ceil(band_height * 0.30)))
    for mask, transverse_axis in ((horizontal, cv2.CC_STAT_HEIGHT), (vertical, cv2.CC_STAT_WIDTH)):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        keep = np.zeros(count, dtype=np.uint8)
        for label in range(1, count):
            if stats[label, transverse_axis] <= max_thickness:
                keep[label] = 255
        mask[:] = keep[labels]
    rules = cv2.bitwise_or(horizontal, vertical)
    return rules[y0 - cy0:y1 - cy0, x0 - cx0:x1 - cx0].copy()


def _repair_image_background(image, units):
    """Inpaint detected glyph strokes instead of erasing whole OCR rectangles."""
    try:
        import cv2
        import numpy as np
    except Exception:
        # Minimal fallback for stripped/test builds.  Production bundles include
        # OpenCV and use stroke-level inpainting below.
        from PIL import ImageDraw

        repaired = image.convert("RGBA").copy()
        draw = ImageDraw.Draw(repaired)
        for unit in units:
            polygon = _unit_polygon(unit, repaired.width, repaired.height)
            x0, y0, x1, y1 = [int(round(value)) for value in unit["bbox"]]
            sample_points = [
                (max(0, min(repaired.width - 1, x0 - 2)), max(0, min(repaired.height - 1, y0 - 2))),
                (max(0, min(repaired.width - 1, x1 + 2)), max(0, min(repaired.height - 1, y0 - 2))),
                (max(0, min(repaired.width - 1, x0 - 2)), max(0, min(repaired.height - 1, y1 + 2))),
                (max(0, min(repaired.width - 1, x1 + 2)), max(0, min(repaired.height - 1, y1 + 2))),
            ]
            samples = [repaired.getpixel(point) for point in sample_points]
            fill = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*samples))
            draw.polygon([tuple(point) for point in polygon], fill=fill)
        return repaired, None, len(units)

    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
    rgb = rgba[:, :, :3]
    rule_source = rgb.copy() if any(unit.get("scan_page") for unit in units) else rgb
    pending_units = []

    # Native PDF glyph boxes and scanned-document OCR polygons usually sit on
    # a flat page/table/header color.
    # Filling those exact glyph rectangles with a border-sampled color is both
    # cleaner and more faithful than OCR-style stroke inpainting, which leaves
    # white ghosts on blue bars and black ghosts around bold text.  Text on a
    # genuinely textured/image background still falls through to inpainting.
    for unit in units:
        flat_scan_region = bool(unit.get("scan_page"))
        if not unit.get("pdf_native") and not flat_scan_region:
            pending_units.append(unit)
            continue
        polygon = np.asarray(_unit_polygon(unit, image.width, image.height), dtype=np.int32)
        polygon_width = max(1, int(polygon[:, 0].max()) - int(polygon[:, 0].min()))
        polygon_height = max(1, int(polygon[:, 1].max()) - int(polygon[:, 1].min()))
        scan_padding = max(2, min(6, int(round(min(polygon_width, polygon_height) * 0.10)))) if flat_scan_region else 0
        px0 = max(0, int(polygon[:, 0].min()) - scan_padding)
        py0 = max(0, int(polygon[:, 1].min()) - scan_padding)
        px1 = min(image.width, int(polygon[:, 0].max()) + 1 + scan_padding)
        py1 = min(image.height, int(polygon[:, 1].max()) + 1 + scan_padding)
        if px1 <= px0 or py1 <= py0:
            continue
        inside = rgb[py0:py1, px0:px1].reshape(-1, 3)
        if not inside.size:
            pending_units.append(unit)
            continue
        # The median inside a native glyph box represents its actual backdrop:
        # white on body text, blue on white title text, etc. For a scan, the
        # polygon border is a better sample of the local paper tone and avoids
        # obvious solid-white bands on slightly gray JPEG pages.
        target = rgb[py0:py1, px0:px1]
        if flat_scan_region:
            # Sample outside the detected text polygon. Sampling the polygon's
            # own border often included ascenders/descenders and selected gray
            # glyph pixels as the replacement color, leaving visible ghosts.
            border_pixels = np.concatenate((
                target[0, :, :], target[-1, :, :], target[:, 0, :], target[:, -1, :]
            ), axis=0)
            background = np.median(border_pixels, axis=0)
            background_luminance = float(
                0.299 * background[0] + 0.587 * background[1] + 0.114 * background[2]
            )
            border_variation = float(np.mean(np.std(border_pixels.astype(np.float32), axis=0)))
            paper_like = background_luminance >= 150 and border_variation <= 38
        else:
            background = np.median(inside, axis=0)
            paper_like = False
        difference = np.max(np.abs(inside.astype(np.int16) - background.astype(np.int16)), axis=1)
        uniform_ratio = float(np.mean(difference <= 24))
        if uniform_ratio < (0.34 if flat_scan_region else 0.52) and not paper_like:
            pending_units.append(unit)
            continue
        local_polygon = polygon - np.asarray([px0, py0], dtype=np.int32)
        fill_mask = np.zeros((py1 - py0, px1 - px0), dtype=np.uint8)
        cv2.fillPoly(fill_mask, [local_polygon], 255)
        if flat_scan_region:
            if paper_like:
                # Cover antialiasing fringes and mild OCR under-detection on
                # ordinary paper while the rule-preservation pass below keeps
                # table borders intact.
                fill_mask = cv2.dilate(fill_mask, np.ones((3, 3), dtype=np.uint8), iterations=1)
            # Keep long table/rule strokes crossing an OCR polygon. Feathering
            # the replacement mask matches the local scan tone without hard
            # rectangular seams or the severe propagation artifacts caused by
            # inpainting an entire long, skewed line band.
            preserve = _scan_structural_rules(
                rule_source, (px0, py0, px1, py1), polygon, background,
            )
            if cv2.countNonZero(preserve):
                preserve = cv2.dilate(preserve, np.ones((3, 3), dtype=np.uint8), iterations=1)
                fill_mask = cv2.bitwise_and(fill_mask, cv2.bitwise_not(preserve))
            feather = cv2.GaussianBlur(fill_mask, (5, 5), 0).astype(np.float32) / 255.0
            # Reconstruct a spatial backdrop instead of painting a constant
            # white band over gray paper/shadows. Closing removes glyph-sized
            # dark strokes while retaining broader scan tone variations.
            band_height = max(1.0, min(
                math.dist(polygon[0], polygon[3]), math.dist(polygon[1], polygon[2]),
            ))
            kernel_size = max(5, min(81, int(math.ceil(band_height * 0.9)) | 1))
            bx0, by0 = max(0, px0 - kernel_size), max(0, py0 - kernel_size)
            bx1, by1 = min(image.width, px1 + kernel_size), min(image.height, py1 + kernel_size)
            field = cv2.morphologyEx(
                rule_source[by0:by1, bx0:bx1], cv2.MORPH_CLOSE,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
            )
            background_pixel = field[py0 - by0:py1 - by0, px0 - bx0:px1 - bx0].astype(np.float32)
            if background_luminance < 150:
                background_pixel[:] = np.clip(np.rint(background), 0, 255)
            blended = (
                target.astype(np.float32) * (1.0 - feather[:, :, None])
                + background_pixel * feather[:, :, None]
            )
            target[:] = np.clip(np.rint(blended), 0, 255).astype(np.uint8)
        else:
            target[fill_mask > 0] = np.clip(np.rint(background), 0, 255).astype(np.uint8)

    if not pending_units:
        from PIL import Image
        return Image.fromarray(rgba, "RGBA"), None, 0

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    fallback_regions = 0

    for unit in pending_units:
        polygon = np.asarray(_unit_polygon(unit, image.width, image.height), dtype=np.int32)
        x0 = max(0, int(polygon[:, 0].min()) - 3)
        y0 = max(0, int(polygon[:, 1].min()) - 3)
        x1 = min(image.width, int(polygon[:, 0].max()) + 4)
        y1 = min(image.height, int(polygon[:, 1].max()) + 4)
        if x1 <= x0 or y1 <= y0:
            continue
        local_polygon = polygon - np.asarray([x0, y0], dtype=np.int32)
        polygon_mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        cv2.fillPoly(polygon_mask, [local_polygon], 255)
        crop_gray = gray[y0:y1, x0:x1]
        crop_rgb = rgb[y0:y1, x0:x1]
        short_side = max(3, min(crop_gray.shape[:2]))
        kernel_size = max(5, min(31, int(short_side * 0.45) | 1))
        blurred = cv2.GaussianBlur(crop_gray, (kernel_size, kernel_size), 0)
        detail = cv2.absdiff(crop_gray, blurred)
        otsu_threshold, _ = cv2.threshold(detail, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        border_pixels = np.concatenate((
            crop_rgb[0, :, :], crop_rgb[-1, :, :], crop_rgb[:, 0, :], crop_rgb[:, -1, :]
        ), axis=0)
        background = np.median(border_pixels, axis=0)
        color_difference = np.max(np.abs(crop_rgb.astype(np.int16) - background.astype(np.int16)), axis=2)
        detected_ink = np.where(
            (detail >= max(10, int(otsu_threshold * 0.72))) | (color_difference >= 30), 255, 0
        ).astype(np.uint8)
        detected_ink = cv2.bitwise_and(detected_ink, polygon_mask)
        candidate = detected_ink

        # Long table/diagram rules are not glyphs.  Remove them from the text
        # mask before inpainting so grid structure remains intact.
        crop_h, crop_w = candidate.shape
        if crop_w >= 24 and not unit.get("scan_page"):
            horizontal = cv2.morphologyEx(
                detected_ink, cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, int(crop_w * 0.52)), 1)),
            )
            candidate = cv2.bitwise_and(candidate, cv2.bitwise_not(horizontal))
        if crop_h >= 24 and not unit.get("scan_page"):
            vertical = cv2.morphologyEx(
                detected_ink, cv2.MORPH_OPEN,
                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, int(crop_h * 0.68)))),
            )
            candidate = cv2.bitwise_and(candidate, cv2.bitwise_not(vertical))

        polygon_area = max(1, int(cv2.countNonZero(polygon_mask)))
        if cv2.countNonZero(candidate) < max(4, int(polygon_area * 0.004)):
            fallback = np.where(color_difference >= 17, 255, 0).astype(np.uint8)
            candidate = cv2.bitwise_and(fallback, polygon_mask)
        if cv2.countNonZero(candidate) < max(4, int(polygon_area * 0.002)):
            # Very low-contrast text cannot be isolated safely.  A slightly
            # eroded polygon is the final fallback and is reported for QA.
            candidate = cv2.erode(polygon_mask, np.ones((3, 3), dtype=np.uint8), iterations=1)
            fallback_regions += 1

        dilation = max(1, min(3, int(round(max(1.0, unit["bbox"][3] - unit["bbox"][1]) * 0.055))))
        candidate = cv2.dilate(candidate, np.ones((dilation * 2 + 1, dilation * 2 + 1), dtype=np.uint8), iterations=1)
        if unit.get("scan_page"):
            # Apply rule protection after dilation, which otherwise grows the
            # removal mask back over the rules on textured scan backgrounds.
            rules = _scan_structural_rules(rule_source, (x0, y0, x1, y1), polygon, background)
            rules = cv2.dilate(rules, np.ones((3, 3), dtype=np.uint8), iterations=1)
            candidate = cv2.bitwise_and(candidate, cv2.bitwise_not(rules))
        mask[y0:y1, x0:x1] = cv2.bitwise_or(mask[y0:y1, x0:x1], candidate)

    if not cv2.countNonZero(mask):
        from PIL import Image
        return Image.fromarray(rgba, "RGBA"), mask, fallback_regions
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    repaired_bgr = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
    repaired_rgb = cv2.cvtColor(repaired_bgr, cv2.COLOR_BGR2RGB)
    alpha = rgba[:, :, 3]
    if int(alpha.min()) != int(alpha.max()):
        alpha = cv2.inpaint(alpha, mask, 3, cv2.INPAINT_TELEA)
    repaired = np.dstack((repaired_rgb, alpha)).astype(np.uint8)
    from PIL import Image
    return Image.fromarray(repaired, "RGBA"), mask, fallback_regions


def _document_content_bounds(image, units):
    boxes = [item.get("bbox") for item in units if item.get("bbox")]
    if not boxes:
        return [0, 0, image.width, image.height]
    heights = sorted(max(1.0, float(box[3]) - float(box[1])) for box in boxes)
    median_height = heights[len(heights) // 2]
    margin = max(10, int(round(median_height * 1.25)))
    return [
        max(0, int(math.floor(min(float(box[0]) for box in boxes))) - margin),
        max(0, int(math.floor(min(float(box[1]) for box in boxes))) - margin),
        min(image.width, int(math.ceil(max(float(box[2]) for box in boxes))) + margin),
        min(image.height, int(math.ceil(max(float(box[3]) for box in boxes))) + margin),
    ]


def _is_text_document_image(image, units):
    """Detect text-heavy paper/screenshots that need screen-style replacement."""
    if len(units) < 6:
        return False
    try:
        import cv2
        import numpy as np

        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        longest = max(rgb.shape[:2])
        if longest > 1200:
            scale = 1200.0 / longest
            rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        median_luminance = float(np.median(gray))
        median_saturation = float(np.median(hsv[:, :, 1]))
        bright_ratio = float(np.count_nonzero(gray >= 175)) / max(1, gray.size)
    except Exception:
        from PIL import ImageStat

        sample = image.convert("RGB").copy()
        sample.thumbnail((1200, 1200))
        stats = ImageStat.Stat(sample)
        median_luminance = sum(stats.mean) / 3.0
        median_saturation = 0.0
        bright_ratio = 0.7 if median_luminance >= 175 else 0.0

    bounds = _document_content_bounds(image, units)
    span_width = (bounds[2] - bounds[0]) / max(1.0, float(image.width))
    span_height = (bounds[3] - bounds[1]) / max(1.0, float(image.height))
    return bool(
        median_luminance >= 155
        and median_saturation <= 70
        and bright_ratio >= 0.52
        and span_width >= 0.38
        and span_height >= 0.30
    )


def _build_document_backdrop(image, units):
    """Build the same readable, dominant-color backdrop used by screen translation."""
    from PIL import Image, ImageFilter

    bounds = _document_content_bounds(image, units)
    x0, y0, x1, y1 = bounds
    source_crop = image.convert("RGBA").crop((x0, y0, x1, y1))
    heights = sorted(max(1.0, float(item["bbox"][3]) - float(item["bbox"][1])) for item in units)
    median_height = heights[len(heights) // 2] if heights else 12.0
    radius = max(6, min(48, int(round(median_height * 0.90))))

    try:
        import numpy as np

        sample = np.asarray(source_crop.convert("RGB").resize((96, 96)), dtype=np.uint8)
        background_rgb = tuple(int(value) for value in np.median(sample.reshape(-1, 3), axis=0))
    except Exception:
        background_rgb = (248, 248, 248)
    blurred = source_crop.convert("RGB").filter(ImageFilter.GaussianBlur(radius=radius))
    solid = Image.new("RGB", blurred.size, background_rgb)
    # Stronger than the live overlay because the saved file must not retain
    # ghost source glyphs after zooming in.
    clean_crop = Image.blend(blurred, solid, 0.90).convert("RGBA")

    # Recover only long horizontal/vertical rules (table borders, column rules,
    # outer frames).  Text strokes are intentionally not copied back.
    try:
        import cv2
        import numpy as np

        original_rgb = np.asarray(source_crop.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(original_rgb, cv2.COLOR_RGB2GRAY)
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12
        )
        crop_h, crop_w = gray.shape
        horizontal = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(60, int(crop_w * 0.10)), 1)),
        )
        vertical = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(60, int(crop_h * 0.08)))),
        )
        line_mask = cv2.bitwise_or(horizontal, vertical)
        line_mask = cv2.dilate(line_mask, np.ones((3, 3), dtype=np.uint8), iterations=1)
        clean_pixels = np.asarray(clean_crop, dtype=np.uint8).copy()
        source_pixels = np.asarray(source_crop, dtype=np.uint8)
        clean_pixels[line_mask > 0] = source_pixels[line_mask > 0]
        clean_crop = Image.fromarray(clean_pixels, "RGBA")
    except Exception:
        pass

    result = image.convert("RGBA").copy()
    # Feather the replacement boundary to avoid a visible rectangular seam.
    mask = Image.new("L", clean_crop.size, 255)
    feather = max(2, min(18, int(round(median_height * 0.35))))
    if feather > 1:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather))
    result.paste(clean_crop, (x0, y0), mask)
    return result, bounds


def _same_visual_row(a, b):
    a_box, b_box = a["bbox"], b["bbox"]
    top = max(float(a_box[1]), float(b_box[1]))
    bottom = min(float(a_box[3]), float(b_box[3]))
    intersection = max(0.0, bottom - top)
    a_height = max(1.0, float(a_box[3]) - float(a_box[1]))
    b_height = max(1.0, float(b_box[3]) - float(b_box[1]))
    return intersection >= min(a_height, b_height) * 0.35


def _layout_document_units(image, units, translations, font_file, content_bounds):
    """Port the screen overlay's width allocation and collision-down layout."""
    from PIL import ImageDraw

    frame_left, frame_top, frame_right, frame_bottom = [float(value) for value in content_bounds]
    ordered = sorted(units, key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0])))
    measure_draw = ImageDraw.Draw(image)

    def build_layout(font_scale):
        placed = []
        overflow = False
        for unit in ordered:
            box = [float(value) for value in unit["bbox"]]
            left = max(frame_left, min(box[0], frame_right - 8))
            origin_top = max(frame_top, min(box[1], frame_bottom - 8))
            source_height = max(8.0, box[3] - box[1])
            right_limit = frame_right
            for other in ordered:
                if other is unit:
                    continue
                other_box = other["bbox"]
                if float(other_box[0]) > left + 2 and _same_visual_row(unit, other):
                    right_limit = min(right_limit, float(other_box[0]) - max(4.0, source_height * 0.25))
            max_width = max(24, int(right_limit - left))
            preferred = max(10, int(round(float(unit.get("font_size") or source_height * 0.75) * font_scale)))
            font = _load_font(font_file, preferred)
            lines = _pil_text_wrap(measure_draw, translations[unit["id"]], font, max_width)
            metrics = measure_draw.textbbox((0, 0), "Ag国", font=font)
            line_height = max(8, metrics[3] - metrics[1] + max(2, int(preferred * 0.10)))
            block_height = max(line_height, line_height * len(lines))
            text_width = min(max_width, max((measure_draw.textbbox((0, 0), line, font=font)[2] for line in lines), default=0))
            top = origin_top

            attempts = 0
            while attempts < 300:
                blocker = next((
                    item for item in placed
                    if not (
                        top + block_height + 2 <= item["top"]
                        or item["top"] + item["height"] + 2 <= top
                        or left + text_width + 2 <= item["left"]
                        or item["left"] + item["text_width"] + 2 <= left
                    )
                ), None)
                if blocker is None:
                    break
                top = blocker["top"] + blocker["height"] + 2
                attempts += 1
            if top + block_height > frame_bottom:
                overflow = True
            placed.append({
                "unit": unit,
                "left": left,
                "top": top,
                "width": max_width,
                "text_width": text_width,
                "height": block_height,
                "font": font,
                "font_size": preferred,
                "line_height": line_height,
                "lines": lines,
            })
        return placed, overflow

    layout = []
    overflow = True
    used_scale = 1.0
    for scale in (1.0, 0.94, 0.88, 0.82, 0.76, 0.70):
        candidate, candidate_overflow = build_layout(scale)
        layout, overflow, used_scale = candidate, candidate_overflow, scale
        if not overflow:
            break
    if overflow:
        # Keep every line visible within the fixed canvas.  This can compress
        # the final gap but never writes beyond the image boundary.
        excess = max((item["top"] + item["height"] - frame_bottom for item in layout), default=0.0)
        if excess > 0 and layout:
            total_range = max(1.0, max(item["top"] for item in layout) - frame_top)
            for item in layout:
                relative = max(0.0, item["top"] - frame_top) / total_range
                item["top"] = max(frame_top, item["top"] - excess * relative)
    return layout, overflow, used_scale


def _render_document_translations(image, translations, units, target_language):
    from PIL import ImageDraw

    backdrop, content_bounds = _build_document_backdrop(image, units)
    font_file = _font_path(target_language)
    layout, overflow, used_scale = _layout_document_units(
        backdrop, units, translations, font_file, content_bounds
    )
    draw = ImageDraw.Draw(backdrop)
    try:
        import numpy as np
        backdrop_rgb = np.asarray(backdrop.convert("RGB"), dtype=np.uint8)
    except Exception:
        backdrop_rgb = None
    for item in layout:
        text_fill, stroke_fill = _local_text_colors(backdrop, [
            item["left"], item["top"], item["left"] + item["width"], item["top"] + item["height"]
        ], rgb_array=backdrop_rgb)
        stroke_width = max(1, min(2, int(item["font_size"] / 14)))
        for line_index, line in enumerate(item["lines"]):
            draw.text(
                (item["left"], item["top"] + line_index * item["line_height"]),
                line,
                font=item["font"],
                fill=text_fill,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
        item["unit"]["_render_bbox"] = [
            item["left"], item["top"],
            min(backdrop.width, item["left"] + max(item["width"], item["text_width"] + 2)),
            min(backdrop.height, item["top"] + item["height"] + 2),
        ]
    warnings = []
    if overflow:
        warnings.append("文档页译文接近页面底部容量，已全局压缩间距并建议复核")
    return backdrop, warnings, {
        "render_mode": "document",
        "document_clean_bbox": content_bounds,
        "layout_scale": used_scale,
        "overflow_units": 1 if overflow else 0,
        "rotated_units": 0,
        "fallback_background_units": 0,
        "background_repaired": True,
    }


def _area_is_clear(image, box):
    try:
        import cv2
        import numpy as np
        x0, y0, x1, y1 = [int(round(value)) for value in box]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(image.width, x1), min(image.height, y1)
        if x1 <= x0 or y1 <= y0:
            return False
        crop = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        edge_ratio = float(np.count_nonzero(cv2.Canny(gray, 60, 160))) / max(1, gray.size)
        return float(gray.std()) <= 34.0 and edge_ratio <= 0.10
    except Exception:
        try:
            from PIL import ImageStat
            x0, y0, x1, y1 = [int(round(value)) for value in box]
            crop = image.convert("RGB").crop((max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1)))
            return bool(crop.width and crop.height and max(ImageStat.Stat(crop).stddev or [999]) <= 34.0)
        except Exception:
            return False


def _available_text_box(image, unit, units):
    x0, y0, x1, y1 = [float(value) for value in unit["bbox"]]
    original_width = max(4.0, x1 - x0)
    original_height = max(4.0, y1 - y0)
    right_limit = float(image.width)
    bottom_limit = float(image.height)
    for other in units:
        if other is unit:
            continue
        ox0, oy0, ox1, oy1 = [float(value) for value in other["bbox"]]
        vertical_overlap = max(0.0, min(y1, oy1) - max(y0, oy0))
        horizontal_overlap = max(0.0, min(x1, ox1) - max(x0, ox0))
        if ox0 >= x1 - 1 and vertical_overlap >= min(original_height, max(1.0, oy1 - oy0)) * 0.30:
            right_limit = min(right_limit, ox0 - 3)
        if oy0 >= y1 - 1 and horizontal_overlap >= min(original_width, max(1.0, ox1 - ox0)) * 0.20:
            bottom_limit = min(bottom_limit, oy0 - 2)
    expanded_right = min(right_limit, x0 + original_width * 2.4)
    expanded_bottom = min(bottom_limit, y0 + original_height * 3.2)
    candidate = [x0, y0, max(x1, expanded_right), max(y1, expanded_bottom)]
    added_regions = []
    if candidate[2] > x1 + 2:
        added_regions.append([x1, y0, candidate[2], candidate[3]])
    if candidate[3] > y1 + 2:
        added_regions.append([x0, y1, x1, candidate[3]])
    if added_regions and not all(_area_is_clear(image, area) for area in added_regions):
        return [x0, y0, x1, y1]
    return candidate


def _load_font(font_file, size):
    from PIL import ImageFont
    cache = getattr(_FONT_CACHE_LOCAL, "fonts", None)
    if cache is None:
        cache = {}
        _FONT_CACHE_LOCAL.fonts = cache
    key = (str(font_file or ""), max(1, int(size)))
    if key in cache:
        return cache[key]
    try:
        font = ImageFont.truetype(font_file, key[1]) if font_file else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    if len(cache) >= 128:
        cache.clear()
    cache[key] = font
    return font


def _fit_image_text(
    draw, text, font_file, preferred_size, minimum_size, original_box, expanded_box,
    single_line=False,
):
    def layout(size, box):
        font = _load_font(font_file, size)
        width = max(4, int(box[2] - box[0]))
        if single_line:
            lines = [re.sub(r"\s+", " ", str(text or "")).strip()]
        else:
            lines = _pil_text_wrap(draw, text, font, width)
        metrics = draw.textbbox((0, 0), "Ag国", font=font)
        line_height = max(8, metrics[3] - metrics[1] + max(2, int(size * 0.12)))
        required_height = line_height * len(lines)
        required_width = max((draw.textbbox((0, 0), line, font=font)[2] for line in lines), default=0)
        return font, lines, line_height, required_width, required_height

    preferred_size = max(minimum_size, int(round(preferred_size)))
    for box in (original_box, expanded_box):
        result = layout(preferred_size, box)
        if result[3] <= box[2] - box[0] and result[4] <= box[3] - box[1]:
            return box, result, False
    for size in range(preferred_size - 1, minimum_size - 1, -1):
        result = layout(size, expanded_box)
        if result[3] <= expanded_box[2] - expanded_box[0] and result[4] <= expanded_box[3] - expanded_box[1]:
            return expanded_box, result, False
    return expanded_box, layout(minimum_size, expanded_box), True


def _local_text_colors(image, box, rgb_array=None):
    try:
        import numpy as np
        x0, y0, x1, y1 = [int(round(value)) for value in box]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(image.width, x1), min(image.height, y1)
        if rgb_array is not None:
            crop = rgb_array[y0:y1, x0:x1]
        else:
            crop = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB"), dtype=np.uint8)
        rgb = np.median(crop.reshape(-1, 3), axis=0) if crop.size else np.asarray([255, 255, 255])
        luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        return ((18, 24, 36, 255), (248, 250, 252, 230)) if luminance >= 145 else ((248, 250, 252, 255), (4, 10, 20, 220))
    except Exception:
        return (20, 20, 20, 255), (255, 255, 255, 220)


def _render_translated_image(
    image, translations, units, target_language, document_mode=None,
    background_units=None, layout_obstacles=None,
):
    from PIL import Image, ImageDraw

    if document_mode is None:
        document_mode = _is_text_document_image(image, units)
    if document_mode:
        return _render_document_translations(image, translations, units, target_language)

    changed_units = [
        unit for unit in units
        if re.sub(r"\s+", " ", str(translations[unit["id"]])).strip()
        != re.sub(r"\s+", " ", str(unit.get("text") or "")).strip()
    ]
    # Unchanged acronyms/logos retain their original artwork and color. Keep
    # them in `units` below as layout obstacles for neighboring translations.
    repaired, _mask, fallback_regions = _repair_image_background(
        image, background_units if background_units is not None else changed_units,
    )
    draw = ImageDraw.Draw(repaired)
    try:
        import numpy as np
        repaired_rgb = np.asarray(repaired.convert("RGB"), dtype=np.uint8)
    except Exception:
        repaired_rgb = None
    warnings = []
    overflow_count = 0
    rotated_count = 0
    reflowed_count = 0
    minimum_font_scale = 1.0
    minimum_horizontal_scale = 1.0
    for unit in changed_units:
        fixed_slot = bool(unit.get("pdf_native") or unit.get("kind") == "ocr")
        source_box = [float(value) for value in unit["bbox"]]
        original_box = [
            max(0.0, source_box[0]), max(0.0, source_box[1]),
            min(float(repaired.width), max(source_box[0] + 4.0, source_box[2])),
            min(float(repaired.height), max(source_box[1] + 4.0, source_box[3])),
        ]
        angle = float(unit.get("angle") or 0)
        # Scanned text commonly has a 1-3 degree page skew. For long lines that
        # small angle materially changes the axis-aligned box height and causes
        # collisions, so preserve it. Free-form image labels retain the more
        # conservative threshold.
        rotated = abs(angle) >= (0.6 if unit.get("scan_page") else 3.0)
        expanded_box = original_box if rotated else _available_text_box(
            repaired, unit, layout_obstacles or units,
        )
        slot_width = None
        slot_height = None
        if rotated:
            polygon = unit.get("polygon") or []
            try:
                slot_width = (
                    math.dist(polygon[0], polygon[1]) + math.dist(polygon[3], polygon[2])
                ) / 2.0
                slot_height = (
                    math.dist(polygon[0], polygon[3]) + math.dist(polygon[1], polygon[2])
                ) / 2.0
            except (TypeError, ValueError, IndexError):
                slot_width = max(4.0, original_box[2] - original_box[0])
                slot_height = max(4.0, original_box[3] - original_box[1])
            slot_width = max(4.0, slot_width)
            slot_height = max(4.0, slot_height)

        is_ocr_slot = unit.get("kind") == "ocr"
        preferred = max(5 if is_ocr_slot else 8, int(round(float(unit.get("font_size") or 10))))
        minimum = max(4 if is_ocr_slot else 7, int(math.floor(preferred * 0.70)))
        flags = int(unit.get("flags") or 0)
        font_file = _font_path(target_language, bold=bool(flags & 16), italic=bool(flags & 2))
        fit_original_box = original_box
        fit_expanded_box = expanded_box
        if rotated and fixed_slot:
            # Fit against the true quadrilateral edge lengths, not its inflated
            # axis-aligned bounding box. Otherwise a 2° long line appears to
            # have several rows of height and accepts a grossly oversized font.
            fit_original_box = [
                original_box[0], original_box[1],
                min(float(repaired.width), original_box[0] + slot_width),
                min(float(repaired.height), original_box[1] + slot_height),
            ]
            fit_expanded_box = fit_original_box
        selected_box, layout, overflow = _fit_image_text(
            draw,
            translations[unit["id"]],
            font_file,
            preferred,
            minimum,
            fit_original_box,
            fit_expanded_box,
            single_line=fixed_slot,
        )
        font, lines, line_height, required_width, required_height = layout
        if len(lines) != max(1, int(unit.get("line_count") or 1)):
            reflowed_count += 1
        minimum_font_scale = min(
            minimum_font_scale,
            float(getattr(font, "size", preferred) or preferred) / max(1.0, float(preferred)),
        )
        text_fill, stroke_fill = _local_text_colors(
            repaired, selected_box, rgb_array=repaired_rgb,
        )
        # A white outline improves free-form photo labels, but on a scanned
        # document it makes every glyph visibly heavier than the source and
        # blurs small table text.
        stroke_width = 0 if unit.get("pdf_native") or unit.get("scan_page") else max(1, min(2, int(preferred / 12)))
        horizontal_scale = 1.0
        if overflow:
            overflow_count += 1
            if fixed_slot:
                # Native PDF lines are fixed layout slots.  Do not turn one
                # source line into a paragraph or let it collide with the next
                # column.  A final horizontal text-matrix style compression is
                # safer than moving the layout; the QA report still flags very
                # aggressive compression for manual review.
                horizontal_scale = min(
                    1.0,
                    max(0.01, float(selected_box[2] - selected_box[0]) / max(1.0, float(required_width))),
                )
                minimum_horizontal_scale = min(minimum_horizontal_scale, horizontal_scale)
            else:
                # Never silently crop OCR/image text: use remaining page space
                # and mark the area for visual review.
                selected_box = list(selected_box)
                selected_box[2] = min(repaired.width, max(selected_box[2], selected_box[0] + required_width))
                selected_box[3] = min(repaired.height, max(selected_box[3], selected_box[1] + required_height))
                if selected_box[3] - selected_box[1] < required_height:
                    selected_box[1] = max(0, selected_box[3] - required_height)

        if rotated:
            rotated_count += 1
            if fixed_slot and required_width > slot_width:
                horizontal_scale = min(horizontal_scale, slot_width / max(1.0, required_width))
                minimum_horizontal_scale = min(minimum_horizontal_scale, horizontal_scale)

            # Build an unrotated text band using the actual quadrilateral edge
            # lengths, then rotate the band into the polygon's axis-aligned
            # bounds. Using the already-inflated bounding-box height here used
            # to shift neighboring skewed lines into each other.
            text_layer = Image.new(
                "RGBA",
                (max(4, int(math.ceil(required_width + 4))), max(4, int(math.ceil(required_height + 4)))),
                (0, 0, 0, 0),
            )
            layer_draw = ImageDraw.Draw(text_layer)
            for line_index, line in enumerate(lines):
                layer_draw.text(
                    (1, 1 + line_index * line_height), line, font=font, fill=text_fill,
                    stroke_width=stroke_width, stroke_fill=stroke_fill,
                )
            if horizontal_scale < 0.999:
                text_layer = text_layer.resize(
                    (max(1, int(round(text_layer.width * horizontal_scale))), text_layer.height),
                    Image.Resampling.LANCZOS,
                )
            band = Image.new(
                "RGBA",
                (max(4, int(math.ceil(slot_width + 2))), max(4, int(math.ceil(slot_height + 2)))),
                (0, 0, 0, 0),
            )
            align = int(unit.get("align") or 0)
            if align == 2:
                band_x = max(0, band.width - text_layer.width)
            elif align == 1:
                band_x = max(0, int(round((band.width - text_layer.width) / 2.0)))
            else:
                band_x = 0
            band.alpha_composite(text_layer, (band_x, 0))
            rotated_layer = band.rotate(-angle, expand=True, resample=Image.Resampling.BICUBIC)
            paste_x = int(math.floor(source_box[0]))
            paste_y = int(math.floor(source_box[1]))
            repaired.alpha_composite(rotated_layer, (paste_x, paste_y))
            unit["_render_bbox"] = [
                max(0, paste_x), max(0, paste_y),
                min(repaired.width, paste_x + rotated_layer.width),
                min(repaired.height, paste_y + rotated_layer.height),
            ]
        else:
            if fixed_slot and horizontal_scale < 0.999:
                layer_width = max(4, int(math.ceil(required_width + 4)))
                layer_height = max(4, int(math.ceil(required_height + 4)))
                layer = Image.new("RGBA", (layer_width, layer_height), (0, 0, 0, 0))
                layer_draw = ImageDraw.Draw(layer)
                layer_draw.text(
                    (1, 0), lines[0], font=font, fill=text_fill,
                    stroke_width=stroke_width, stroke_fill=stroke_fill,
                )
                scaled_width = max(1, int(round(required_width * horizontal_scale)))
                layer = layer.resize((scaled_width + 2, layer.height), Image.Resampling.LANCZOS)
                align = int(unit.get("align") or 0)
                if align == 2:
                    paste_x = int(round(selected_box[2] - layer.width))
                elif align == 1:
                    paste_x = int(round((selected_box[0] + selected_box[2] - layer.width) / 2.0))
                else:
                    paste_x = int(round(selected_box[0]))
                paste_y = int(round(selected_box[1]))
                repaired.alpha_composite(layer, (paste_x, paste_y))
                unit["_render_bbox"] = [
                    max(0, paste_x), max(0, paste_y),
                    min(repaired.width, paste_x + layer.width),
                    min(repaired.height, paste_y + layer.height),
                ]
                continue
            for line_index, line in enumerate(lines):
                line_width = draw.textbbox((0, 0), line, font=font)[2]
                align = int(unit.get("align") or 0)
                if align == 2:
                    draw_x = selected_box[2] - line_width
                elif align == 1:
                    draw_x = (selected_box[0] + selected_box[2] - line_width) / 2.0
                else:
                    draw_x = selected_box[0]
                draw.text(
                    (draw_x, selected_box[1] + line_index * line_height), line,
                    font=font, fill=text_fill, stroke_width=stroke_width, stroke_fill=stroke_fill,
                )
            unit["_render_bbox"] = [
                selected_box[0], selected_box[1],
                min(repaired.width, max(selected_box[2], selected_box[0] + required_width + 2)),
                min(repaired.height, max(selected_box[3], selected_box[1] + required_height + 2)),
            ]

    if overflow_count:
        warnings.append(f"图片中有 {overflow_count} 个长译文区域已扩展排版，建议重点复核")
    if fallback_regions:
        warnings.append(f"图片中有 {fallback_regions} 个低对比度区域使用了保守背景修复，建议重点复核")
    return repaired, warnings, {
        "render_mode": "localized",
        "background_repaired": True,
        "overflow_units": overflow_count,
        "reflowed_units": reflowed_count,
        "minimum_font_scale": round(minimum_font_scale, 3),
        "minimum_horizontal_scale": round(minimum_horizontal_scale, 3),
        "rotated_units": rotated_count,
        "fallback_background_units": fallback_regions,
    }


def _inverse_exif_display_transform(image, orientation):
    from PIL import Image
    transpose = Image.Transpose
    operations = {
        2: transpose.FLIP_LEFT_RIGHT,
        3: transpose.ROTATE_180,
        4: transpose.FLIP_TOP_BOTTOM,
        5: transpose.TRANSPOSE,
        6: transpose.ROTATE_90,
        7: transpose.TRANSVERSE,
        8: transpose.ROTATE_270,
    }
    operation = operations.get(int(orientation or 1))
    return image.transpose(operation) if operation is not None else image


def _process_image(source_path, output_path, translations, units, target_language):
    from PIL import Image, ImageOps

    with Image.open(source_path) as source:
        source.load()
        exif = source.info.get("exif")
        icc_profile = source.info.get("icc_profile")
        orientation = int(source.getexif().get(274, 1) or 1)
        display = ImageOps.exif_transpose(source).convert("RGBA")
    image, warnings, render_stats = _render_translated_image(display, translations, units, target_language)
    # Restore raw JPEG orientation so both pixel dimensions and EXIF behaviour
    # remain compatible with the source while OCR still used the visual image.
    stored_image = _inverse_exif_display_transform(image, orientation)
    ext = os.path.splitext(output_path)[1].lower()
    if ext in {".jpg", ".jpeg"}:
        kwargs = {"quality": 95, "subsampling": 0}
        if exif:
            kwargs["exif"] = exif
        if icc_profile:
            kwargs["icc_profile"] = icc_profile
        stored_image.convert("RGB").save(output_path, "JPEG", **kwargs)
    else:
        kwargs = {"optimize": True}
        if icc_profile:
            kwargs["icc_profile"] = icc_profile
        stored_image.save(output_path, "PNG", **kwargs)
    qa = {
        "width": stored_image.width,
        "height": stored_image.height,
        "translated_units": len(units),
        "low_confidence_units": sum(1 for item in units if float(item.get("confidence") or 1) < 0.85),
        "tiled_ocr_units": sum(1 for item in units if item.get("tiled_ocr")),
        "orientation_preserved": True,
        **render_stats,
    }
    return qa, warnings


def _thumbnail(path, target_path):
    ext = os.path.splitext(path)[1].lower()
    try:
        from PIL import Image
        if ext == ".pdf":
            fitz = _load_fitz()
            doc = fitz.open(path)
            if not doc.page_count:
                return ""
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(0.75, 0.75), alpha=False)
            pix.save(target_path)
            doc.close()
            return target_path
        if ext in {".png", ".jpg", ".jpeg"}:
            image = Image.open(path)
            image.thumbnail((480, 360))
            image.convert("RGB").save(target_path, "PNG")
            return target_path
        if ext in {".docx", ".xlsx"}:
            etree = _load_lxml()
            texts = []
            with zipfile.ZipFile(path, "r") as package:
                names = package.namelist()
                if ext == ".docx":
                    selected = [name for name in names if name == "word/document.xml"]
                else:
                    selected = [name for name in names if name == "xl/sharedStrings.xml" or name.startswith("xl/worksheets/")]
                for name in selected:
                    try:
                        root = etree.fromstring(package.read(name))
                    except Exception:
                        continue
                    for node in root.xpath('.//*[local-name()="t"]'):
                        value = re.sub(r"\s+", " ", str(node.text or "")).strip()
                        if value:
                            texts.append(value)
                        if len(texts) >= 18:
                            break
                    if len(texts) >= 18:
                        break
            canvas = Image.new("RGB", (480, 360), "white")
            from PIL import ImageDraw, ImageFont
            draw = ImageDraw.Draw(canvas)
            font_path = _font_path("")
            font = ImageFont.truetype(font_path, 17) if font_path else ImageFont.load_default()
            y = 24
            for text in texts or [os.path.basename(path)]:
                preview_text = text[:90]
                lines = _pil_text_wrap(draw, preview_text, font, 424)
                for line in lines[:2]:
                    draw.text((28, y), line, fill=(31, 41, 55), font=font)
                    y += 24
                    if y > 326:
                        break
                if y > 326:
                    break
            draw.rectangle((8, 8, 471, 351), outline=(210, 218, 230), width=2)
            canvas.save(target_path, "PNG")
            return target_path
    except Exception:
        return ""
    return ""


def process_direct_artifacts(
    artifacts,
    upload_folder,
    translate_batch,
    progress_callback,
    should_cancel,
    source_language,
    target_language,
    instructions="",
    max_workers=3,
    ocr_workers=1,
    ocr_callback=None,
):
    """Process every file independently; successful files survive sibling failures."""
    artifacts = artifacts or []
    reserved_output_names = {
        str((item.get("output") or {}).get("name") or "").casefold()
        for item in artifacts
        if item.get("status") in {"completed", "completed_with_warnings"}
        and (item.get("output") or {}).get("name")
    }

    def notify():
        if progress_callback:
            progress_callback(artifacts)

    for artifact_index, artifact in enumerate(artifacts):
        if artifact.get("status") in {"completed", "completed_with_warnings"}:
            continue
        _check_pause(should_cancel, artifact)
        source_path = artifact_file_path(upload_folder, artifact, "source")
        artifact_dir = _artifact_dir(upload_folder, artifact)
        ext = os.path.splitext(source_path)[1].lower()
        output_path = _safe_join(artifact_dir, f"output{ext}")
        manifest = _read_manifest(upload_folder, artifact)
        translations = manifest.get("translations") if isinstance(manifest.get("translations"), dict) else {}
        ocr_quality, ocr_target_longest = _ocr_quality_config(ocr_callback)
        artifact["ocr_quality"] = ocr_quality
        artifact["ocr_target_longest"] = ocr_target_longest
        if ext == ".pdf" and manifest.get("translation_schema") != PDF_TRANSLATION_CHECKPOINT_SCHEMA:
            # Earlier PDF checkpoints may contain translations assigned to the
            # wrong neighboring OCR IDs. Keep extraction resumable, but never
            # silently reuse those semantically unsafe model results.
            translations = {}
        pdf_checkpoint_signature = None
        pdf_page_checkpoint_dir = None
        try:
            _set_artifact(artifact, "preflight", 4, "正在预检文件")
            notify()
            _check_pause(should_cancel, artifact)

            if ext in EXTRA_EXTENSIONS:
                _set_artifact(artifact, "extract", 14, "正在提取文件可编辑文字")
                notify()
                units, extra_state = extract_extra(source_path, ext, ocr_callback, should_cancel)
            elif ext in {".docx", ".xlsx"}:
                _set_artifact(artifact, "extract", 14, "正在精确提取 OOXML 可编辑文字")
                notify()
                etree, trees, units = _xml_units_from_package(source_path, ext.lstrip("."))
            elif ext == ".pdf":
                _set_artifact(artifact, "extract", 14, "正在逐页提取 PDF 文字层、版面坐标和保留区域")
                notify()

                pdf_checkpoint_signature = _pdf_checkpoint_signature(
                    source_path,
                    source_language,
                    ocr_target_longest,
                )
                pdf_page_checkpoint_dir = _pdf_page_checkpoint_dir(upload_folder, artifact)
                cached_extraction = _read_pdf_extraction_cache(
                    upload_folder,
                    artifact,
                    pdf_checkpoint_signature,
                )

                if cached_extraction is not None:
                    units, source_meta = cached_extraction
                    total_cached_pages = max(0, int(source_meta.get("page_count") or 0))
                    cached_native_pages = sum(
                        1 for item in source_meta.get("page_classifications") or []
                        if item.get("base_type") == "text"
                    )
                    cached_ocr_pages = sum(
                        1 for item in source_meta.get("page_classifications") or []
                        if item.get("base_type") == "scanned"
                    )
                    artifact["page_progress"] = {
                        "current_page": total_cached_pages,
                        "completed_pages": total_cached_pages,
                        "total_pages": total_cached_pages,
                        "native_pages": cached_native_pages,
                        "ocr_pages": cached_ocr_pages,
                        "ocr_completed_pages": cached_ocr_pages,
                        "ocr_failed_pages": 0,
                        "ocr_cached_pages": cached_ocr_pages,
                        "ocr_workers": max(1, int(source_meta.get("ocr_worker_count") or 1)),
                        "ocr_preprocess": source_meta.get("ocr_preprocess") or "adaptive",
                        "ocr_quality": source_meta.get("ocr_quality") or ocr_quality,
                        "ocr_target_longest": int(source_meta.get("ocr_target_longest") or ocr_target_longest),
                        "ocr_runtime": dict(source_meta.get("ocr_runtime") or {}),
                        "phase": "cached",
                        "status": "completed",
                    }
                    _set_artifact(
                        artifact,
                        "extract",
                        24,
                        f"已从检查点恢复 {total_cached_pages} 页提取结果，无需重新 OCR",
                    )
                    notify()
                else:

                    def report_pdf_page(state):
                        state = dict(state or {})
                        artifact["page_progress"] = state
                        total_pages = max(0, int(state.get("total_pages") or 0))
                        current_page = max(0, int(state.get("current_page") or 0))
                        completed_pages = max(0, int(state.get("completed_pages") or 0))
                        ocr_pages = max(0, int(state.get("ocr_pages") or 0))
                        ocr_completed = max(0, int(state.get("ocr_completed_pages") or 0))
                        ocr_failed = max(0, int(state.get("ocr_failed_pages") or 0))
                        ocr_cached = max(0, int(state.get("ocr_cached_pages") or 0))
                        ocr_workers = max(1, int(state.get("ocr_workers") or 1))
                        phase = str(state.get("phase") or "inspect")
                        status = str(state.get("status") or "processing")
                        if total_pages:
                            extraction_progress = 14 + (
                                int(math.ceil(10.0 * completed_pages / total_pages))
                                if completed_pages else 0
                            )
                        else:
                            extraction_progress = 14
                        if status == "starting":
                            message = f"PDF 共 {total_pages} 页，准备逐页提取" if total_pages else "正在读取 PDF 页数"
                        elif status == "processing":
                            if phase == "ocr" and ocr_workers > 1:
                                message = f"正在 {ocr_workers} 路并行 OCR · 已完成 {completed_pages}/{total_pages} 页"
                            else:
                                action = "OCR 识别" if phase == "ocr" else ("提取文字层" if phase == "native" else "检查页面类型")
                                message = f"正在{action}第 {current_page}/{total_pages} 页 · 已完成 {completed_pages} 页"
                            if ocr_cached:
                                message += f" · 已恢复 {ocr_cached} 页"
                        else:
                            message = f"已提取 {completed_pages}/{total_pages} 页"
                            if ocr_pages:
                                message += f" · OCR {ocr_completed}/{ocr_pages} 页"
                            if ocr_cached:
                                message += f" · 检查点 {ocr_cached} 页"
                            if ocr_failed:
                                message += f" · 失败 {ocr_failed} 页"
                        _set_artifact(artifact, "extract", min(24, extraction_progress), message)
                        notify()

                    units, _page_refs, source_meta = _extract_pdf(
                        source_path,
                        ocr_callback,
                        should_cancel,
                        artifact,
                        page_progress_callback=report_pdf_page,
                        ocr_workers=ocr_workers,
                        page_checkpoint_dir=pdf_page_checkpoint_dir,
                        checkpoint_signature=pdf_checkpoint_signature,
                    )
                    extraction_failures = int(
                        (source_meta.get("page_progress_summary") or {}).get("ocr_failed_pages") or 0
                    )
                    if units and extraction_failures == 0:
                        _write_pdf_extraction_cache(
                            upload_folder,
                            artifact,
                            pdf_checkpoint_signature,
                            units,
                            source_meta,
                        )
                        _clear_pdf_page_checkpoints(pdf_page_checkpoint_dir)
            else:
                _set_artifact(artifact, "ocr", 14, "正在识别图片文字与坐标")
                notify()
                units = _extract_image(source_path, ocr_callback)

            if not units:
                raise RuntimeError("文件中没有识别到可翻译的文字")
            source_name = str((artifact.get("source") or {}).get("name") or f"file{ext}")
            source_stem = os.path.splitext(source_name)[0]
            filename_unit = None
            if _is_translatable(source_stem):
                filename_unit = {
                    "id": _unit_id("filename", artifact.get("artifact_id") or source_name, source_stem),
                    "text": source_stem,
                    "context": "输出文件名主体；只翻译名称，不添加扩展名、路径、引号、说明或额外后缀",
                    "kind": "filename",
                }
            # Put the name in the first batch so the artifact card receives its
            # translated download name early, even for very large documents.
            translation_units = ([filename_unit] if filename_unit else []) + list(units)
            low_confidence = [item for item in units if "confidence" in item and float(item.get("confidence") or 0) < 0.85]
            if low_confidence:
                artifact.setdefault("warnings", []).append(f"有 {len(low_confidence)} 个 OCR 区域置信度低于 85%，建议重点复核")
            _set_artifact(artifact, "translate", 25, f"已建立 {len(units)} 个稳定翻译单元")
            notify()

            def translate_with_context(batch):
                clean = [
                    {
                        "id": item["id"],
                        "text": item["text"],
                        "context": item.get("context") or "",
                        "kind": item.get("kind") or "content",
                    }
                    for item in batch
                ]
                return translate_batch(
                    clean,
                    source_language=source_language,
                    target_language=target_language,
                    instructions=instructions,
                )

            translations = _translate_units(
                translation_units,
                translate_with_context,
                translations,
                max_workers,
                artifact,
                notify,
                should_cancel,
                upload_folder,
                target_language=target_language,
            )
            translated_stem = translations.get(filename_unit["id"], source_stem) if filename_unit else source_stem
            artifact.setdefault("output", {})["name"] = _translated_output_name(
                source_name,
                target_language,
                translated_stem,
                reserved_output_names,
            )
            _write_manifest(upload_folder, artifact, {"translations": translations})
            notify()
            _set_artifact(artifact, "validate", 74, "正在校验 ID、数字、单位和译文完整性")
            notify()
            validation_warnings = []
            for unit in translation_units:
                validation_warnings.extend(_validate_translation(unit["text"], translations.get(unit["id"], "")))
            language_suspects = sum(bool(_target_language_issue(unit['text'], translations.get(unit['id'], ''), target_language)) for unit in translation_units)
            if language_suspects:
                validation_warnings.append(f'有 {language_suspects} 个文本单元可能未完整译为目标语言，请复核（自动检测，仅供参考）')

            _check_pause(should_cancel, artifact)
            rebuild_message = (
                "正在清理原文字形、重建 PDF 视觉背景和可搜索译文层"
                if ext == ".pdf" else "正在原位回填并生成同格式文件"
            )
            _set_artifact(artifact, "rebuild", 82, rebuild_message)
            notify()

            last_pdf_rebuild_report = {"phase": None, "percent": None}

            def report_pdf_rebuild(state):
                state = dict(state or {})
                phase = str(state.get("phase") or "pages")
                percent = max(0, min(100, int(state.get("percent") or 0)))
                completed_pages = max(0, int(state.get("completed_pages") or 0))
                total_pages = max(0, int(state.get("total_pages") or 0))
                artifact["rebuild_progress"] = state
                overall_progress = min(93, 82 + int(math.floor(11.0 * percent / 100.0)))
                if phase == "save":
                    message = "回填 99% · 页面处理完成，正在压缩保存 PDF"
                elif phase == "verify":
                    message = "回填 100% · 正在验证页数、尺寸和可搜索译文层"
                else:
                    message = (
                        f"回填 {percent}% · 正在逐页清理原文字形并重建译文层"
                        f" · 已完成 {completed_pages}/{total_pages} 页"
                    )
                _set_artifact(artifact, "rebuild", overall_progress, message)

                # Persist at percentage boundaries rather than on every page.
                # This keeps a 1,000-page PDF responsive without adding 1,000
                # database commits to the rebuild hot path.
                should_publish = (
                    phase != last_pdf_rebuild_report["phase"]
                    or percent != last_pdf_rebuild_report["percent"]
                    or completed_pages >= total_pages
                )
                if should_publish:
                    last_pdf_rebuild_report.update({"phase": phase, "percent": percent})
                    notify()

            if ext in EXTRA_EXTENSIONS:
                qa, format_warnings = rebuild_extra(source_path, output_path, units, translations, extra_state, target_language, should_cancel)
            elif ext == ".docx":
                for unit in units:
                    _apply_node_translation(unit, translations[unit["id"]])
                source_audit = _zip_audit(source_path)
                source_structure = _ooxml_structure_audit(source_path, "docx")
                _write_zip_copy(source_path, output_path, _serialize_trees(etree, trees))
                output_audit = _zip_audit(output_path)
                output_structure = _ooxml_structure_audit(output_path, "docx")
                if output_audit["bad_crc_part"]:
                    raise RuntimeError(f"输出 DOCX CRC 异常: {output_audit['bad_crc_part']}")
                structure_preserved = source_audit["part_names"] == output_audit["part_names"] and source_structure == output_structure
                qa = {"source_parts": source_audit["parts"], "output_parts": output_audit["parts"], "translated_units": len(units), "source_structure": source_structure, "output_structure": output_structure, "structure_preserved": structure_preserved}
                format_warnings = [] if qa["structure_preserved"] else ["OOXML 部件集合发生变化"]
            elif ext == ".xlsx":
                renamed_sheets, updated_sheet_references, _rename_map = _apply_xlsx_sheet_name_translations(
                    trees, units, translations
                )
                for unit in units:
                    if unit.get("kind") == "sheet_name":
                        continue
                    _apply_node_translation(unit, translations[unit["id"]])
                full_recalculation_requested = _request_xlsx_full_recalculation(etree, trees)
                changed_rows, changed_cols = _autofit_xlsx(etree, trees)
                source_audit = _zip_audit(source_path)
                source_structure = _ooxml_structure_audit(source_path, "xlsx")
                _write_zip_copy(source_path, output_path, _serialize_trees(etree, trees))
                output_audit = _zip_audit(output_path)
                output_structure = _ooxml_structure_audit(output_path, "xlsx")
                if output_audit["bad_crc_part"]:
                    raise RuntimeError(f"输出 XLSX CRC 异常: {output_audit['bad_crc_part']}")
                structure_preserved = source_audit["part_names"] == output_audit["part_names"] and source_structure == output_structure
                qa = {"source_parts": source_audit["parts"], "output_parts": output_audit["parts"], "translated_units": len(units), "rows_autofitted": changed_rows, "columns_autofitted": changed_cols, "renamed_sheets": renamed_sheets, "updated_sheet_references": updated_sheet_references, "full_recalculation_requested": full_recalculation_requested, "source_structure": source_structure, "output_structure": output_structure, "structure_preserved": structure_preserved}
                format_warnings = [] if qa["structure_preserved"] else ["XLSX 部件集合发生变化"]
            elif ext == ".pdf":
                qa, format_warnings = _process_pdf(
                    source_path,
                    output_path,
                    translations,
                    units,
                    source_meta,
                    target_language,
                    progress_callback=report_pdf_rebuild,
                )
                if qa.get("signed_copy_warning"):
                    format_warnings.append("原 PDF 含数字签名；当前文件是新的未签名翻译副本")
            else:
                qa, format_warnings = _process_image(source_path, output_path, translations, units, target_language)

            _set_artifact(artifact, "audit", 94, "正在执行结构和视觉结果审计")
            notify()
            if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                raise RuntimeError("输出文件生成失败")
            storage_rel = "/".join((ARTIFACT_DIRECTORY_NAME, artifact["job_id"], artifact["artifact_id"], f"output{ext}"))
            artifact["output"].update({
                "size": os.path.getsize(output_path),
                "url": f"/api/agents/translate/artifact/{artifact['artifact_id']}/content",
                "preview_url": f"/api/agents/translate/artifact/{artifact['artifact_id']}/content",
                "download_url": f"/api/agents/translate/artifact/{artifact['artifact_id']}/download",
                "storage_key": storage_rel,
            })
            preview_path = _safe_join(artifact_dir, "preview.png")
            if _thumbnail(output_path, preview_path):
                artifact["output"]["thumbnail_url"] = f"/static/uploads/{ARTIFACT_DIRECTORY_NAME}/{artifact['job_id']}/{artifact['artifact_id']}/preview.png"
            all_warnings = list(dict.fromkeys((artifact.get("warnings") or []) + validation_warnings + format_warnings))
            artifact["warnings"] = all_warnings[:30]
            artifact["qa_summary"] = qa
            final_status = "completed_with_warnings" if all_warnings else "completed"
            _set_artifact(artifact, "audit", 100, "翻译文件已生成，可预览和下载", final_status)
            _write_manifest(upload_folder, artifact, {"translations": {}, "completed": True, "qa_summary": qa, "warnings": all_warnings})
            if ext == ".pdf":
                _clear_pdf_extraction_checkpoints(upload_folder, artifact)
            notify()
        except DirectTranslationPaused:
            notify()
            for remaining in artifacts[artifact_index + 1:]:
                if remaining.get("status") == "queued":
                    _set_artifact(remaining, "preflight", 0, "任务已暂停", "paused")
            notify()
            raise
        except Exception as exc:
            artifact.setdefault("warnings", []).append(str(exc))
            _set_artifact(artifact, artifact.get("stage") or "audit", artifact.get("progress") or 0, f"文件处理失败: {exc}", "failed")
            _write_manifest(upload_folder, artifact, {"translations": translations, "error": str(exc)})
            notify()
    return artifacts
