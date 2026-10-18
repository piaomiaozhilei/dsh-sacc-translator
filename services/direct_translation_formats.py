"""Additional upload formats. Existing DOCX/XLSX/PDF/JPEG/PNG code is unchanged."""
from html import escape, unescape
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
import csv
import re
import zipfile

EXTRA_EXTENSIONS = {".doc", ".xls", ".ppt", ".pptx", ".csv", ".txt", ".md", ".html", ".htm", ".gif", ".webp", ".bmp"}
LEGACY = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}
IMAGES = {".gif": "GIF", ".webp": "WEBP", ".bmp": "BMP"}


def validate_extra(path, ext):
    if ext == ".pptx":
        with zipfile.ZipFile(path) as package:
            if "ppt/presentation.xml" not in package.namelist() or package.testzip():
                raise ValueError("PPTX 文件包结构无效或已损坏")
    elif ext in LEGACY:
        with open(path, "rb") as stream:
            if stream.read(8) != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                raise ValueError("旧版 Office 文件内容与扩展名不一致")
    elif ext in IMAGES:
        from PIL import Image
        with Image.open(path) as image:
            if image.format != IMAGES[ext]:
                raise ValueError("图片内容与扩展名不一致")
            image.verify()
    else:
        _read_text(path)


def _read_text(path):
    raw = Path(path).read_bytes()
    encodings = (["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else
                 ["utf-8-sig"] if raw.startswith(b"\xef\xbb\xbf") else ["utf-8", "gb18030", "cp1252"])
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            if "\x00" in text:
                raise ValueError("文本文件包含二进制内容")
            return text, encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文本文件编码")


def _unit(units, text, location, context):
    from services.direct_translation import _is_translatable, _unit_id
    if not _is_translatable(text) or text.lstrip().startswith(("=", "+", "@")):
        return None
    item = {"id": _unit_id("extra", str(location), text), "text": text, "context": context}
    units.append(item)
    return item


class _HtmlText(HTMLParser):
    """Record source spans so scripts, URLs, markup and whitespace stay intact."""
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source, self.spans, self.skip = source, [], []
        self.lines = [0] + [m.end() for m in re.finditer("\n", source)]

    def source_offset(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "code", "pre", "svg", "math"}:
            self.skip.append(tag)
        if not self.skip:
            for match in re.finditer(r'''\b(?:alt|title|aria-label|placeholder)\s*=\s*(["'])(.*?)\1''', self.get_starttag_text(), re.I | re.S):
                self.spans.append((self.source_offset() + match.start(2), self.source_offset() + match.end(2)))

    def handle_endtag(self, tag):
        if tag in self.skip:
            self.skip = self.skip[:self.skip.index(tag)]

    def handle_data(self, data):
        if not self.skip:
            self.spans.append((self.source_offset(), self.source_offset() + len(data)))


def _text_spans(text, ext):
    if ext in {".html", ".htm"}:
        parser = _HtmlText(text)
        parser.feed(text)
        return parser.spans
    spans, offset, fence, frontmatter = [], 0, None, False
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if ext == ".md":
            if offset == 0 and content.strip() == '---':
                frontmatter = True
                offset += len(line)
                continue
            if frontmatter:
                if content.strip() in {'---', '...'}:
                    frontmatter = False
                offset += len(line)
                continue
            marker = re.match(r"^\s*(`{3,}|~{3,})", content)
            if marker:
                if fence is None:
                    fence = marker[1][0]
                elif marker[1][0] == fence:
                    fence = None
                offset += len(line)
                continue
            if fence or re.match(r"^( {4}|\t)|^\s*\[[^\]]+\]:|^\s*<", content):
                offset += len(line)
                continue
            # Translate only prose spans; leave syntax, code and link targets.
            protected = r"`+[^`]*`+|<[^>]*>|!?\[|\]\([^)]*\)|\]\[[^\]]*\]|https?://\S+|[*_~|#>]+|^\s*[-+\d.)]+\s+|\\."
            start = 0
            for match in re.finditer(protected, content):
                spans.append((offset + start, offset + match.start()))
                start = match.end()
            spans.append((offset + start, offset + len(content)))
        else:
            spans.append((offset, offset + len(content)))
        offset += len(line)
    return spans


def _extract_pptx(path):
    from services.direct_translation import _load_lxml
    etree, trees, units = _load_lxml(), {}, []
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    with zipfile.ZipFile(path) as package:
        for name in sorted(package.namelist()):
            if not re.match(r"ppt/(slides|notesSlides|slideMasters|slideLayouts|charts|diagrams)/[^/]+\.xml$", name):
                continue
            root = etree.fromstring(package.read(name), parser=etree.XMLParser(resolve_entities=False, no_network=True))
            trees[name] = {"root": root, "declaration": True}
            for index, paragraph in enumerate(root.findall(".//a:p", ns)):
                nodes = paragraph.findall(".//a:t", ns)
                item = _unit(units, "".join(n.text or "" for n in nodes), f"{name}:{index}", f"PowerPoint 段落 {name}")
                if item:
                    item["refs"] = [{"kind": "text", "node": n, "original": n.text or ""} for n in nodes]
            chart_ns = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
            for index, node in enumerate(root.xpath('.//c:strCache/c:pt/c:v | .//c:tx/c:v', namespaces=chart_ns)):
                item = _unit(units, node.text or "", f"{name}:chart:{index}", "PowerPoint 图表标签")
                if item:
                    item["refs"] = [{"kind": "text", "node": node, "original": node.text or ""}]
    return units, {"etree": etree, "trees": trees}


def extract_extra(source_path, ext, ocr_callback, should_cancel):
    from services import direct_translation as direct
    state = {"ext": ext}
    if ext in LEGACY:
        from services.office_conversion import convert_office
        converted = str(Path(source_path).with_name("legacy_input" + LEGACY[ext]))
        convert_office(source_path, converted)
        direct._check_pause(should_cancel)
        state["converted"] = converted
        if ext == ".ppt":
            units, nested = _extract_pptx(converted)
        else:
            etree, trees, units = direct._xml_units_from_package(converted, LEGACY[ext][1:])
            # Legacy sheet names remain stable so formulas and named ranges
            # cannot be detached from their targets during binary conversion.
            units = [unit for unit in units if unit.get("kind") != "sheet_name"]
            nested = {"etree": etree, "trees": trees}
        state.update(nested)
    elif ext == ".pptx":
        units, nested = _extract_pptx(source_path)
        state.update(nested)
    elif ext in IMAGES:
        from PIL import Image
        units = []
        state["frames"] = []
        with Image.open(source_path) as image:
            count = getattr(image, "n_frames", 1)
            # Pillow encoders retain animation frames; reject pathological
            # animations before allocating their full decoded size.
            if count > 1 and image.width * image.height * count * 4 > 256 * 1024 * 1024:
                raise ValueError("动画解码体积超过 256 MiB，请缩短动画或降低尺寸后重试")
            state["loop"] = image.info.get("loop")
            for frame in range(count):
                direct._check_pause(should_cancel)
                image.seek(frame)
                frame_path = str(Path(source_path).with_name(f"frame-{frame}.png"))
                image.convert("RGBA").save(frame_path)
                frame_units = direct._extract_image(frame_path, ocr_callback)
                for unit in frame_units:
                    unit["id"] = f"frame{frame}:" + unit["id"]
                units.extend(frame_units)
                state["frames"].append({"path": frame_path, "units": frame_units, "duration": image.info.get("duration", 100)})
    else:
        text, encoding = _read_text(source_path)
        units = []
        state.update(text=text, encoding=encoding)
        if ext == ".csv":
            try:
                dialect = csv.Sniffer().sniff(text[:16384], delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.reader(StringIO(text, newline=""), dialect))
            state.update(rows=rows, dialect=dialect, cells=[])
            for row_index, row in enumerate(rows):
                for col_index, value in enumerate(row):
                    item = _unit(units, value, f"{row_index}:{col_index}", f"CSV 第 {row_index + 1} 行第 {col_index + 1} 列")
                    if item:
                        state["cells"].append((row_index, col_index, item["id"]))
        else:
            state["spans"] = []
            for start, end in _text_spans(text, ext):
                value = text[start:end]
                left = len(value) - len(value.lstrip())
                start += left
                end -= len(value) - len(value.rstrip())
                value = text[start:end]
                item = _unit(units, unescape(value) if ext in {".html", ".htm"} else value, start, f"{ext[1:].upper()} 正文")
                if item:
                    state["spans"].append((start, end, item["id"]))
    return units, state


def rebuild_extra(source_path, output_path, units, translations, state, target_language, should_cancel):
    from services import direct_translation as direct
    ext, warnings = state["ext"], []
    qa = {"translated_units": len(units)}
    if ext in LEGACY or ext == ".pptx":
        for unit in units:
            direct._apply_node_translation(unit, translations[unit["id"]])
        source = state.get("converted", source_path)
        target = str(Path(output_path).with_name("legacy_translated" + LEGACY[ext])) if ext in LEGACY else output_path
        direct._write_zip_copy(source, target, direct._serialize_trees(state["etree"], state["trees"]))
        before, after = direct._zip_audit(source), direct._zip_audit(target)
        if after["bad_crc_part"] or before["part_names"] != after["part_names"]:
            raise RuntimeError("Office 文件部件审计失败")
        qa["structure_preserved"] = True
        if ext in LEGACY:
            from services.office_conversion import convert_office
            direct._check_pause(should_cancel)
            convert_office(target, output_path)
            validate_extra(output_path, ext)
            qa["legacy_roundtrip"] = True
            warnings.append("旧版 Office 文件经过格式转换，复杂排版请复核")
    elif ext in IMAGES:
        from PIL import Image
        rendered = []
        for index, frame in enumerate(state["frames"]):
            direct._check_pause(should_cancel)
            path = str(Path(output_path).with_name(f"rendered-{index}.png"))
            frame_qa, frame_warnings = direct._process_image(frame["path"], path, translations, frame["units"], target_language)
            warnings.extend(frame_warnings)
            rendered.append(path)
        def following_frames():
            for path in rendered[1:]:
                with Image.open(path) as image:
                    yield image.copy()
        with Image.open(rendered[0]) as first:
            kwargs = {}
            if len(rendered) > 1:
                kwargs.update(save_all=True, append_images=following_frames(), duration=[f["duration"] for f in state["frames"]])
                if state["loop"] is not None:
                    kwargs["loop"] = state["loop"]
            if ext == ".webp":
                kwargs["lossless"] = True
            if ext == ".gif":
                kwargs["disposal"] = 2
            first.convert("RGB" if ext == ".bmp" else "RGBA").save(output_path, IMAGES[ext], **kwargs)
        with Image.open(output_path) as result:
            qa.update(width=result.width, height=result.height, frame_count=getattr(result, "n_frames", 1))
            if result.format != IMAGES[ext]:
                raise RuntimeError("输出图片编码校验失败")
        for path in rendered + [f["path"] for f in state["frames"]]:
            Path(path).unlink(missing_ok=True)
    else:
        if ext == ".csv":
            rows = state["rows"]
            for row, col, unit_id in state["cells"]:
                value = translations[unit_id]
                # A translated label must never become an executable formula.
                rows[row][col] = "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
            buffer = StringIO(newline="")
            newline = "\r\n" if "\r\n" in state["text"] else "\n"
            csv.writer(buffer, state["dialect"], lineterminator=newline).writerows(rows)
            output = buffer.getvalue()
            qa["row_count"] = len(rows)
        else:
            pieces, cursor = [], 0
            for start, end, unit_id in state["spans"]:
                value = translations[unit_id]
                if ext in {".html", ".htm"}:
                    value = escape(value, quote=True)
                elif ext == ".md":
                    value = re.sub(r"([\\`*_{}\[\]<>|#])", r"\\\1", value)
                value = value.replace("\r", " ").replace("\n", " ")
                pieces.extend((state["text"][cursor:start], value))
                cursor = end
            pieces.append(state["text"][cursor:])
            output = "".join(pieces)
        encoding = state["encoding"]
        try:
            encoded = output.encode(encoding)
        except UnicodeEncodeError:
            encoding = "utf-8-sig" if ext == ".csv" else "utf-8"
            if ext in {".html", ".htm"}:
                output = re.sub(r"(?i)(charset\s*=\s*[\"']?)[\w-]+", r"\1utf-8", output)
            encoded = output.encode(encoding)
            warnings.append("译文字符超出原编码范围，已使用 UTF-8 保存")
        Path(output_path).write_bytes(encoded)
        qa["encoding"] = encoding
    return qa, warnings
