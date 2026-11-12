#!/usr/bin/env python3
"""Supplier Scout local workspace. No runtime dependencies. No outbound messaging."""
import argparse
import base64
import binascii
import csv
import io
import json
import os
import re
import sqlite3
import threading
import subprocess
import signal
import sys
import queue
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from model_service import ModelService
from importers import MAX_FILE_BYTES, parse_file, parse_text, prepare_rows
from engine import DEFAULT_RULES, evaluate, extract_page, crawl, validate_rules

ROOT=Path(__file__).resolve().parent

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def encode(value): return json.dumps(value,ensure_ascii=False,allow_nan=False)

class CollectionCancelled(Exception): pass
class AgentServiceError(ValueError):
    def __init__(self,message,partial=None):super().__init__(message);self.partial=partial
class ModelTransientSiteError(ValueError):
    """Model still unavailable after retries for this site only; the batch should continue.
    collect_site raises it for worker message {'kind':'error','fatal':False,'model_transient':True,'partial':...}.
    .partial is a run_agent-shaped result (contacts, contact_records, contact_status, contact_summary, sources, steps, warnings, offers, scope, model_transient=True) with records found so far, or None."""
    def __init__(self,message,partial=None):super().__init__(message);self.partial=partial

def collect_site(url,log,stop_event,scope="all",model_path=None,rules=None):
    if stop_event.is_set(): raise CollectionCancelled()
    command=[sys.executable,str(ROOT/'collection_worker.py'),url,scope,str(model_path or ROOT/'data'/'model-service.json'),encode(rules or [])]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',start_new_session=True)
    messages=queue.Queue()
    def read_output():
        try:
            for line in process.stdout: messages.put(line)
        finally: messages.put(None)
    threading.Thread(target=read_output,daemon=True).start()
    deadline=time.monotonic()+600
    try:
        while True:
            if stop_event.is_set(): raise CollectionCancelled()
            if time.monotonic()>deadline: raise ValueError('单站 Agent 超过 10 分钟，已停止；可稍后单独继续')
            try: line=messages.get(timeout=.1)
            except queue.Empty: continue
            if line is None: raise ValueError('采集进程意外结束，请查看该网站后重试')
            try: message=json.loads(line)
            except ValueError: continue
            if message.get('kind')=='log': log(message['message'])
            elif message.get('kind')=='result': return message['data']
            elif message.get('kind')=='error':
                if message.get('fatal'): raise AgentServiceError(message['message'],message.get('partial'))
                if message.get('model_transient'): raise ModelTransientSiteError(message['message'],message.get('partial'))
                raise ValueError(message['message'])
    finally:
        if process.poll() is None:
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
        process.stdout.close()

class Store:
    def __init__(self,path,seed=True):
        self.path=str(path); self.model_service=ModelService(Path(path).with_name('model-service.json')); self.lock=threading.RLock(); self.job_lock=threading.Lock(); self.stop_event=threading.Event(); self.job={'status':'idle','stopping':False,'stage':'','running':False,'total':0,'done':0,'mode':'demo','current':''}
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL,url TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',reason TEXT NOT NULL DEFAULT '',contacts TEXT NOT NULL DEFAULT '{}',offers TEXT NOT NULL DEFAULT '[]',sources TEXT NOT NULL DEFAULT '[]',warnings TEXT NOT NULL DEFAULT '[]',notes TEXT NOT NULL DEFAULT '',checked_at TEXT,created_at TEXT NOT NULL, UNIQUE(url,mode));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,supplier_id INTEGER,mode TEXT,message TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,supplier_id INTEGER,mode TEXT,offers TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY,supplier_id INTEGER UNIQUE,subject TEXT,body TEXT,created_at TEXT);
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(suppliers)')}
            for column,definition in [('contact_status',"TEXT NOT NULL DEFAULT 'not_started'"),('contact_checked_at','TEXT'),('contact_result',"TEXT NOT NULL DEFAULT '{}' ")]:
                if column not in columns: db.execute('ALTER TABLE suppliers ADD COLUMN '+column+' '+definition)
            for row in db.execute("SELECT id,contacts FROM suppliers WHERE contact_status='not_started'").fetchall():
                if any(json.loads(row['contacts']).values()): db.execute("UPDATE suppliers SET contact_status='legacy' WHERE id=?",(row['id'],))
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('rules',encode(DEFAULT_RULES)))
            count=db.execute('SELECT count(*) FROM suppliers WHERE mode="demo"').fetchone()[0]
        if seed and not count: self.seed()
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20); db.row_factory=sqlite3.Row; return db
    def rules(self):
        with self.connect() as db: return json.loads(db.execute('SELECT value FROM settings WHERE key="rules"').fetchone()[0])
    def event(self,message,sid=None,mode='real'):
        with self.lock,self.connect() as db: db.execute('INSERT INTO events(supplier_id,mode,message,created_at) VALUES(?,?,?,?)',(sid,mode,message,now()))
    def get(self,sid):
        with self.connect() as db: row=db.execute('SELECT * FROM suppliers WHERE id=?',(sid,)).fetchone()
        if not row: raise ValueError('供应商不存在')
        return self.decode_row(row)
    @staticmethod
    def decode_row(row):
        item=dict(row)
        for key in ['contacts','offers','sources','warnings','contact_result']: item[key]=json.loads(item[key])
        return item
    def state(self):
        with self.lock,self.connect() as db:
            return {'suppliers':[self.decode_row(r) for r in db.execute('SELECT * FROM suppliers ORDER BY id')],
                'rules':self.rules(),'events':[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 300')],
                'history':[dict(r,offers=json.loads(r['offers'])) for r in db.execute('SELECT * FROM history ORDER BY id DESC LIMIT 500')],
                'drafts':[dict(r) for r in db.execute('SELECT * FROM drafts ORDER BY id DESC')], 'job':dict(self.job),
                'model_service':self.model_service.public(),'integrations':{'email':False,'feishu':False,'llm':self.model_service.public()['tested']},'version':'0.1.0'}
    def import_rows(self,rows):
        valid,rejected=prepare_rows(rows)
        if rejected: raise ValueError('第 '+str(rejected[0]['row'])+' 行：'+rejected[0]['reason'])
        added=0
        with self.lock,self.connect() as db:
            numeric=[int(r[0]) for r in db.execute('SELECT name FROM suppliers WHERE mode="real"') if str(r[0]).isdigit()]
            explicit=[int(row['name']) for row in valid if str(row.get('name','')).isdigit()]
            next_number=max(numeric+explicit+[0])+1
            for row in valid:
                if db.execute('SELECT 1 FROM suppliers WHERE url=? AND mode="real"',(row['url'],)).fetchone(): continue
                name=str(row.get('name') or '').strip()
                if not name:
                    name=str(next_number); next_number+=1
                result=db.execute('INSERT OR IGNORE INTO suppliers(name,url,mode,created_at) VALUES(?,?,?,?)',(name[:120],row['url'],'real',now())); added+=result.rowcount
        self.event('导入 '+str(added)+' 家供应商，跳过 '+str(len(valid)-added)+' 条重复记录')
        return {'added':added,'duplicates':len(valid)-added}
    def save_result(self,sid,result):
        row=self.get(sid); offers=[evaluate(o,self.rules()) for o in result.get('offers',[])]
        contacts=result.get('contacts',{}); status='review'; reason='已读取网页，未发现明确折扣；请核对价格表或粘贴报价'
        if not offers:
            if any(contacts.values()): reason='已找到联系方式；页面暂无明确折扣，需核对价格表'
            elif result.get('sources') and all(len(source.get('excerpt',''))<180 for source in result['sources']): reason='网页内容很少，可能需要浏览器加载或登录；请查看来源'
        if offers:
            best=min(offers,key=lambda o:{'qualified':0,'review':1,'skipped':2}[o['status']]); status=best['status']; reason=best['reason']
        if status=='qualified' and not any(contacts.values()): status='review'; reason='价格符合区间，但缺少联系方式，待补查'
        with self.lock,self.connect() as db:
            db.execute('UPDATE suppliers SET status=?,reason=?,contacts=?,offers=?,sources=?,warnings=?,checked_at=? WHERE id=?',(status,reason,encode(contacts),encode(offers),encode(result.get('sources',[])),encode(result.get('warnings',[])),now(),sid))
            if offers: db.execute('INSERT INTO history(supplier_id,mode,offers,created_at) VALUES(?,?,?,?)',(sid,row['mode'],encode(offers),now()))
        self.event('完成分析：'+row['name']+' · '+reason,sid,row['mode'])
    def save_contacts(self,sid,result):
        with self.lock:
            row=self.get(sid); contacts={key:list(value) for key,value in row['contacts'].items()}
            for kind,values in result.get('contacts',{}).items():
                existing={str(value).lower() for value in contacts.get(kind,[])}
                for value in values:
                    if str(value).lower() not in existing:
                        contacts.setdefault(kind,[]).append(value);existing.add(str(value).lower())
            sources=list(row['sources'])
            known={(entry.get('url'),entry.get('excerpt')) for entry in sources}
            for entry in result.get('sources',[]):
                if (entry.get('url'),entry.get('excerpt')) not in known:
                    sources.append(entry);known.add((entry.get('url'),entry.get('excerpt')))
            current=result.get('contact_status','incomplete')
            if not result.get('contacts') and any(row['contacts'].values()):current='needs_review'
            detail={key:result.get(key) for key in ['contact_records','contact_summary','steps','warnings','scope']}
            for key in ['contact_records','steps','warnings']:detail[key]=detail[key] or []
            for record in detail['contact_records']:record.setdefault('checked_at',now())
            previous=(row['contact_result'].get('previous_contact_records') or [])+(row['contact_result'].get('contact_records') or [])
            detail['previous_contact_records']=list({(r.get('type'),r.get('value'),r.get('source_url'),r.get('evidence')):r for r in previous}.values())
            with self.connect() as db:
                db.execute('UPDATE suppliers SET contacts=?,sources=?,contact_status=?,contact_checked_at=?,contact_result=? WHERE id=?',(encode(contacts),encode(sources),current,now(),encode(detail),sid))
            self.event('联系方式检查：'+row['name']+' · '+str(result.get('contact_summary') or current),sid,row['mode'])
    def review(self,sid,status,note):
        if status not in ['qualified','review','skipped'] or not str(note).strip(): raise ValueError('请选择结果并填写复核依据')
        row=self.get(sid)
        with self.lock,self.connect() as db: db.execute('UPDATE suppliers SET status=?,reason=?,notes=? WHERE id=?',(status,'人工复核：'+str(note).strip()[:1500],str(note).strip()[:1500],sid))
        self.event('人工复核 '+row['name']+'：'+note[:1500],sid,row['mode'])
    def update_rules(self,rules):
        validate_rules(rules)
        with self.lock,self.connect() as db: db.execute('UPDATE settings SET value=? WHERE key="rules"',(encode(rules),))
        self.event('已更新折扣规则；历史判断保留，重新分析后应用新规则')
    def seed(self):
        fixtures=[('云桥 API','YQ','deepseek-v3',.35,'qualified'),('星河算力','XH','deepseek-r1',.38,'qualified'),('Token Harbor','TH','gpt-4o',.5,'qualified'),('青禾智能','QH','deepseek-v3',.32,'qualified'),('北辰模型服务','BC','deepseek-r1',.46,'review'),('Flow API','FL','gpt-4o',.6,'review'),('知行云','ZX','deepseek-v3',.18,'review'),('极速智算','JS','deepseek-v3',.75,'skipped'),('远山科技','YS','deepseek-r1',.68,'skipped'),('一叶云','YY','deepseek-v3',None,'pending'),('灵犀接口','LX','gpt-4o',None,'pending'),('橙子算力','CZ','deepseek-r1',.36,'review')]
        for idx,(name,abbr,model,ratio,status) in enumerate(fixtures):
            url='https://'+abbr.lower()+'.example'
            contacts={'email':[abbr.lower()+'@supplier.example']}
            if idx%3==0: contacts['wechat']=[abbr.lower()+'_cloud_demo']
            if idx==11: contacts={}
            offers=[] if ratio is None else [evaluate({'model':model,'ratio':ratio,'basis':'published_discount','evidence':name+'：'+model+' '+str(round(ratio*10,1))+'折（虚构演示报价）','source':url+'/pricing'},self.rules())]
            reason={'qualified':'演示报价符合目标区间','review':'演示报价需要人工复核','skipped':'演示报价高于目标区间','pending':'等待采集（演示）'}[status]
            if idx==11: reason='价格符合区间，未找到联系方式'
            with self.connect() as db:
                cur=db.execute('INSERT INTO suppliers(name,url,mode,status,reason,contacts,offers,sources,created_at,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,url,'demo',status,reason,encode(contacts),encode(offers),encode([{'url':url+'/pricing','excerpt':'虚构供应商与报价，仅用于验证流程与界面。'}]),now(),now() if ratio else None))
                if offers: db.execute('INSERT INTO history(supplier_id,mode,offers,created_at) VALUES(?,?,?,?)',(cur.lastrowid,'demo',encode(offers),now()))
        self.event('示例工作区已就绪：12 家虚构供应商，未访问或联系任何真实供应商',mode='demo')
    def start_job(self,mode,ids=None,scope='all',selection='remaining'):
        if mode not in ['real','demo']: raise ValueError('数据模式错误')
        if scope not in ['all','contacts'] or selection not in ['remaining','all']: raise ValueError('采集模式或范围无效')
        if mode=='real':
            if not self.model_service.public()['configured']: raise ValueError('请先在模型服务设置中填写 API URL、模型名和 API Key')
            from browser_agent import runtime_paths
            runtime_paths()
        if not self.job_lock.acquire(blocking=False): raise ValueError('已有批次运行，请等待完成')
        rows=[r for r in self.state()['suppliers'] if r['mode']==mode and (
            (ids and r['id'] in ids) or (not ids and (mode=='demo' or selection=='all' or
            (scope=='contacts' and r['contact_status']!='found') or
            (scope=='all' and r['status']=='pending'))))]
        if not rows: self.job_lock.release(); raise ValueError('该范围没有待采集记录；可选择重新检查全部')
        self.stop_event.clear()
        self.job.update(scope=scope,selection=selection,running=True,status='running',stopping=False,stage='准备中',total=len(rows),done=0,mode=mode,current='准备中')
        self.job_thread=threading.Thread(target=self.run_job,args=(rows,mode,scope),daemon=True)
        self.job_thread.start()
        return dict(self.job)
    def stop_job(self):
        with self.lock:
            if self.job['running']:
                self.stop_event.set(); self.job.update(stopping=True,status='stopping',stage='正在停止当前请求，保留已完成结果')
            return dict(self.job)
    def run_job(self,rows,mode,scope='all'):
        try:
            self.event(('演示回放' if mode=='demo' else '仅联系方式 Agent' if scope=='contacts' else '全量浏览器 Agent')+'开始：'+str(len(rows))+' 家',mode=mode)
            model_failures=0
            for row in rows:
                if self.stop_event.is_set(): break
                self.job.update(current=row['name'],stage='准备访问 '+row['url'])
                def progress(message):
                    if self.stop_event.is_set(): return
                    self.job['stage']=message
                    self.event(message,row['id'],mode)
                try:
                    if mode=='demo':
                        if self.stop_event.wait(.35): raise CollectionCancelled()
                        result={k:row[k] for k in ['contacts','offers','sources','warnings']}
                    else: result=collect_site(row['url'],progress,self.stop_event,scope,self.model_service.path,self.rules() if scope=='all' else [])
                    with self.lock:
                        if self.stop_event.is_set(): raise CollectionCancelled()
                        if scope=='all':
                            prior=self.get(row['id']);merged={k:list(v) for k,v in prior['contacts'].items()}
                            for kind,values in result.get('contacts',{}).items():merged[kind]=sorted(set(merged.get(kind,[])+values))
                            self.save_result(row['id'],dict(result,contacts=merged,sources=prior['sources']+result.get('sources',[])))
                        self.save_contacts(row['id'],result)
                        self.job['done']+=1
                    model_failures=0
                except CollectionCancelled: break
                except ModelTransientSiteError as e:
                    with self.lock:
                        if self.stop_event.is_set(): break
                        partial=dict(e.partial or {});found=any((partial.get('contacts') or {}).values())
                        self.save_contacts(row['id'],dict(partial,contacts=partial.get('contacts') or {},sources=partial.get('sources') or [],warnings=list(dict.fromkeys((partial.get('warnings') or [])+[str(e)[:400]])),contact_status='incomplete' if found else 'blocked',contact_summary='模型服务暂时不可用，本站稍后重试',scope=scope))
                        self.event('模型服务暂时不可用，跳过 '+row['name']+'：'+str(e)[:300],row['id'],mode)
                        self.job['done']+=1
                    model_failures+=1
                    if model_failures>=3:
                        self.job.update(status='failed',error=str(e),stage='模型服务连续 3 家不可用，批次已暂停；可稍后继续')
                        self.event('模型服务连续 3 家不可用，批次已暂停；可稍后继续',mode=mode)
                        break
                except AgentServiceError as e:
                    if self.stop_event.is_set():break
                    self.save_contacts(row['id'],e.partial or {'contact_status':'blocked','contact_summary':'模型服务调用失败，已停止批次，请检查 API 配置','warnings':[str(e)],'contacts':{},'sources':[],'steps':[],'scope':scope})
                    self.job.update(status='failed',error=str(e),stage='模型服务不可用，批次已停止')
                    self.event('Agent 批次因模型服务错误停止：'+str(e)[:500],row['id'],mode)
                    break
                except Exception as e:
                    with self.lock:
                        if self.stop_event.is_set(): break
                        if scope=='all':
                            with self.connect() as db: db.execute('UPDATE suppliers SET status="error",reason=?,checked_at=? WHERE id=?',(str(e)[:1500],now(),row['id']))
                        self.save_contacts(row['id'],{'contact_status':'blocked','contact_summary':str(e)[:500],'contacts':{},'sources':[],'warnings':[str(e)],'scope':scope})
                        self.event('采集失败 '+row['name']+'：'+str(e)[:500],row['id'],mode)
                        self.job['done']+=1
            stopped=self.stop_event.is_set()
            if self.job['status']!='failed':self.job['status']='stopped' if stopped else 'completed'
            self.event(('批次因模型服务失败结束' if self.job['status']=='failed' else '批次已停止' if stopped else '批次完成')+'：已处理 '+str(self.job['done'])+' / '+str(len(rows))+' 家；已完成结果保留',mode=mode)
        finally:
            with self.lock:
                terminal='failed' if self.job['status']=='failed' else 'stopped' if self.stop_event.is_set() else 'completed'
                self.job.update(status=terminal,running=False,stopping=False,current='',stage='模型服务失败，已停止' if terminal=='failed' else '已停止' if terminal=='stopped' else '已完成')
            self.job_lock.release()

def draft_content(row):
    models='、'.join(sorted(set(o['model'] for o in row['offers']))) or '贵司支持的大模型 API'
    subject='【询价 #S'+str(row['id'])+'】'+models+' API 批量采购合作'
    body=f'''您好，{row['name']}团队：

我们正在评估 {models} 的 API 采购方案，希望了解以下信息：

1. 完整模型名称与版本、输入/输出/缓存价格、币种及计价单位；
2. 阶梯折扣、最低采购量、有效期及其他适用条件；
3. 服务来源、并发与限流、稳定性保障；
4. 结算方式、发票及售后联系方式。

烦请提供正式报价及价格页链接。回复时请保留主题中的 #S{row['id']}，便于关联询价。

谢谢！
[请填写姓名 / 公司 / 联系方式]
'''
    return subject,body

def make_draft(row,subject=None,body=None):
    default_subject,default_body=draft_content(row); msg=EmailMessage(policy=policy.SMTP)
    emails=row['contacts'].get('email',[])
    if not emails: raise ValueError('该供应商没有邮箱，请先补充联系方式')
    msg['To']=emails[0]; msg['Subject']=subject or default_subject; msg['X-Unsent']='1'
    msg.set_content(body or default_body)
    return msg.as_bytes()

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
            if path=='/api/state': return self.send(store.state())
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
                return self.send(make_draft(row,saved['subject'] if saved else None,saved['body'] if saved else None),ctype='message/rfc822',filename='inquiry-'+str(sid)+'.eml')
            name={'/':'index.html','/app.js':'app.js','/admin.js':'admin.js','/style.css':'style.css','/favicon.svg':'favicon.svg'}.get(path)
            if name:
                mime={'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'}[Path(name).suffix]
                return self.send((ROOT/'static'/name).read_bytes(),ctype=mime)
            return self.send({'error':'路径不存在'},404)
        except (ValueError,KeyError) as e: self.send({'error':str(e)},400)
        except Exception as e: self.send({'error':'服务错误：'+str(e)},500)
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
            elif path=='/api/review': store.review(int(data['id']),data['status'],data['note']); result={'ok':True}
            elif path=='/api/rules': store.update_rules(data['rules']); result={'ok':True}
            elif path=='/api/drafts':
                row=store.get(int(data['id'])); subject,body=draft_content(row)
                subject=str(data.get('subject',subject)); body=str(data.get('body',body))
                if '\n' in subject or '\r' in subject or len(subject)>300 or len(body)>30000: raise ValueError('邮件内容格式或长度无效')
                if not row['contacts'].get('email'): raise ValueError('该供应商没有邮箱')
                with store.lock,store.connect() as db: db.execute('INSERT INTO drafts(supplier_id,subject,body,created_at) VALUES(?,?,?,?) ON CONFLICT(supplier_id) DO UPDATE SET subject=excluded.subject,body=excluded.body,created_at=excluded.created_at',(row['id'],subject,body,now()))
                store.event('询价草稿已保存（未发送）',row['id'],row['mode']); result={'ok':True}
            elif path=='/api/reply':
                import html
                row=store.get(int(data['id'])); content=str(data.get('text','')).strip()
                if not content or len(content)>30000: raise ValueError('请粘贴 1–30000 字回复内容')
                # Escape pasted text so literal markup cannot hide evidence.
                page=extract_page('<p>'+html.escape(content).replace('\n','</p><p>')+'</p>','手动粘贴回复 #S'+str(row['id']))
                contacts={key:sorted(set(row['contacts'].get(key,[])+page['contacts'].get(key,[]))) for key in set(row['contacts'])|set(page['contacts'])}
                offers=page['offers']
                store.save_result(row['id'],{'contacts':contacts,'offers':offers,'sources':row['sources']+[{'url':'手动粘贴回复','excerpt':content,'created_at':now()}],'warnings':[] if offers else ['未识别明确折扣，需要人工复核；历史报价保留在报价历史中']})
                result={'ok':True,'offers':len(offers)}
            else: return self.send({'error':'路径不存在'},404)
            self.send(result)
        except (ValueError,KeyError,TypeError) as e: self.send({'error':str(e)},400)
        except Exception as e: self.send({'error':'服务错误：'+str(e)},500)

def create_server(port=8765,path=None,seed=True):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler); server.daemon_threads=True; server.store=Store(path or ROOT/'data'/'supplier.db',seed=seed); return server

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765); parser.add_argument('--db'); args=parser.parse_args()
    (ROOT/'data').mkdir(exist_ok=True)
    database_path=Path(args.db) if args.db else ROOT/'data'/'supplier.db'
    new_workspace=not database_path.exists()
    app=create_server(args.port,args.db)
    if new_workspace:
        from rulebook import A_RULES
        app.store.update_rules(A_RULES)
    print('Supplier Scout → http://127.0.0.1:'+str(app.server_port),flush=True)
    try: app.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        app.store.stop_job()
        if getattr(app.store,'job_thread',None): app.store.job_thread.join(timeout=3)
        app.server_close()
