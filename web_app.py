"""HTTP layer for the local workspace: request handling and the server factory."""
import base64
import binascii
import csv
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from collection import ROOT, now, encode, log_exception, merge_contacts
from engine import extract_page
from importers import MAX_FILE_BYTES, parse_file, parse_text, prepare_rows
from local_store import Store, make_draft

class Handler(BaseHTTPRequestHandler):
    @property
    def store(self):return self.server.store
    def log_message(self,fmt,*args): pass
    def send(self,data,status=200,ctype='application/json; charset=utf-8',filename=None):
        raw=encode(data).encode() if ctype.startswith('application/json') else data if isinstance(data,bytes) else data.encode('utf-8')
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('X-Frame-Options','DENY')
        if filename: self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
        self.end_headers(); self.wfile.write(raw)
    def check_host(self):
        return self.headers.get('Host','').split(':')[0] in ['127.0.0.1','localhost']
    def do_GET(self):
        if not self.check_host(): return self.send({'error':'仅允许本机访问'},403)
        path=urlsplit(self.path).path; params=parse_qs(urlsplit(self.path).query); store=self.store
        try:
            if path=='/api/state': return self.send(store.state(full=False))
            if path=='/api/supplier':
                try:return self.send(store.get(int(params.get('id',['0'])[0])))
                except (ValueError,KeyError):return self.send({'error':'供应商不存在'},404)
            if path=='/api/pricing': return self.send(store.pricing(params.get('mode',['real'])[0]))
            if path=='/api/export-contacts':
                mode=params.get('mode',['real'])[0];output=io.StringIO();writer=csv.writer(output)
                writer.writerow(['编号','网址','本轮联系状态','类型','联系方式','用途','来源','原文证据','风险/备注','最近检查'])
                for row in store.state()['suppliers']:
                    if row['mode']!=mode:continue
                    records=list(row['contact_result'].get('contact_records') or [])
                    fresh={(r.get('type'),r.get('value')) for r in records}
                    previous={}
                    for item in row['contact_result'].get('previous_contact_records') or []:
                        key=(item.get('type'),item.get('value'))
                        if key not in fresh:previous[key]=dict(item,purpose='历史线索，本轮未核验；'+str(item.get('purpose','')))
                    records.extend(previous.values());fresh.update(previous)
                    for kind,values in row['contacts'].items():
                        for value in values:
                            if (kind,value) not in fresh:records.append({'type':kind,'value':value,'purpose':'原有线索，尚未在本轮核验','source_url':'','evidence':'','risk_flags':['保留原有线索']})
                    for item in records or [{}]:writer.writerow([row['name'],row['url'],row['contact_status'],item.get('type',''),item.get('value',''),item.get('purpose',''),item.get('source_url',''),item.get('evidence',''),'；'.join(item.get('risk_flags',[])) or row['contact_result'].get('contact_summary',''),row['contact_checked_at'] or ''])
                return self.send('\ufeff'+output.getvalue(),ctype='text/csv; charset=utf-8',filename='contacts-'+mode+'.csv')
            if path=='/api/export':
                mode=params.get('mode',['real'])[0]; output=io.StringIO(); writer=csv.writer(output); writer.writerow(['供应商','网址','状态','目标规则','实际模型','输入价','输出价','缓存价','币种','计价单位','分组','分组倍率','可比较折扣','资源形态','报价说明','联系方式','判断依据','来源','最近采集时间','数据类型'])
                for row in store.state()['suppliers']:
                    if row['mode']!=mode: continue
                    for offer in row['offers'] or [{}]: writer.writerow([row['name'],row['url'],row['status'],offer.get('model',''),offer.get('model_name',offer.get('model','')),offer.get('input_price',''),offer.get('output_price',''),offer.get('cache_price',''),offer.get('currency',''),offer.get('unit',''),offer.get('group',''),offer.get('group_multiplier',''),offer.get('ratio',''),offer.get('resource_form','待确认'),offer.get('notes',''),encode(row['contacts']),row['reason'],offer.get('source',''),row['checked_at'] or '',mode])
                return self.send('\ufeff'+output.getvalue(),ctype='text/csv; charset=utf-8',filename='suppliers-'+mode+'.csv')
            if path=='/api/draft':
                sid=int(params.get('id',['0'])[0]); row=store.get(sid)
                with store.connect() as db: saved=db.execute('SELECT * FROM drafts WHERE supplier_id=?',(sid,)).fetchone()
                return self.send(make_draft(row,saved['subject'] if saved else None,saved['body'] if saved else None,store.draft_recipient(sid)),ctype='message/rfc822',filename='inquiry-'+str(sid)+'.eml')
            name={'/':'index.html','/app.js':'app.js','/admin.js':'admin.js','/workspace.js':'workspace.js','/workspace.css':'workspace.css','/style.css':'style.css','/favicon.svg':'favicon.svg','/vendor/qrcode.js':'vendor/qrcode.js'}.get(path)
            if name:
                mime={'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'}[Path(name).suffix]
                return self.send((ROOT/'static'/name).read_bytes(),ctype=mime)
            return self.send({'error':'路径不存在'},404)
        except (ValueError,KeyError) as e: self.send({'error':str(e)},400)
        except Exception:
            log_exception('do_GET')
            self.send({'error':'服务器内部错误，请稍后重试'},500)
    def do_POST(self):
        if not self.check_host(): return self.send({'error':'仅允许本机访问'},403)
        origin=self.headers.get('Origin')
        if origin and origin not in [scheme+'://'+self.headers.get('Host','') for scheme in ['http','https']]: return self.send({'error':'禁止跨站请求'},403)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json': return self.send({'error':'须使用 JSON'},415)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8*1024*1024: raise ValueError('上传内容过大，请使用 5MB 以内的文件')
            data=json.loads(self.rfile.read(length)); path=urlsplit(self.path).path; store=self.store
            if not isinstance(data,dict): raise ValueError('请求须为 JSON 对象')
            if path in ['/api/import','/api/import-preview']:
                if 'file_base64' in data:
                    try: raw=base64.b64decode(data['file_base64'],validate=True)
                    except (ValueError,TypeError,binascii.Error): raise ValueError('文件传输失败，请重新选择文件')
                    if len(raw)>MAX_FILE_BYTES: raise ValueError('文件请控制在 5MB 以内')
                    incoming=parse_file(data.get('filename',''),raw)
                elif 'text' in data: incoming=parse_text(data['text'])
                else: incoming=data.get('rows')
                valid,rejected=prepare_rows(incoming)
                if path=='/api/import-preview':
                    result={'rows':valid,'valid_count':len(valid),'total_count':len(incoming),'rejected':rejected}
                else:
                    if not valid:
                        first=rejected[0]
                        raise ValueError('没有导入任何地址。第 '+str(first['row'])+' 行：'+first['reason'])
                    result=dict(store.import_rows(valid),rejected=rejected)
            elif path=='/api/model-service': result=store.model_service.save(data)
            elif path=='/api/model-service/test': result=store.model_service.test()
            elif path=='/api/stop': result=store.stop_job()
            elif path=='/api/run': result=store.start_job(data.get('mode'),data.get('ids'),data.get('scope','all'),data.get('selection','remaining'))
            elif path=='/api/supplier-entry': result=store.update_entry(int(data['id']),data.get('url'),data.get('evidence'),data.get('source_url',''),data.get('expected_url'))
            elif path=='/api/review': store.review(int(data['id']),data['status'],data['note']); result={'ok':True}
            elif path=='/api/rules': store.update_rules(data['rules']); result={'ok':True}
            elif path=='/api/followup': result=store.save_followup(int(data['id']),data['status'],data['note'])
            elif path=='/api/drafts':
                options={'recipient':data['recipient']} if 'recipient' in data else {}
                result=store.save_draft(int(data['id']),data.get('subject'),data.get('body'),**options)
            elif path=='/api/reply':
                import html
                row=store.get(int(data['id'])); content=str(data.get('text','')).strip()
                if not content or len(content)>30000: raise ValueError('请粘贴 1–30000 字回复内容')
                # Escape pasted text so literal markup cannot hide evidence.
                page=extract_page('<p>'+html.escape(content).replace('\n','</p><p>')+'</p>','手动粘贴回复 #S'+str(row['id']))
                contacts=merge_contacts(row['contacts'],page['contacts'])
                offers=page['offers']
                store.save_result(row['id'],{'contacts':contacts,'offers':offers,'sources':row['sources']+[{'url':'手动粘贴回复','excerpt':content,'created_at':now()}],'warnings':[] if offers else ['未识别明确折扣，需要人工复核；历史报价保留在报价历史中']})
                result={'ok':True,'offers':len(offers)}
            else: return self.send({'error':'路径不存在'},404)
            self.send(result)
        except (ValueError,KeyError,TypeError) as e: self.send({'error':str(e)},400)
        except Exception:
            log_exception('do_POST')
            self.send({'error':'服务器内部错误，请稍后重试'},500)

def create_server(port=8765,path=None,seed=True):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler); server.daemon_threads=True; server.store=Store(path or ROOT/'data'/'supplier.db',seed=seed); return server
