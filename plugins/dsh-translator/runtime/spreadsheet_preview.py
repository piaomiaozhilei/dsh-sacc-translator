"""Bound spreadsheet preview dimensions without changing translation inputs."""
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('', NS)


def coord(ref):
    match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', ref)
    if not match:
        raise ValueError('无效单元格坐标')
    col = 0
    for char in match[1]:
        col = col * 26 + ord(char) - 64
    return int(match[2]), col


def cell_name(row, col):
    letters = ''
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f'{letters}{row}'


def trim_sheet(raw):
    for _, (prefix,uri) in ET.iterparse(BytesIO(raw),events=['start-ns']):
        if not re.fullmatch(r'ns\d+',prefix):
            ET.register_namespace(prefix,uri)
    sheet = ET.fromstring(raw)
    tag = lambda name: f'{{{NS}}}{name}'
    data = sheet.find(tag('sheetData'))
    if data is None:
        return raw
    used = []
    for cell in data.iter(tag('c')):
        # A cached-empty formula is still meaningful. Zero and false are values.
        if cell.find(tag('f')) is not None or any((node.text or '') for node in cell.iter() if node.tag in (tag('v'), tag('t'))):
            used.append(coord(cell.attrib['r']))
    maxrow = max((r for r, c in used), default=1)
    maxcol = max((c for r, c in used), default=1)
    merges = sheet.find(tag('mergeCells'))
    if merges is not None:
        for merge in list(merges):
            first, last = merge.attrib['ref'].split(':') if ':' in merge.attrib['ref'] else [merge.attrib['ref']] * 2
            r1,c1 = coord(first); r2,c2 = coord(last)
            if any(r1 <= r <= r2 and c1 <= c <= c2 for r,c in used):
                maxrow=max(maxrow,r2);maxcol=max(maxcol,c2)
            elif r2 > maxrow or c2 > maxcol:
                merges.remove(merge)
        merges.set('count', str(len(merges)))
    for row in list(data):
        if int(row.attrib['r']) > maxrow:
            data.remove(row);continue
        row.attrib.pop('spans', None)
        for cell in list(row):
            if cell.tag == tag('c') and coord(cell.attrib['r'])[1] > maxcol:
                row.remove(cell)
    columns = sheet.find(tag('cols'))
    if columns is not None:
        for column in list(columns):
            if int(column.attrib['min']) > maxcol:
                columns.remove(column)
            else:
                column.set('max',str(min(maxcol,int(column.attrib['max']))))
    dimension = sheet.find(tag('dimension'))
    if dimension is not None:
        dimension.set('ref',f'A1:{cell_name(maxrow,maxcol)}')
    return ET.tostring(sheet, encoding='utf-8', xml_declaration=True)


def prepare_preview(source, destination):
    source,destination=Path(source),Path(destination)
    if source.resolve()==destination.resolve():
        raise ValueError('预览必须使用独立副本')
    if source.suffix.lower()=='.xls':
        from services.office_conversion import convert_office
        converted=destination.with_name('converted.xlsx')
        convert_office(source,converted)
        source=converted
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as output:
        for info in original.infolist():
            raw=original.read(info)
            if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',info.filename):
                raw=trim_sheet(raw)
            output.writestr(info,raw)
    return str(destination)
