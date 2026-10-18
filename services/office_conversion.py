"""Bounded, isolated LibreOffice conversion for legacy Office formats only."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import threading

_CONVERSION_LOCK = threading.Lock()
FILTERS = {
    ".doc": "MS Word 97", ".xls": "MS Excel 97", ".ppt": "MS PowerPoint 97",
    ".docx": "Office Open XML Text", ".xlsx": "Calc Office Open XML",
    ".pptx": "Impress MS PowerPoint 2007 XML",
}


def office_executable():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    candidates = [root / "libreoffice/program/soffice.com",
                  root / ".tools/libreoffice/runtime/program/soffice.com"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    installed = shutil.which("soffice")
    if installed:
        return installed
    raise RuntimeError("安装包缺少旧版 Office 转换组件，请修复安装")


def convert_office(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    ext = destination.suffix.lower()
    # A short temporary root avoids Windows MAX_PATH failures in LibreOffice's
    # deeply nested extension registry (artifact directories are much longer).
    with _CONVERSION_LOCK, tempfile.TemporaryDirectory(prefix="sacc-office-") as work:
        profile = Path(work) / "profile"
        profile.mkdir()
        # Never use the interactive user's profile. Disable document macros and
        # external-link refresh in this disposable conversion process.
        (profile / "user").mkdir()
        (profile / "user/registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Calc/Content/Update">'
            '<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>'
            '</oor:items>', encoding="utf-8")
        command = [office_executable(), f"-env:UserInstallation={profile.as_uri()}",
                   "--headless", "--nologo", "--nodefault", "--norestore",
                   "--convert-to", f"{ext[1:]}:{FILTERS[ext]}", "--outdir", work, str(source)]
        env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
        flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, creationflags=flags)
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                process.kill()
            process.communicate()
            raise RuntimeError("旧版 Office 文件转换超时，请检查文件是否损坏或加密") from None
        converted = Path(work) / (source.stem + ext)
        if process.returncode or not converted.is_file() or not converted.stat().st_size:
            detail = (stderr or stdout).decode("utf-8", errors="replace")[-1200:]
            raise RuntimeError(f"旧版 Office 文件转换失败（可能损坏或加密）: {detail}")
        shutil.copyfile(converted, destination)
    return str(destination)
