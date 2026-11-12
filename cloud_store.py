"""Private Postgres persistence and durable, fenced browser-job queue."""
import contextlib
import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from cryptography.fernet import Fernet
from model_service import ModelService
from server import Store, encode, now
from rulebook import A_RULES

WRITE_LOCK=732198461
IDLE={'status':'idle','stopping':False,'stage':'','running':False,'total':0,'done':0,'mode':'real','current':''}
ACTIVE=('queued','running','stopping')

class LostLease(RuntimeError):pass

class Row(dict):
    def __getitem__(self,key):return list(self.values())[key] if isinstance(key,int) else super().__getitem__(key)

class Cursor:
    def __init__(self,cursor,lastrowid=None):self.cursor=cursor;self.lastrowid=lastrowid
    @property
    def rowcount(self):return self.cursor.rowcount
    def fetchone(self):
        row=self.cursor.fetchone();return Row(row) if row is not None else None
    def fetchall(self):return [Row(row) for row in self.cursor.fetchall()]
    def __iter__(self):return iter(self.fetchall())

class SQL:
    """Adapt only the small, fixed SQLite statement vocabulary used by Store."""
    def __init__(self,connection):self.connection=connection
    def execute(self,statement,parameters=()):
        statement=statement.strip();ignore=bool(re.match(r'INSERT OR IGNORE',statement,re.I))
        statement=re.sub(r'^INSERT OR IGNORE','INSERT',statement,flags=re.I)
        statement=re.sub(r'"([^"\n]*)"',lambda m:"'"+m[1].replace("'","''")+"'",statement)
        statement=statement.replace('?', '%s')
        if ignore:statement+=' ON CONFLICT DO NOTHING'
        returning=bool(re.match(r'INSERT INTO suppliers\b',statement,re.I)) and 'RETURNING' not in statement.upper()
        if returning:statement+=' RETURNING id'
        cursor=self.connection.execute(statement,parameters)
        last=cursor.fetchone()['id'] if returning and cursor.rowcount else None
        return Cursor(cursor,last)

class Database:
    def __init__(self,url=None,username=None):self.url=url or os.environ['DATABASE_URL'];self.username=username;self.local=threading.local()
    def for_user(self,username):
        if not isinstance(username,str) or not username:raise ValueError('请先登录')
        return Database(self.url,username=username)
    @contextlib.contextmanager
    def transaction(self,write=False):
        existing=getattr(self.local,'sql',None)
        if existing is not None:
            if write:existing.connection.execute('SELECT pg_advisory_xact_lock(%s)',(WRITE_LOCK,))
            yield existing;return
        connection=psycopg.connect(self.url,connect_timeout=12,sslmode='require',prepare_threshold=None,row_factory=dict_row,keepalives=1,keepalives_idle=5,keepalives_interval=5,keepalives_count=3,tcp_user_timeout=15000)
        sql=SQL(connection);self.local.sql=sql
        try:
            connection.execute('SET LOCAL search_path TO scout_private, pg_catalog')
            connection.execute("SELECT set_config('scout.username',%s,true),set_config('scout.system',%s,true)",(self.username or '', 'on' if self.username is None else 'off'))
            connection.execute("SET LOCAL statement_timeout TO '20s'")
            if write:connection.execute('SELECT pg_advisory_xact_lock(%s)',(WRITE_LOCK,))
            yield sql;connection.commit()
        except Exception:connection.rollback();raise
        finally:self.local.sql=None;connection.close()

class CloudModelService(ModelService):
    def __init__(self,database,key=None,username=None):
        self.setting_key='model-service' if username is None else 'model-service:user:'+username
        self.database=database;self.lock=threading.RLock();self.cipher=Fernet((key or os.environ['SCOUT_ENCRYPTION_KEY']).encode())
    def read(self):
        with self.database.transaction() as db:row=db.execute("SELECT value FROM settings WHERE key=?",(self.setting_key,)).fetchone()
        return json.loads(self.cipher.decrypt(row['value'].encode())) if row else {}
    def _write(self,data):
        value=self.cipher.encrypt(encode(data).encode()).decode()
        with self.database.transaction(write=True) as db:db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(self.setting_key,value))
    def _finish_test(self,config,original_model,answer):
        with self.lock,self.database.transaction(write=True):
            return super()._finish_test(config,original_model,answer)
    def _fail_test(self,config):
        with self.lock,self.database.transaction(write=True):
            return super()._fail_test(config)
    def snapshot(self):
        config=self.read()
        if not config.get('base_url') or not config.get('model') or not config.get('api_key'):raise ValueError('请先为当前账号填写 API URL、模型名和 API Key')
        return self.cipher.encrypt(encode(config).encode()).decode()
    def materialize(self,folder,snapshot=None):
        if snapshot is None:config=self.read()
        elif not snapshot:raise ValueError('该任务没有个人模型配置，请登录后重新发起采集')
        else:config=json.loads(self.cipher.decrypt(snapshot.encode()))
        path=Path(folder)/'model-service.json';ModelService(path)._write(config);return path

class PostgresSessionStore:
    def __init__(self,database):self.database=database
    def create(self,username,token_hash,expires_at):
        with self.database.transaction() as db:
            db.execute('DELETE FROM sessions WHERE expires_at<?',(time.time(),))
            db.execute('INSERT INTO sessions(token_hash,username,expires_at) VALUES(?,?,?)',(token_hash,username,expires_at))
    def get(self,token_hash):
        with self.database.transaction() as db:return db.execute('SELECT username,expires_at FROM sessions WHERE token_hash=?',(token_hash,)).fetchone()
    def delete(self,token_hash):
        with self.database.transaction() as db:db.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash,))

class PostgresLoginLimiter:
    def __init__(self,database):self.database=database
    def allow(self,key):
        with self.database.transaction() as db:row=db.execute('SELECT failures,window_start FROM login_attempts WHERE key=?',(str(key),)).fetchone()
        return not row or row['window_start']+900<=time.time() or row['failures']<5
    def failed(self,key):
        stamp=time.time()
        with self.database.transaction() as db:
            db.execute('DELETE FROM login_attempts WHERE window_start<?',(stamp-900,))
            db.execute('INSERT INTO login_attempts(key,failures,window_start) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET failures=CASE WHEN login_attempts.window_start<? THEN 1 ELSE login_attempts.failures+1 END,window_start=CASE WHEN login_attempts.window_start<? THEN excluded.window_start ELSE login_attempts.window_start END',(str(key),stamp,stamp-900,stamp-900))
    def clear(self,key):
        with self.database.transaction() as db:db.execute('DELETE FROM login_attempts WHERE key=?',(str(key),))

class PostgresStore(Store):
    def __init__(self,database=None):
        self.database=database or Database();self.lock=threading.RLock();self.model_service=CloudModelService(self.database);self.path='postgres'
        with self.database.transaction(write=True) as db:db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',('rules',encode(A_RULES)))
    def for_user(self,username):
        if not isinstance(username,str) or not username:raise ValueError('请先登录')
        scoped=copy.copy(self);scoped.username=username;scoped.database=self.database.for_user(username)
        scoped.is_admin=username==os.environ.get('SCOUT_ADMIN_USERNAME','')
        scoped.model_service=copy.copy(self.model_service);scoped.model_service.lock=threading.RLock()
        scoped.model_service.database=scoped.database
        scoped.model_service.setting_key='model-service:user:'+username
        scoped.model_service.cipher=self.model_service.cipher
        return scoped
    def rules(self):
        key='rules:user:'+self.username if getattr(self,'username',None) else 'rules'
        with self.connect() as db:row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else copy.deepcopy(A_RULES)
    def connect(self):return self.database.transaction()
    def _write(self,method,*args,**kwargs):
        with self.database.transaction(write=True),self.lock:return method(self,*args,**kwargs)
    def event(self,*args,**kwargs):return self._write(Store.event,*args,**kwargs)
    def import_rows(self,*args,**kwargs):return self._write(Store.import_rows,*args,**kwargs)
    def save_result(self,sid,result):
        with self.database.transaction(write=True),self.lock:
            current=self.get(sid);contacts={kind:list(values) for kind,values in current['contacts'].items()}
            for kind,values in result.get('contacts',{}).items():
                known={str(value).casefold() for value in contacts.get(kind,[])}
                for value in values:
                    if str(value).casefold() not in known:
                        contacts.setdefault(kind,[]).append(value);known.add(str(value).casefold())
            sources=list(current['sources']);known={(item.get('url'),item.get('excerpt')) for item in sources}
            for item in result.get('sources',[]):
                key=(item.get('url'),item.get('excerpt'))
                if key not in known:sources.append(item);known.add(key)
            return Store.save_result(self,sid,dict(result,contacts=contacts,sources=sources))
    def save_contacts(self,*args,**kwargs):return self._write(Store.save_contacts,*args,**kwargs)
    def update_entry(self,*args,**kwargs):return self._write(Store.update_entry,*args,**kwargs)
    def review(self,*args,**kwargs):return self._write(Store.review,*args,**kwargs)
    def save_followup(self,*args,**kwargs):return self._write(Store.save_followup,*args,**kwargs)
    def save_draft(self,*args,**kwargs):return self._write(Store.save_draft,*args,**kwargs)
    def update_rules(self,rules):
        from engine import validate_rules
        validate_rules(rules)
        key='rules:user:'+self.username if getattr(self,'username',None) else 'rules'
        with self.database.transaction(write=True) as db:db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode(rules)))
        self.event('已更新个人报价规则；历史判断保留，重新采集后应用')
    @property
    def job(self):
        with self.connect() as db:row=db.execute('SELECT * FROM cloud_jobs ORDER BY created_at DESC LIMIT 1').fetchone()
        return self.job_state(row) if row else dict(IDLE)
    @staticmethod
    def job_state(row):
        state={key:value for key,value in row['state'].items() if not key.startswith('_')};state.update(status=row['status'],running=row['status'] in ACTIVE,stopping=bool(row['cancel_requested']) and row['status'] in ('running','stopping'),done=row['position'],scope=row['scope'],mode=row['mode'],id=row['id'])
        clock=time.time()
        # Ages are computed server-side (worker and HTTP hosts share NTP time) so a skewed browser clock cannot fake the hints.
        state.update(waiting_seconds=int(clock-state['queued_at']) if row['status']=='queued' and not state.get('claimed_at') and state.get('queued_at') else 0,heartbeat_age=int(clock-state['heartbeat_at']) if row['status'] in ('running','stopping') and state.get('heartbeat_at') else 0)
        return state
    @staticmethod
    def order_rows(rows,scope):
        """Never-attempted rows first, then the oldest attempt, so a retried batch does not reopen on the same failing site."""
        def attempt(row):
            stamps=[row.get('checked_at')] if scope=='all' else []
            if scope=='contacts' or row.get('attempt_scope')=='all':stamps.append(row.get('contact_checked_at'))
            stamps=[str(stamp) for stamp in stamps if stamp];return (1,max(stamps)) if stamps else (0,'')
        return sorted(rows,key=lambda row:(attempt(row),row['id']))
    def state(self,full=True):
        if full:
            data=Store.state(self)
        else:
            with self.lock,self.connect() as db:
                projection="""SELECT id,name,url,mode,status,reason,contacts,notes,checked_at,created_at,contact_status,contact_checked_at,warnings,
                    '[]'::text AS sources,
                    jsonb_path_query_array(offers::jsonb,'$[0 to 2]')::text AS offers,
                    jsonb_array_length(offers::jsonb) AS offer_count,
                    jsonb_path_query_array(offers::jsonb,'$[*].model') AS offer_models,
                    COALESCE((SELECT bool_or(item->>'status'='review' OR COALESCE(item->>'ratio','')='') FROM jsonb_array_elements(offers::jsonb) item),false) AS needs_price_review,
                    jsonb_build_object('contact_records',COALESCE(contact_result::jsonb->'contact_records','[]'::jsonb),'contact_summary',contact_result::jsonb->'contact_summary','scope',contact_result::jsonb->'scope','warnings',COALESCE(contact_result::jsonb->'warnings','[]'::jsonb),'diagnostics',COALESCE(contact_result::jsonb->'diagnostics','[]'::jsonb),'entry_history',COALESCE(contact_result::jsonb->'entry_history','[]'::jsonb),'entry_recovery',contact_result::jsonb->'entry_recovery')::text AS contact_result
                    FROM suppliers ORDER BY id"""
                suppliers=[self.decode_row(row) for row in db.execute(projection)]
                for row in suppliers:row['_summary']=True
                data={'suppliers':suppliers,'rules':self.rules(),'events':[dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 300')],
                    'history':[dict(row,offers=json.loads(row['offers'])) for row in db.execute('SELECT * FROM history ORDER BY id DESC LIMIT 30')],
                    'drafts':[dict(row) for row in db.execute('SELECT * FROM drafts ORDER BY id DESC')],'job':self.job,
                    'model_service':self.model_service.public(),'integrations':{'email':False,'feishu':False,'llm':self.model_service.public()['tested']},'version':'0.1.0'}
        if not full:self.workspace_state(data)
        data.update(deployment='cloud',auth_enabled=True,username=getattr(self,'username',''),is_admin=getattr(self,'is_admin',False),limits={'file_bytes':3*1024*1024})
        return data
    def team_overview(self,names):
        if not getattr(self,'is_admin',False):raise PermissionError('仅超级管理员可查看成员汇总')
        result={name:{'username':name,'suppliers':0,'contacts_found':0,'qualified':0,'drafts':0,'jobs':[]} for name in names}
        with Database(self.database.url).transaction() as db:
            for row in db.execute("SELECT owner,count(*) AS suppliers,count(*) FILTER(WHERE contact_status='found') AS contacts_found,count(*) FILTER(WHERE status='qualified') AS qualified FROM suppliers WHERE mode='real' GROUP BY owner"):
                if row['owner'] in result:result[row['owner']].update({key:row[key] for key in ['suppliers','contacts_found','qualified']})
            for row in db.execute('SELECT owner,count(*) AS drafts FROM drafts GROUP BY owner'):
                if row['owner'] in result:result[row['owner']]['drafts']=row['drafts']
            for row in db.execute('SELECT * FROM cloud_jobs ORDER BY created_at DESC LIMIT 200'):
                if row['owner'] in result:
                    job=self.job_state(row);job.update(created_at=str(row['created_at']),updated_at=str(row['updated_at']))
                    result[row['owner']]['jobs'].append(job)
        return {'members':list(result.values())}
    @staticmethod
    def busy_message(row,admin=False):
        """Name the occupant of the single cloud browser and its progress instead of a bare refusal.

        Only administrators see the occupant's name and progress; other members just learn
        that the shared browser is busy, so one member's workload is not visible to the team.
        """
        if not row or not admin:return '云端浏览器正在执行其他批次，请稍后重试'
        name=str(row.get('owner') or '').strip()
        if not name:return '云端浏览器正在执行其他批次，请稍后重试'
        total=int(row.get('total') or 0);done=int(row.get('position') or 0)
        progress='，已完成 %d/%d 家'%(done,total) if total else ''
        return '云端浏览器同一时间只执行一个批次：成员 '+name+'正在采集'+progress+'，请稍后重试或等该批次结束后继续'
        return '云端浏览器同一时间只执行一个批次：'+who+'正在采集'+progress+'，请稍后重试或等该批次结束后继续'
    def _busy_message(self):
        """The insert collides with the global one-active-batch index, which RLS hides from the caller's pre-check."""
        try:
            with Database(self.database.url).transaction() as db:
                row=db.execute("SELECT owner,position,state->>'total' AS total FROM cloud_jobs WHERE status IN ('queued','running','stopping') ORDER BY created_at LIMIT 1").fetchone()
        except psycopg.Error:
            row=None
        return self.busy_message(row,getattr(self,'is_admin',False))
    def start_job(self,mode,ids=None,scope='all',selection='remaining'):
        try:return self._start_job(mode,ids,scope,selection)
        except psycopg.errors.UniqueViolation:raise ValueError(self._busy_message()) from None
    def _start_job(self,mode,ids=None,scope='all',selection='remaining'):
        if mode not in ['real','demo'] or scope not in ['all','contacts'] or selection not in ['remaining','all']:raise ValueError('采集模式或范围无效')
        if mode=='real' and (not self.model_service.public()['configured'] or not self.model_service.public()['key_configured']):raise ValueError('请先为当前账号配置 API URL、模型名和 API Key')
        from cloud_scaling import enabled
        if os.environ.get('VERCEL') and not enabled():raise ValueError('云端浏览器启动凭据未就绪，请检查部署配置')
        with self.database.transaction(write=True) as db:
            active=db.execute("SELECT id,status,(lease_owner IS NULL OR lease_until IS NULL OR lease_until<now()) AS stale FROM cloud_jobs WHERE status IN ('queued','running','stopping')").fetchone()
            if active:raise ValueError('上一个批次的后台进程已失联，请先点击停止' if active['status'] in ('running','stopping') and active['stale'] else '已有云端批次运行，请停止或等待完成')
            # Light projection under the same per-user RLS scope as Store.state; avoids decoding every source excerpt just to pick ids.
            candidates=db.execute("SELECT id,mode,status,contact_status,checked_at,contact_checked_at,contact_result::jsonb->>'scope' AS attempt_scope FROM suppliers ORDER BY id").fetchall()
            rows=[row for row in candidates if row['mode']==mode and ((ids and row['id'] in ids) or (not ids and (mode=='demo' or selection=='all' or scope=='contacts' and row['contact_status']!='found' or scope=='all' and row['status']=='pending')))]
            if not rows:raise ValueError('该范围没有待采集记录；可选择重新检查全部')
            rows=self.order_rows(rows,scope)
            snapshot=self.model_service.snapshot() if mode=='real' else ''
            job_id=uuid.uuid4().hex;state=dict(IDLE,requested_by=getattr(self,'username',''),_model_config=snapshot,mode=mode,scope=scope,selection=selection,total=len(rows),queued_at=time.time(),claimed_at=None,heartbeat_at=None,stage='云端任务已排队，等待浏览器执行',current='等待云端浏览器')
            db.execute('INSERT INTO cloud_jobs(id,mode,scope,selection,supplier_ids,state) VALUES(?,?,?,?,?,?)',(job_id,mode,scope,selection,Jsonb([row['id'] for row in rows]),Jsonb(state)))
            self.event(('仅联系方式' if scope=='contacts' else '全量采集')+'云端批次已排队：'+str(len(rows))+' 家',mode=mode)
            from cloud_scaling import set_instances
            set_instances(1)
        return self.job
    def stop_job(self):
        with self.database.transaction(write=True) as db:
            active=db.execute("SELECT state,id,mode,position,status,(status='queued' OR lease_owner IS NULL OR lease_until IS NULL OR lease_until<now()) AS stale FROM cloud_jobs WHERE status IN ('queued','running','stopping') FOR UPDATE").fetchone()
            if active and getattr(self,'username',None) and active['state'].get('requested_by')!=self.username:raise ValueError('仅任务发起人可以停止此批次')
            if active and active['stale']:
                # No live lease: finalise here. A late worker's fenced writes and heartbeats require its lease and an active status, so they now raise LostLease.
                db.execute("UPDATE cloud_jobs SET cancel_requested=true,status='stopped',lease_owner=NULL,lease_until=NULL,state=state||?,updated_at=now() WHERE id=?",(Jsonb({'stage':'云端采集已停止','current':''}),active['id']))
                self.event('云端批次已停止'+('' if active['status']=='queued' else '（后台进程已失联）')+'；已完成 '+str(active['position'])+' / '+str(active['state'].get('total',0))+' 家',mode=active['mode'])
            elif active:db.execute("UPDATE cloud_jobs SET cancel_requested=true,status='stopping',state=state||?,updated_at=now() WHERE id=?",(Jsonb({'stage':'已请求停止云端浏览器，保留完成记录'}),active['id']))
        return self.job
    def claim_job(self,owner):
        with self.database.transaction(write=True) as db:
            row=db.execute("SELECT * FROM cloud_jobs WHERE status='queued' OR (status IN ('running','stopping') AND lease_until<now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
            if not row:return None
            return db.execute("UPDATE cloud_jobs SET status=CASE WHEN cancel_requested THEN 'stopping' ELSE 'running' END,lease_owner=?,lease_until=now()+interval '45 seconds',state=state||?,updated_at=now() WHERE id=? RETURNING *",(owner,Jsonb({'claimed_at':time.time(),'heartbeat_at':time.time()}),row['id'])).fetchone()
    @contextlib.contextmanager
    def fenced(self,job_id,owner):
        with self.database.transaction(write=True) as db:
            row=db.execute("SELECT * FROM cloud_jobs WHERE id=? AND lease_owner=? AND lease_until>now() AND status IN ('running','stopping') FOR UPDATE",(job_id,owner)).fetchone()
            if not row:raise LostLease('云端任务租约已变更，当前进程停止写入')
            yield db,row
    def heartbeat(self,job_id,owner):
        with self.database.transaction() as db:
            row=db.execute("UPDATE cloud_jobs SET lease_until=now()+interval '45 seconds',state=state||?,updated_at=now() WHERE id=? AND lease_owner=? AND lease_until>now() AND status IN ('running','stopping') RETURNING cancel_requested",(Jsonb({'heartbeat_at':time.time()}),job_id,owner)).fetchone()
        if not row:raise LostLease('云端任务租约失效')
        return row['cancel_requested']
    def progress(self,job_id,owner,stage,current=None):
        values={'stage':stage}
        if current is not None:values['current']=current
        with self.fenced(job_id,owner) as (db,row):db.execute('UPDATE cloud_jobs SET state=state||?,updated_at=now() WHERE id=?',(Jsonb(values),job_id))
    def finish_job(self,job_id,owner,status,error=''):
        with self.fenced(job_id,owner) as (db,row):
            if row['cancel_requested'] and status!='failed':status='stopped'
            db.execute('UPDATE cloud_jobs SET status=?,state=state||?,lease_until=NULL,updated_at=now() WHERE id=?',(status,Jsonb({'current':'','error':error,'stage':'模型服务失败，批次停止' if status=='failed' else '云端采集已停止' if status=='stopped' else '云端批次已完成'}),job_id))
            self.event('云端批次'+{'failed':'因模型服务失败结束','stopped':'已停止','completed':'已完成'}[status]+'；已完成 '+str(row['position'])+' / '+str(row['state']['total'])+' 家',mode=row['mode'])
