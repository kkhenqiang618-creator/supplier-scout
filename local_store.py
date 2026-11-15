"""Local SQLite workspace store and the batch target that persists local runs."""
import contextlib
import json
import sqlite3
import threading
from email import policy
from email.message import EmailMessage
from pathlib import Path
from collection import (now, encode, CollectionCancelled, collect_site, manual_decision,
    merge_sources, merge_contacts, quote_fingerprint, QUOTE_CHANGED_WARNING)
from engine import DEFAULT_RULES, evaluate, validate_rules
from collection_diagnostics import collection_diagnostics
from entry_recovery import validate_entry_url, canonical_url
from importers import parse_file, parse_text, prepare_rows
from model_service import ModelService

LOCAL_SCHEMA_VERSION=1

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
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version>LOCAL_SCHEMA_VERSION: raise RuntimeError('工作区数据库版本比程序新（%d > %d），请先更新程序' % (version,LOCAL_SCHEMA_VERSION))
            if version<LOCAL_SCHEMA_VERSION:
                columns={r[1] for r in db.execute('PRAGMA table_info(suppliers)')}
                for column,definition in [('contact_status',"TEXT NOT NULL DEFAULT 'not_started'"),('contact_checked_at','TEXT'),('contact_result',"TEXT NOT NULL DEFAULT '{}' ")]:
                    if column not in columns: db.execute('ALTER TABLE suppliers ADD COLUMN '+column+' '+definition)
                db.execute('PRAGMA user_version=%d' % LOCAL_SCHEMA_VERSION)
            for row in db.execute("SELECT id,contacts FROM suppliers WHERE contact_status='not_started'").fetchall():
                if any(json.loads(row['contacts']).values()): db.execute("UPDATE suppliers SET contact_status='legacy' WHERE id=?",(row['id'],))
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('rules',encode(DEFAULT_RULES)))
            count=db.execute("SELECT count(*) FROM suppliers WHERE mode='demo'").fetchone()[0]
        if seed and not count: self.seed()
    @contextlib.contextmanager
    def connect(self):
        """One connection per unit of work, committed and closed even on error.

        Windows refuses to delete or move a directory while a SQLite handle is
        still open, so a leaked handle makes `data/` look "in use" after the app
        exits and turns every temp-dir cleanup into a PermissionError.
        """
        db=sqlite3.connect(self.path,timeout=20); db.row_factory=sqlite3.Row
        try:
            yield db; db.commit()
        except Exception:
            db.rollback(); raise
        finally:
            db.close()
    def rules(self):
        with self.connect() as db: return json.loads(db.execute("SELECT value FROM settings WHERE key='rules'").fetchone()[0])
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
        result=item.get('contact_result') or {}
        if not result.get('diagnostics'):result['diagnostics']=collection_diagnostics(result,fallback_url=item.get('url',''))
        item['contact_result']=result
        return item
    def workspace_key(self,kind):
        # settings has a global primary key even with tenant RLS enabled.
        username=getattr(self,'username',None)
        return kind+(':user:'+username if username else '')
    def setting(self,key,default=None):
        with self.connect() as db:row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else default
    def followup_prefix(self):
        return self.workspace_key('followups')+':'
    def followups(self):
        """All follow-up records of this workspace, one settings row per supplier.

        Saving a record used to read and rewrite one JSON blob holding every record.
        Each record now lives under its own key, so a single save touches one row.
        The legacy blob is still read for workspaces written before this split.
        """
        prefix=self.followup_prefix();records={}
        legacy=self.setting(self.workspace_key('followups'),{})
        if isinstance(legacy,dict):records.update({str(key):value for key,value in legacy.items() if isinstance(value,dict)})
        pattern=prefix.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.connect() as db:
            rows=db.execute("SELECT key,value FROM settings WHERE key LIKE ? ESCAPE '\\'",(pattern,)).fetchall()
        for row in rows:
            try:record=json.loads(row['value'])
            except (TypeError,ValueError):continue
            if isinstance(record,dict):records[row['key'][len(prefix):]]=record
        return records
    def followup(self,sid):
        with self.connect() as db:row=db.execute('SELECT value FROM settings WHERE key=?',(self.followup_prefix()+str(sid),)).fetchone()
        if row:
            try:record=json.loads(row['value'])
            except (TypeError,ValueError):record=None
            if isinstance(record,dict):return record
        return self.followups().get(str(sid),{})
    def save_followup(self,sid,status,note):
        with self.lock,self.connect() as db:
            self.get(sid)
            if status not in ('waiting','replied','quote','done'):raise ValueError('跟进状态无效')
            if not isinstance(note,str) or not note.strip() or len(note)>5000:raise ValueError('请填写 1–5000 字联系记录')
            previous=self.followup(sid)
            stamp=now();entry={'status':status,'note':note.strip(),'updated_at':stamp}
            history=list(previous.get('history') or [])
            if previous and not history:history=[{key:previous.get(key) for key in ('status','note','updated_at')}]
            record={'id':sid,**entry,'created_at':previous.get('created_at') or stamp,'history':(history+[entry])[-100:]}
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(self.followup_prefix()+str(sid),encode(record)))
            return record
    def draft_recipient(self,sid):
        row=self.get(sid)
        saved=self.setting(self.workspace_key('draft-recipient')+':'+str(sid))
        emails=row['contacts'].get('email',[])
        recipient=saved if saved is not None else (emails[0] if emails else None)
        return self.validate_recipient(row,recipient)
    @staticmethod
    def validate_recipient(row,recipient):
        if not isinstance(recipient,str) or '\r' in recipient or '\n' in recipient or recipient not in row['contacts'].get('email',[]):
            raise ValueError('收件人须为该供应商当前邮箱，请重新选择')
        return recipient
    def save_draft(self,sid,subject=None,body=None,**options):
        with self.lock,self.connect() as db:
            row=self.get(sid);default_subject,default_body=draft_content(row)
            subject=str(default_subject if subject is None else subject);body=str(default_body if body is None else body)
            if '\n' in subject or '\r' in subject or len(subject)>300 or len(body)>30000:raise ValueError('邮件内容格式或长度无效')
            recipient=self.validate_recipient(row,options['recipient']) if 'recipient' in options else self.draft_recipient(sid)
            db.execute('INSERT INTO drafts(supplier_id,subject,body,created_at) VALUES(?,?,?,?) ON CONFLICT(supplier_id) DO UPDATE SET subject=excluded.subject,body=excluded.body,created_at=excluded.created_at',(sid,subject,body,now()))
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(self.workspace_key('draft-recipient')+':'+str(sid),encode(recipient)))
        self.event('询价草稿已保存（未发送）',sid,row['mode'])
        return {'ok':True,'recipient':recipient}
    def workspace_state(self,data):
        data['followups']=self.followups()
        # One settings read for all drafts, including large existing workspaces.
        prefix=self.workspace_key('draft-recipient')+':'
        keys=[prefix+str(draft['supplier_id']) for draft in data['drafts']]
        recipients={}
        if keys:
            with self.connect() as db:
                recipients={row['key']:json.loads(row['value']) for row in db.execute('SELECT key,value FROM settings WHERE key IN ('+','.join('?' for _ in keys)+')',tuple(keys))}
        for draft in data['drafts']:
            draft['recipient']=recipients.get(prefix+str(draft['supplier_id']))
        return data
    def pricing(self,mode='real'):
        if mode not in ('real','demo'):raise ValueError('报价模式无效')
        with self.lock,self.connect() as db:
            fields=('id','name','url','mode','offers','checked_at')
            suppliers=[{key:json.loads(row[key]) if key=='offers' else row[key] for key in fields} for row in db.execute('SELECT id,name,url,mode,offers,checked_at FROM suppliers WHERE mode=? ORDER BY id',(mode,))]
            history=[dict(row,offers=json.loads(row['offers'])) for row in db.execute('SELECT * FROM history WHERE mode=? ORDER BY id DESC',(mode,))]
            return {'suppliers':suppliers,'history':history,'rules':self.rules()}
    def state(self,full=True):
        with self.lock,self.connect() as db:
            if full:
                suppliers=[self.decode_row(r) for r in db.execute('SELECT * FROM suppliers ORDER BY id')]
            else:
                # The 1.5s poll must not ship every source excerpt; the drawer fetches /api/supplier.
                suppliers=[self.decode_row(r) for r in db.execute("""SELECT id,name,url,mode,status,reason,contacts,notes,checked_at,created_at,contact_status,contact_checked_at,warnings,offers,contact_result,'[]' AS sources,json_array_length(sources) AS source_count FROM suppliers ORDER BY id""")]
                for row in suppliers:row['_summary']=True
            return self.workspace_state({'suppliers':suppliers,
                'rules':self.rules(),'events':[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 300')],
                'history':[dict(r,offers=json.loads(r['offers'])) for r in db.execute('SELECT * FROM history ORDER BY id DESC LIMIT 500')],
                'drafts':[dict(r) for r in db.execute('SELECT * FROM drafts ORDER BY id DESC')], 'job':dict(self.job),
                'model_service':self.model_service.public(),'integrations':{'email':False,'feishu':False,'llm':self.model_service.public()['tested']},'version':'0.1.0'})
    def import_rows(self,rows):
        valid,rejected=prepare_rows(rows)
        if rejected: raise ValueError('第 '+str(rejected[0]['row'])+' 行：'+rejected[0]['reason'])
        added=0
        with self.lock,self.connect() as db:
            numeric=[int(r[0]) for r in db.execute("SELECT name FROM suppliers WHERE mode='real'") if str(r[0]).isdigit()]
            explicit=[int(row['name']) for row in valid if str(row.get('name','')).isdigit()]
            next_number=max(numeric+explicit+[0])+1
            for row in valid:
                if db.execute("SELECT 1 FROM suppliers WHERE url=? AND mode='real'",(row['url'],)).fetchone(): continue
                name=str(row.get('name') or '').strip()
                if not name:
                    name=str(next_number); next_number+=1
                result=db.execute('INSERT OR IGNORE INTO suppliers(name,url,mode,created_at) VALUES(?,?,?,?)',(name[:120],row['url'],'real',now())); added+=result.rowcount
        self.event('导入 '+str(added)+' 家供应商，跳过 '+str(len(valid)-added)+' 条重复记录')
        return {'added':added,'duplicates':len(valid)-added}
    def save_result(self,sid,result):
        with self.lock:
            row=self.get(sid); offers=[evaluate(o,self.rules()) for o in result.get('offers',[])]
            contacts=result.get('contacts',{}); status='review'; reason='已读取网页，未发现明确折扣；请核对价格表或粘贴报价'
            if not offers:
                if any(contacts.values()): reason='已找到联系方式；页面暂无明确折扣，需核对价格表'
                elif result.get('sources') and all(len(source.get('excerpt',''))<180 for source in result['sources']): reason='网页内容很少，可能需要浏览器加载或登录；请查看来源'
            if offers:
                best=min(offers,key=lambda o:{'qualified':0,'review':1,'skipped':2}[o['status']]); status=best['status']; reason=best['reason']
            if status=='qualified' and not any(contacts.values()): status='review'; reason='价格符合区间，但缺少联系方式，待补查'
            sources=merge_sources(row['sources'],result.get('sources',[]))
            warnings=list(result.get('warnings',[]))
            changed=manual_decision(row) and quote_fingerprint(row['offers'])!=quote_fingerprint(offers)
            if manual_decision(row):
                status,reason=row['status'],row['reason']
                if changed or QUOTE_CHANGED_WARNING in row['warnings']:warnings.append(QUOTE_CHANGED_WARNING)
            warnings=list(dict.fromkeys(warnings))
            with self.connect() as db:
                db.execute('UPDATE suppliers SET status=?,reason=?,contacts=?,offers=?,sources=?,warnings=?,checked_at=? WHERE id=?',(status,reason,encode(contacts),encode(offers),encode(sources),encode(warnings),now(),sid))
                if offers: db.execute('INSERT INTO history(supplier_id,mode,offers,created_at) VALUES(?,?,?,?)',(sid,row['mode'],encode(offers),now()))
            self.event('完成分析：'+row['name']+' · '+reason,sid,row['mode'])
            if changed:self.event(QUOTE_CHANGED_WARNING,sid,row['mode'])
    def _entry_values(self,row,url,evidence,source_url,expected_url,method='manual'):
        target=validate_entry_url(url)
        if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>3000:raise ValueError('请填写已核验新网址的依据（1–3000字）')
        if source_url:validate_entry_url(source_url)
        if not isinstance(expected_url,str) or canonical_url(expected_url)!=canonical_url(row['url']):raise ValueError('网址已变更，请刷新后重试')
        if canonical_url(row['url'])==target:raise ValueError('新旧网址相同，无需更新')
        with self.connect() as db:
            for other in db.execute('SELECT id,url FROM suppliers WHERE mode=?',(row['mode'],)):
                if other['id']!=row['id'] and canonical_url(other['url'])==target:raise ValueError('该网址已在你的名单中，请核对已有记录；未合并或删除数据')
        detail=dict(row['contact_result']);history=list(detail.get('entry_history') or [])
        history.append({'from':row['url'],'to':target,'source_url':source_url or '', 'evidence':evidence.strip(),'method':method,'checked_at':now()})
        detail['entry_history']=history[-30:]
        return target,detail
    def _apply_entry(self,sid,url,evidence,source_url,expected_url,method='manual'):
        row=self.get(sid);target,detail=self._entry_values(row,url,evidence,source_url,expected_url,method)
        with self.connect() as db:db.execute('UPDATE suppliers SET url=?,contact_result=? WHERE id=?',(target,encode(detail),sid))
        self.event('网址已核验更新：'+row['url']+' → '+target,sid,row['mode'])
        return self.get(sid)
    def update_entry(self,sid,url,evidence,source_url='',expected_url=None):
        # The local batch owns job_lock for its whole lifetime; reject a URL edit during collection.
        job_lock=getattr(self,'job_lock',None)
        if job_lock is not None and not job_lock.acquire(blocking=False):raise ValueError('采集运行中，请完成或停止后再更新网址')
        try:
            with self.lock:
                if self.job.get('running'):raise ValueError('采集运行中，请完成或停止后再更新网址')
                return self._apply_entry(sid,url,evidence,source_url,expected_url)
        finally:
            if job_lock is not None:job_lock.release()
    def save_contacts(self,sid,result):
        with self.lock:
            row=self.get(sid);original_url=row['url'];target_url=original_url
            recovery=result.get('entry_recovery') or {}
            if recovery.get('verified') is True:
                try:
                    target_url,entry_detail=self._entry_values(row,recovery.get('to'),recovery.get('evidence'),recovery.get('source_url',''),recovery.get('from'),method='official_notice')
                    row=dict(row,contact_result=entry_detail)
                except ValueError as error:
                    result=dict(result,warnings=list(result.get('warnings') or [])+['新入口未自动写入：'+str(error)])
            contacts=merge_contacts(row['contacts'],result.get('contacts'))
            sources=list(row['sources'])
            known={(entry.get('url'),entry.get('excerpt')) for entry in sources}
            for entry in result.get('sources',[]):
                if (entry.get('url'),entry.get('excerpt')) not in known:
                    sources.append(entry);known.add((entry.get('url'),entry.get('excerpt')))
            current=result.get('contact_status','incomplete')
            if not result.get('contacts') and any(row['contacts'].values()):current='needs_review'
            detail={key:result.get(key) for key in ['contact_records','contact_summary','steps','warnings','scope','diagnostics','entry_recovery']}
            for key in ['contact_records','steps','warnings']:detail[key]=detail[key] or []
            detail['entry_history']=list(row['contact_result'].get('entry_history') or [])
            detail['diagnostics']=result.get('diagnostics') or collection_diagnostics(result,fallback_url=row['url'])
            for record in detail['contact_records']:record.setdefault('checked_at',now())
            previous=(row['contact_result'].get('previous_contact_records') or [])+(row['contact_result'].get('contact_records') or [])
            detail['previous_contact_records']=list({(r.get('type'),r.get('value'),r.get('source_url'),r.get('evidence')):r for r in previous}.values())
            with self.connect() as db:
                db.execute('UPDATE suppliers SET url=?,contacts=?,sources=?,contact_status=?,contact_checked_at=?,contact_result=? WHERE id=?',(target_url,encode(contacts),encode(sources),current,now(),encode(detail),sid))
            if target_url!=original_url:self.event('官网升级入口已验证：'+original_url+' → '+target_url,sid,row['mode'])
            self.event('联系方式检查：'+row['name']+' · '+str(result.get('contact_summary') or current),sid,row['mode'])
    def review(self,sid,status,note):
        if status not in ['qualified','review','skipped'] or not str(note).strip(): raise ValueError('请选择结果并填写复核依据')
        with self.lock:
            row=self.get(sid)
            with self.connect() as db: db.execute('UPDATE suppliers SET status=?,reason=?,notes=?,warnings=? WHERE id=?',(status,'人工复核：'+str(note).strip()[:1500],str(note).strip()[:1500],encode([warning for warning in row['warnings'] if warning!=QUOTE_CHANGED_WARNING]),sid))
        self.event('人工复核 '+row['name']+'：'+note[:1500],sid,row['mode'])
    def update_rules(self,rules):
        validate_rules(rules)
        with self.lock,self.connect() as db: db.execute("UPDATE settings SET value=? WHERE key='rules'",(encode(rules),))
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
            service=self.model_service.public()
            if service.get('key_error'): raise ValueError(service['key_error']+'；请重新填写后再开始采集')
            if not service['configured']: raise ValueError('请先在模型服务设置中填写 API URL、模型名和 API Key')
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
        from job_runner import run_batch
        try:
            self.event(('演示回放' if mode=='demo' else '仅联系方式 Agent' if scope=='contacts' else '全量浏览器 Agent')+'开始：'+str(len(rows))+' 家',mode=mode)
            try:
                run_batch(rows,mode,scope,LocalBatchTarget(self,mode))
            except CollectionCancelled:self.job['status']='stopped'
            stopped=self.stop_event.is_set()
            if self.job['status']!='failed':self.job['status']='stopped' if stopped else 'completed'
            self.event(('批次因模型服务失败结束' if self.job['status']=='failed' else '批次已停止' if stopped else '批次完成')+'：已处理 '+str(self.job['done'])+' / '+str(len(rows))+' 家；已完成结果保留',mode=mode)
        finally:
            with self.lock:
                terminal='failed' if self.job['status']=='failed' else 'stopped' if self.stop_event.is_set() else 'completed'
                self.job.update(status=terminal,running=False,stopping=False,current='',stage='模型服务失败，已停止' if terminal=='failed' else '已停止' if terminal=='stopped' else '已完成')
            self.job_lock.release()

class LocalBatchTarget:
    """Persist one local batch run; implements the job_runner.BatchTarget hooks."""
    def __init__(self,store,mode):
        self.store=store
        self.mode=mode
    def active(self):
        return not self.store.stop_event.is_set()
    def start_site(self,row):
        self.store.job.update(current=row['name'],stage='准备访问 '+row['url'])
    def collect(self,row,mode,scope):
        store=self.store
        if mode=='demo':
            if store.stop_event.wait(.35):raise CollectionCancelled()
            return {k:row[k] for k in ['contacts','offers','sources','warnings']}
        def progress(message):
            if store.stop_event.is_set():return
            store.job['stage']=message
            store.event(message,row['id'],mode)
        return collect_site(row['url'],progress,store.stop_event,scope,store.model_service.path,store.rules() if scope=='all' else [])
    def save_success(self,row,result,scope,index):
        store=self.store
        with store.lock:
            if store.stop_event.is_set():raise CollectionCancelled()
            if scope=='all':
                prior=store.get(row['id'])
                store.save_result(row['id'],dict(result,contacts=merge_contacts(prior['contacts'],result.get('contacts')),sources=prior['sources']+result.get('sources',[])))
            store.save_contacts(row['id'],result)
            store.job['done']+=1
    def save_model_transient(self,row,error,scope,index):
        store=self.store
        with store.lock:
            if store.stop_event.is_set():raise CollectionCancelled()
            partial=dict(error.partial or {});found=any((partial.get('contacts') or {}).values())
            store.save_contacts(row['id'],dict(partial,contacts=partial.get('contacts') or {},sources=partial.get('sources') or [],warnings=list(dict.fromkeys((partial.get('warnings') or [])+[str(error)[:400]])),contact_status='incomplete' if found else 'blocked',contact_summary='模型服务暂时不可用，本站稍后重试',scope=scope))
            store.event('模型服务暂时不可用，跳过 '+row['name']+'：'+str(error)[:300],row['id'],self.mode)
            store.job['done']+=1
    def save_agent_failure(self,row,error,scope,index):
        store=self.store
        if not self.active():raise CollectionCancelled()
        store.save_contacts(row['id'],error.partial or {'contact_status':'blocked','contact_summary':'模型服务调用失败，已停止批次，请检查 API 配置','warnings':[str(error)],'contacts':{},'sources':[],'steps':[],'scope':scope})
        store.job.update(status='failed',error=str(error),stage='模型服务不可用，批次已停止')
        store.event('Agent 批次因模型服务错误停止：'+str(error)[:500],row['id'],self.mode)
    def save_site_failure(self,row,error,scope,index):
        store=self.store
        with store.lock:
            if store.stop_event.is_set():raise CollectionCancelled()
            if scope=='all' and not manual_decision(store.get(row['id'])):
                with store.connect() as db:db.execute("UPDATE suppliers SET status='error',reason=?,checked_at=? WHERE id=?",(str(error)[:1500],now(),row['id']))
            store.save_contacts(row['id'],{'contact_status':'blocked','contact_summary':str(error)[:500],'contacts':{},'sources':[],'warnings':[str(error)],'scope':scope})
            store.event('采集失败 '+row['name']+'：'+str(error)[:500],row['id'],self.mode)
            store.job['done']+=1
    def batch_failed(self,error):
        self.store.job.update(status='failed',error=str(error),stage='浏览器运行环境异常，批次已停止')
        self.store.event('批次因浏览器运行环境异常停止：'+str(error)[:500],mode=self.mode)
    def model_failures_exhausted(self):
        from job_runner import MODEL_FAILURE_MESSAGE
        self.store.job.update(status='failed',error=MODEL_FAILURE_MESSAGE,stage=MODEL_FAILURE_MESSAGE)
        self.store.event(MODEL_FAILURE_MESSAGE,mode=self.mode)

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

def make_draft(row,subject=None,body=None,recipient=None):
    default_subject,default_body=draft_content(row); msg=EmailMessage(policy=policy.SMTP)
    emails=row['contacts'].get('email',[])
    if not emails: raise ValueError('该供应商没有邮箱，请先补充联系方式')
    msg['To']=Store.validate_recipient(row,emails[0] if recipient is None else recipient); msg['Subject']=subject or default_subject; msg['X-Unsent']='1'
    msg.set_content(body or default_body)
    return msg.as_bytes()
