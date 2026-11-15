"""Vercel daemon: durable Postgres jobs, isolated Moli processes, fenced commits."""
import os
import signal
import socket
import tempfile
import threading
import time
import uuid
from psycopg.types.json import Jsonb
from urllib.parse import urlsplit
from cloud_store import PostgresStore, LostLease
from model_service import ModelService, ModelConfigError, ModelTransientError, PHASE_TEXT, NETWORK_PHASES
from collection import collect_site, CollectionCancelled, AgentServiceError, BatchRuntimeError, ModelTransientSiteError, merge_contacts  # noqa: F401 - re-exported for tests and callers
from job_runner import MODEL_FAILURE_MESSAGE, run_batch

# Re-exported so tests and deployments can keep using this module as the cloud entry.
__all__ = ['CloudBatchTarget','job_rows','execute_job','run_once','main','self_check','self_check_once',
    'run_batch','MODEL_FAILURE_MESSAGE','collect_site','CollectionCancelled','AgentServiceError',
    'BatchRuntimeError','ModelTransientSiteError','merge_contacts','PostgresStore','LostLease',
    'ModelService','ModelConfigError','ModelTransientError','PHASE_TEXT','NETWORK_PHASES']

HEARTBEAT_INTERVAL=5
HEARTBEAT_GRACE=35
PROXY_VARS=['HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy']
PROBE_PADDING='这是一段用于检测云端网络上传能力的填充文本，请忽略。'*380  # json.dumps escapes CJK as \\uXXXX: ~59KB request body, the size of a real step-1 request.


def probe_reason(error):
    phase=getattr(error,'phase',None)
    return PHASE_TEXT.get(phase) or str(error).replace('模型服务调用失败：','',1)[:160]


def egress_report(path):
    """Proxy variable names (never values) and DNS for the model host; hosts and public IPs only, no URL path/query or key."""
    names=[name for name in PROXY_VARS if os.environ.get(name)]
    host=urlsplit(ModelService(path).read().get('base_url','')).hostname or ''
    if not host:dns='无结果'
    else:
        try:
            infos=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
            families={}
            for family,_,_,_,address in infos:families.setdefault('IPv6' if family==socket.AF_INET6 else 'IPv4',[]).append(address[0])
            dns='；'.join(name+' '+'、'.join(dict.fromkeys(ips)) for name,ips in sorted(families.items())) or '无结果'
        except Exception as error:dns='解析失败（'+type(error).__name__+'）'
    return '云端自检：代理变量 '+('已设置 '+'、'.join(names) if names else '未设置')+'；'+(host or '未知主机')+' 解析为 '+dns


SELF_CHECK_DELAYS=[60,120]  # Waits between self-check rounds after a network-phase failure; 3 rounds in total.


def self_check(store,job,owner,log,stop=None):
    """Retry network-phase self-check failures (egress to the model host is intermittent); config errors and success return at once."""
    for round_index in range(len(SELF_CHECK_DELAYS)+1):
        failure=self_check_once(store,job,owner,log,stop)
        if not failure or failure[0]!='egress':return failure
        if round_index>=len(SELF_CHECK_DELAYS):
            return 'egress',failure[1].replace('；批次未开始','；已自检 '+str(round_index+1)+' 次均未连通，批次未开始',1)
        delay=SELF_CHECK_DELAYS[round_index]
        log('云端自检：第 '+str(round_index+1)+' 次未连通，'+str(delay)+' 秒后重试（共 '+str(len(SELF_CHECK_DELAYS)+1)+' 次）')
        if stop is not None:
            if stop.wait(delay):raise CollectionCancelled()
        else:time.sleep(delay)


def self_check_once(store,job,owner,log,stop=None):
    """Prove this daemon process can reach the model before touching any supplier; returns None or (kind, message)."""
    with tempfile.TemporaryDirectory(prefix='scout-selfcheck-') as folder:
        try:path=store.model_service.materialize(folder,snapshot=job['state'].get('_model_config',''))
        except LostLease:raise
        except Exception as error:return 'config','云端后台无法读取本批次的模型配置：'+str(error)[:200]
        try:log(egress_report(path))
        except LostLease:raise
        except Exception:pass
        service=ModelService(path);instruction={'role':'user','content':'只回复 OK'}
        for label,messages in [('小请求',[instruction]),('60KB 请求',[{'role':'system','content':PROBE_PADDING},instruction])]:
            if stop is not None and stop.is_set():raise CollectionCancelled()
            started=time.monotonic()
            try:service.complete(messages,attempts=1);error=None
            except (ModelConfigError,ModelTransientError) as caught:error=caught
            except Exception as caught:error=ModelTransientError('模型服务调用失败：'+type(caught).__name__)
            elapsed='%.1fs'%(time.monotonic()-started)
            log('云端自检：'+label+(' 成功 ' if error is None else ' 失败（'+probe_reason(error)+'）')+elapsed)
            if error is None:continue
            if label=='小请求':
                if isinstance(error,ModelConfigError):return 'config',str(error)[:400]
                if getattr(error,'phase',None) in NETWORK_PHASES:return 'egress','云端后台无法连接模型服务：'+probe_reason(error)+'；批次未开始，已保存自检结果'
                log('云端自检：已连通模型服务，但模型暂时未正常回复，批次继续，由逐站失败计数决定是否停止');break  # HTTP 429/5xx or slow reply: the network path works.
            if getattr(error,'phase',None) in NETWORK_PHASES:return 'egress','云端后台无法上传较大的请求（疑似网络 MTU 问题）：'+probe_reason(error)+'；批次未开始，已保存自检结果'
            log('云端自检：60KB 请求 上传成功，模型返回异常，已忽略')  # Any HTTP reply or reply-wait timeout proves the upload got through.
    return None

class CloudBatchTarget:
    """Persist one cloud job run; implements the job_runner.BatchTarget hooks."""
    def __init__(self,store,job,owner,stop):
        self.store=store
        self.job=job
        self.owner=owner
        self.stop=stop
    def active(self):
        return not self.stop.is_set()
    def log(self,message,row=None):
        if self.stop.is_set():return
        try:self.store.progress(self.job['id'],self.owner,message,row['name'] if row else '')
        except LostLease:raise
        except Exception:pass
        try:self.store.event(message,mode=self.job['mode'])
        except Exception:pass  # Events are best-effort; the scoped store keeps them in the owner's log.
    def start_site(self,row):
        self.log('准备云端访问 '+row['url'],row)
    def collect(self,row,mode,scope):
        if mode=='demo':
            if self.stop.wait(.35):raise CollectionCancelled()
            return {key:row[key] for key in ['contacts','offers','sources','warnings']}
        with tempfile.TemporaryDirectory(prefix='scout-provider-') as folder:
            config=self.store.model_service.materialize(folder,snapshot=self.job['state'].get('_model_config',''))
            return collect_site(row['url'],lambda message:self.log(message,row),self.stop,scope,config,self.store.rules() if scope=='all' else [])
    def fenced(self):
        return self.store.fenced(self.job['id'],self.owner)
    def advance(self,db,index):
        db.execute('UPDATE cloud_jobs SET position=?,updated_at=now() WHERE id=?',(index+1,self.job['id']))
    def save_success(self,row,result,scope,index):
        with self.fenced() as (db,current):
            if current['cancel_requested'] or self.stop.is_set():raise CollectionCancelled()
            if scope=='all':
                prior=self.store.get(row['id'])
                self.store.save_result(row['id'],dict(result,contacts=merge_contacts(prior['contacts'],result.get('contacts')),sources=prior['sources']+result.get('sources',[])))
            self.store.save_contacts(row['id'],result)
            self.advance(db,index)
    def save_model_transient(self,row,error,scope,index):
        with self.fenced() as (db,current):
            if current['cancel_requested'] or self.stop.is_set():raise CollectionCancelled()
            partial=dict(error.partial or {});found=any((partial.get('contacts') or {}).values())
            # Contacts-only save keeps checked_at/status untouched; never 'found', so contacts-scope '继续未完成' still selects this site, ordered after untried ones.
            self.store.save_contacts(row['id'],dict(partial,contacts=partial.get('contacts') or {},sources=partial.get('sources') or [],warnings=list(dict.fromkeys((partial.get('warnings') or [])+[str(error)[:400]])),contact_status='incomplete' if found else 'blocked',contact_summary='模型服务暂时不可用，本站稍后重试',scope=scope))
            self.advance(db,index)
    def save_agent_failure(self,row,error,scope,index):
        with self.fenced() as (db,current):
            self.store.save_contacts(row['id'],error.partial or {'contacts':{},'sources':[],'warnings':[str(error)],'contact_status':'blocked','contact_summary':'模型服务失败，批次已停止','scope':scope})
            self.store.finish_job(self.job['id'],self.owner,'failed',str(error))
    def save_site_failure(self,row,error,scope,index):
        with self.fenced() as (db,current):
            if current['cancel_requested'] or self.stop.is_set():raise CollectionCancelled()
            self.store.save_contacts(row['id'],{'contacts':{},'sources':[],'warnings':[str(error)[:500]],'contact_status':'blocked','contact_summary':str(error)[:500],'scope':scope})
            self.advance(db,index)
    def batch_failed(self,error):
        self.store.finish_job(self.job['id'],self.owner,'failed',str(error)[:400])  # Environment problem: do not touch any supplier row.
    def model_failures_exhausted(self):
        self.store.finish_job(self.job['id'],self.owner,'failed',MODEL_FAILURE_MESSAGE)


def job_rows(store,job,owner,shutdown):
    """Yield the unprocessed suppliers, checking for cancellation between sites."""
    for supplier_id in job['supplier_ids'][job['position']:]:
        with store.fenced(job['id'],owner) as (db,current):
            if current['cancel_requested'] or shutdown.is_set():raise CollectionCancelled()
        yield store.get(supplier_id)


def execute_job(store,job,owner,shutdown):
    stop=threading.Event();lost=threading.Event();finished=threading.Event()
    def renew():
        last_ok=time.monotonic()
        while not finished.wait(HEARTBEAT_INTERVAL):
            try:
                if store.heartbeat(job['id'],owner) or shutdown.is_set():stop.set()
                last_ok=time.monotonic()
            except LostLease:lost.set();stop.set();return
            except Exception:
                # Transient DB/network error: retry next tick; grace counts from the last successful renewal so we stop before the 45s lease lapses.
                if time.monotonic()-last_ok>=HEARTBEAT_GRACE:lost.set();stop.set();return
    heartbeat=threading.Thread(target=renew,daemon=True);heartbeat.start()
    target=CloudBatchTarget(store,job,owner,stop)
    def job_log(message):
        if stop.is_set():return
        try:store.progress(job['id'],owner,message,'')
        except LostLease:raise
        except Exception:pass
        try:store.event(message,mode=job['mode'])
        except Exception:pass  # Events are best-effort; the scoped store keeps them in the owner's log.
    try:
        if job['mode']!='demo':
            with store.fenced(job['id'],owner) as (db,current):
                if current['cancel_requested'] or shutdown.is_set():raise CollectionCancelled()
            failure=self_check(store,job,owner,job_log,stop)
            with store.fenced(job['id'],owner) as (db,current):
                if current['cancel_requested'] or stop.is_set():raise CollectionCancelled()
            if failure:
                store.finish_job(job['id'],owner,'failed',failure[1]);return
        stopped=run_batch(job_rows(store,job,owner,shutdown),job['mode'],job['scope'],target,start=job['position'],fatal=(LostLease,))
        if not stopped:store.finish_job(job['id'],owner,'completed')
    except CollectionCancelled:
        if not lost.is_set():
            if shutdown.is_set():
                # Release safely for another deployment to resume at the last commit.
                with store.fenced(job['id'],owner) as (db,current):
                    status='stopped' if current['cancel_requested'] else 'queued'
                    db.execute('UPDATE cloud_jobs SET status=?,lease_owner=NULL,lease_until=NULL,state=state||?,updated_at=now() WHERE id=?',(status,Jsonb({'queued_at':time.time(),'claimed_at':None,'heartbeat_at':None}),job['id']))
            else:store.finish_job(job['id'],owner,'stopped')
    except LostLease:pass
    finally:finished.set();stop.set();heartbeat.join(timeout=6)


def run_once(store,shutdown):
    """Claim and run at most one queued job. Returns True when a job was handled."""
    owner=uuid.uuid4().hex
    job=store.claim_job(owner)
    if not job:return False
    if job.get('owner'):execute_job(store.for_user(job['owner']),job,owner,shutdown)
    # No requested_by means no account context: never run it under the system store, where
    # the database default would attribute that member's results to 小姜.
    else:store.fail_orphan_job(job['id'],'任务缺少归属账号，已终止')
    return True


def main():
    shutdown=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:shutdown.set());signal.signal(signal.SIGINT,lambda *_:shutdown.set())
    store=PostgresStore();idle_since=time.monotonic()
    print('Supplier Scout cloud worker ready',flush=True)
    while not shutdown.is_set():
        try:
            if run_once(store,shutdown):
                idle_since=time.monotonic()
            else:
                from cloud_scaling import stop_if_idle
                if time.monotonic()-idle_since>=60 and stop_if_idle(store):break
                shutdown.wait(3)
        except Exception as error:
            # Provider config and DB URLs are never logged.
            print('Worker cycle failed: '+type(error).__name__,flush=True);shutdown.wait(5)

if __name__=='__main__':main()
