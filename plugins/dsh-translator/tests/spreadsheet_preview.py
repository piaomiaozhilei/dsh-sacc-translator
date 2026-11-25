import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
sys.path.insert(0,str(ROOT.parents[1]))
from spreadsheet_preview import trim_sheet, NS


class PreviewTrimTests(unittest.TestCase):
    def test_preserves_content_formulas_merges_and_interior_styles(self):
        raw=f'''<worksheet xmlns="{NS}"><dimension ref="A1:XFD1048576"/>
        <cols><col min="1" max="256" width="18" hidden="1"/></cols><sheetData>
        <row r="1"><c r="A1" s="2"><v>0</v></c><c r="F1" s="9"/></row>
        <row r="2"><c r="B2"><is><t>text</t></is></c><c r="C2" s="3"/></row>
        <row r="5"><c r="D5"><f>A1+1</f><v>1</v></c></row>
        <row r="60000"><c r="Z60000" s="9"/></row></sheetData>
        <mergeCells count="2"><mergeCell ref="B2:C3"/><mergeCell ref="X60000:Z60000"/></mergeCells></worksheet>'''
        sheet=ET.fromstring(trim_sheet(raw.encode()))
        find=lambda path:sheet.find(path,{'s':NS})
        self.assertEqual(find('s:dimension').get('ref'),'A1:D5')
        self.assertEqual(find('s:cols/s:col').get('max'),'4')
        self.assertEqual(find('s:cols/s:col').get('hidden'),'1')
        self.assertEqual(len(sheet.findall('s:sheetData/s:row',{'s':NS})),3)
        self.assertEqual(find('s:sheetData/s:row/s:c/s:v').text,'0')
        self.assertEqual(find('s:mergeCells').get('count'),'1')
        self.assertIn(b'A1+1',trim_sheet(raw.encode()))
        self.assertIn(b'C2',trim_sheet(raw.encode()))
        self.assertNotIn(b'F1',trim_sheet(raw.encode()))


def actual(source, worker):
    from openpyxl import load_workbook
    target=ROOT/'.build/preview-check';target.mkdir(exist_ok=True)
    output=target/'preview.xlsx'
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    request={'type':'spreadsheet-preview','source':str(source),'destination':str(output)}
    result=subprocess.run([worker],input=json.dumps(request)+'\n',capture_output=True,text=True,encoding='utf-8',timeout=200)
    assert result.returncode==0,result.stderr
    events=[json.loads(line) for line in result.stdout.splitlines()]
    assert any(e['type']=='complete' for e in events),events
    assert before==hashlib.sha256(source.read_bytes()).hexdigest()
    original=load_workbook(target/'converted.xlsx',data_only=False)
    cleaned=load_workbook(output,data_only=False)
    assert original.sheetnames==cleaned.sheetnames
    total=0
    for old,new in zip(original,cleaned):
        cells=lambda s:{c.coordinate:(c.value,c.style_id,c.number_format) for row in s for c in row if c.value is not None}
        assert cells(old)==cells(new),old.title
        with zipfile.ZipFile(output) as z:
            index=cleaned.sheetnames.index(new.title)+1
            sheet=ET.fromstring(z.read(f'xl/worksheets/sheet{index}.xml'))
            colmax=max([int(c.attrib['max']) for c in sheet.findall(f'{{{NS}}}cols/{{{NS}}}col')]+[new.max_column])
        total+=new.max_row*colmax
        print(new.title,new.max_row,colmax)
    assert total<=250000,total
    print('native preview matrix',total,'source unchanged; cells/formulas/styles preserved')


if __name__=='__main__':
    if len(sys.argv)>2:
        actual(Path(sys.argv[1]),sys.argv[2])
    else:
        unittest.main()
