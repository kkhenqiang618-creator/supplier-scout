import io
import unittest
from zipfile import ZipFile
from importers import parse_text, parse_xlsx, normalize_url, prepare_rows


def workbook_fixture():
    stream=io.BytesIO()
    with ZipFile(stream,'w') as z:
        z.writestr('xl/worksheets/sheet1.xml','''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is><t>编号</t></is></c><c r="B1" t="inlineStr"><is><t>地址</t></is></c></row>
        <row r="2"><c r="A2" t="inlineStr"><is><t>007</t></is></c><c r="B2" t="inlineStr"><is><t>点击打开</t></is></c></row>
        <row r="3"><c r="A3"><v>8</v></c><c r="B3"><f>HYPERLINK("https://example.org/path?a=1&amp;b=2","官网")</f></c></row>
        </sheetData><hyperlinks><hyperlink ref="B2" r:id="rId1"/></hyperlinks></worksheet>''')
        z.writestr('xl/worksheets/_rels/sheet1.xml.rels','''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/contact" TargetMode="External"/></Relationships>''')
    return stream.getvalue()

class ImporterTests(unittest.TestCase):
    def test_domains_and_browser_links_need_no_formatting(self):
        self.assertEqual(normalize_url(' api.holysheep.ai '),'https://api.holysheep.ai')
        self.assertEqual(normalize_url('https://Example.com/path/?a=1&b=2#pricing'),'https://example.com/path/?a=1&b=2#pricing')
        self.assertEqual(normalize_url('//example.com/path'),'https://example.com/path')
        with self.assertRaises(ValueError): normalize_url('file:///etc/passwd')
    def test_number_and_domain_headers(self):
        self.assertEqual(parse_text('source_row,domain\n2,api.holysheep.ai\n3,36niu.com'),[{'number':'2','url':'api.holysheep.ai'},{'number':'3','url':'36niu.com'}])
        self.assertEqual(parse_text('编号\t地址\n007\texample.com'),[{'number':'007','url':'example.com'}])
    def test_a_workbook_target_header_is_recognized(self):
        self.assertEqual(parse_text('target,联系方式,联系状态\nexample.com,,未联系'),[{'url':'example.com'}])

    def test_paste_one_or_multiple_links_without_name(self):
        self.assertEqual(parse_text('api.holysheep.ai\nhttps://example.com/a?x=1,2'),[{'url':'api.holysheep.ai'},{'url':'https://example.com/a?x=1,2'}])
        self.assertEqual(parse_text('8\texample.com'),[{'number':'8','url':'example.com'}])
    def test_bad_line_does_not_split_another_url_at_its_comma(self):
        text='https://example.com/pricing?models=a,b\n不是网址\nexample.org'
        valid,rejected=prepare_rows(parse_text(text))
        self.assertEqual([r['url'] for r in valid],['https://example.com/pricing?models=a,b','https://example.org'])
        self.assertEqual(len(rejected),1)

    def test_xlsx_clickable_links_and_formula_targets(self):
        rows=parse_xlsx(workbook_fixture())
        self.assertEqual(rows,[{'number':'007','url':'https://example.com/contact'},{'number':'8','url':'https://example.org/path?a=1&b=2'}])
    def test_bad_rows_do_not_block_valid_rows(self):
        valid,rejected=prepare_rows([{'url':'example.com'},{'url':'不是地址'},{'url':'example.org'}])
        self.assertEqual(len(valid),2); self.assertEqual(len(rejected),1); self.assertEqual(rejected[0]['row'],2)
    def test_invalid_workbook_has_human_readable_error(self):
        with self.assertRaisesRegex(ValueError,'Excel'): parse_xlsx(b'not a zip')

if __name__=='__main__': unittest.main()
