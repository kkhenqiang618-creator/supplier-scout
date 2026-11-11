"""Read user lists without requiring a template or executing spreadsheet formulas."""
import csv
import io
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

MAX_ROWS=5000
MAX_FILE_BYTES=5*1024*1024
URL_HEADERS={'target','url','domain','website','address','link','地址','网址','网站','域名','链接','网页地址','供应商地址'}
NUMBER_HEADERS={'source_row','number','no','no.','id','序号','编号','供应商编号'}
NAME_HEADERS={'name','供应商名称','供应商','名称'}
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'

def normalize_url(value):
    url=str(value or '').strip().strip('\u200b\ufeff').strip('<>"\'“”').strip()
    if not url: raise ValueError('没有找到网页地址')
    if url.startswith('//'): url='https:'+url
    elif not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',url): url='https://'+url
    try:
        p=urlsplit(url)
        if p.scheme.lower() not in ['http','https'] or not p.hostname or p.username or p.password: raise ValueError()
        host=p.hostname.encode('idna').decode().lower()
        if any(c.isspace() for c in url) or len(url)>4096: raise ValueError()
        try: ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in host.rstrip('.').split('.')): raise ValueError()
        port=p.port
        netloc=('['+host+']' if ':' in host else host)+((':'+str(port)) if port is not None else '')
        # A root slash is equivalent; preserve every non-root path/query exactly.
        path='' if p.path=='/' and not p.query and not p.fragment else p.path
        return urlunsplit((p.scheme.lower(),netloc,path,p.query,p.fragment))
    except (ValueError,UnicodeError): raise ValueError('这个地址无法识别，请复制浏览器地址栏中的网址')

def grid_to_rows(grid):
    grid=[row for row in grid if any(str(v).strip() for v in row)]
    if not grid: return []
    header=[str(v).strip().lower() for v in grid[0]]
    ui=next((i for i,v in enumerate(header) if v in URL_HEADERS),None)
    ni=next((i for i,v in enumerate(header) if v in NUMBER_HEADERS),None)
    namei=next((i for i,v in enumerate(header) if v in NAME_HEADERS),None)
    rows=[]
    if ui is not None:
        for row in grid[1:]:
            item={'url':str(row[ui]).strip() if ui<len(row) else ''}
            if ni is not None and ni<len(row): item['number']=str(row[ni]).strip()
            if namei is not None and namei<len(row): item['name']=str(row[namei]).strip()
            rows.append(item)
    else:
        for row in grid:
            if len(row)==1: rows.append({'url':str(row[0]).strip()})
            else:
                first=str(row[0]).strip()
                rows.append({('number' if re.fullmatch(r'\d+',first) else 'name'):first,'url':str(row[1]).strip()})
    return rows

def parse_text(text):
    text=str(text).lstrip('\ufeff').strip()
    if not text: raise ValueError('请粘贴网址，或选择 Excel 表格')
    if text.startswith('['):
        try: data=json.loads(text)
        except ValueError: raise ValueError('名单内容无法读取，请直接粘贴网址或上传 Excel')
        if not isinstance(data,list): raise ValueError('名单须为数组')
        return [{'url':v} if isinstance(v,str) else v for v in data]
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    # Browser URLs may contain commas; do not treat those as CSV delimiters.
    looks_url=lambda line: bool(re.match(r'^(?:https?://|//|[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)+(?=[:/\s?#]|$))',line,re.I)) and '\t' not in line
    if all(looks_url(line) for line in lines): return [{'url':line} for line in lines]
    delimiter='\t' if '\t' in lines[0] else ','
    try:
        grid=list(csv.reader(io.StringIO(text),delimiter=delimiter,strict=True))
        has_header=any(str(value).strip().lower() in URL_HEADERS for value in grid[0])
        if delimiter==',' and not has_header and any(looks_url(line) for line in lines):
            grid=[[line] if looks_url(line) else next(csv.reader([line],strict=True)) for line in lines]
    except csv.Error: raise ValueError('名单内容无法读取，请直接粘贴网址或上传 Excel')
    return grid_to_rows(grid)

def parse_xlsx(raw):
    if len(raw)>MAX_FILE_BYTES: raise ValueError('Excel 文件请控制在 5MB 以内')
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            info=archive.infolist()
            if len(info)>10000 or sum(i.file_size for i in info)>50*1024*1024: raise ValueError('Excel 内容过大，请只保留供应商名单')
            def xml(path):
                value=archive.read(path)
                if b'<!DOCTYPE' in value or b'<!ENTITY' in value: raise ValueError('Excel 含不支持的 XML 声明')
                return ET.fromstring(value)
            names=set(archive.namelist()); strings=[]
            if 'xl/sharedStrings.xml' in names:
                strings=[''.join(t.text or '' for t in si.findall('.//s:t',NS)) for si in xml('xl/sharedStrings.xml').findall('s:si',NS)]
            rows=[]
            sheets=sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n))
            for name in sheets:
                root=xml(name); relations={}; relpath=name.replace('worksheets/','worksheets/_rels/')+'.rels'
                if relpath in names:
                    for rel in xml(relpath):
                        if rel.get('Type','').endswith('/hyperlink') and rel.get('TargetMode')=='External': relations[rel.get('Id')]=rel.get('Target','')
                links={link.get('ref'):relations.get(link.get(RID),'') for link in root.findall('s:hyperlinks/s:hyperlink',NS)}
                grid=[]
                for row in root.findall('s:sheetData/s:row',NS):
                    cells={}
                    for cell in row.findall('s:c',NS):
                        ref=cell.get('r',''); column=re.match('[A-Z]+',ref)
                        if not column: continue
                        idx=0
                        for char in column[0]: idx=idx*26+ord(char)-64
                        if idx>100: continue
                        value=cell.find('s:v',NS); text=value.text or '' if value is not None else ''
                        if cell.get('t')=='s': text=strings[int(text)] if text else ''
                        elif cell.get('t')=='inlineStr': text=''.join(t.text or '' for t in cell.findall('.//s:t',NS))
                        formula=cell.find('s:f',NS)
                        if formula is not None:
                            match=re.match(r'\s*(?:_xlfn\.)?HYPERLINK\s*\(\s*"((?:[^"]|"")*)"',formula.text or '',re.I)
                            if match: text=match[1].replace('""','"')
                        cells[idx-1]=links.get(ref) or text
                    if cells:
                        grid.append([cells.get(i,'') for i in range(max(cells)+1)])
                        if len(grid)>MAX_ROWS+1: raise ValueError('每次最多导入 5000 条，请拆分表格')
                rows.extend(grid_to_rows(grid))
                if len(rows)>MAX_ROWS: raise ValueError('每次最多导入 5000 条，请拆分表格')
            if not rows: raise ValueError('Excel 中没有找到地址，请检查表格内容')
            return rows
    except (BadZipFile,KeyError,IndexError,ET.ParseError,OverflowError) as exc: raise ValueError('Excel 文件无法读取，请用 Excel 或 WPS 重新保存为 .xlsx') from exc

def parse_file(name,raw):
    if str(name).lower().endswith('.xlsx'): return parse_xlsx(raw)
    if str(name).lower().endswith('.xls'): raise ValueError('请在 Excel 或 WPS 中另存为 .xlsx 后上传')
    if len(raw)>MAX_FILE_BYTES: raise ValueError('文件请控制在 5MB 以内')
    for codec in ['utf-8-sig','utf-16','gb18030']:
        try: return parse_text(raw.decode(codec))
        except UnicodeError: continue
    raise ValueError('文件无法读取，请改用 Excel 表格或直接粘贴网址')

def prepare_rows(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=MAX_ROWS: raise ValueError('每次可导入 1–5000 个地址')
    valid=[]; rejected=[]
    for index,row in enumerate(rows,1):
        if isinstance(row,str): row={'url':row}
        try:
            if not isinstance(row,dict): raise ValueError('没有找到网页地址')
            value=row.get('url',row.get('domain',row.get('address','')))
            item=dict(row,url=normalize_url(value))
            if not item.get('name'):
                number=row.get('number',row.get('source_row',row.get('编号')))
                if number is not None and str(number).strip(): item['name']=str(number).strip()
            valid.append(item)
        except ValueError as exc: rejected.append({'row':index,'value':str(row.get('url',row.get('domain','')))[:160] if isinstance(row,dict) else str(row)[:160],'reason':str(exc)})
    return valid,rejected
