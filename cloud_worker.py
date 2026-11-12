"""Vercel daemon: durable Postgres jobs, isolated Moli processes, fenced commits."""
import signal
import tempfile
import threading
import time
import uuid
from psycopg.types.json import Jsonb
from cloud_store import PostgresStore, LostLease
from server import collect_site, CollectionCancelled, AgentServiceError
try:from server import ModelTransientSiteError
except ImportError:
    class ModelTransientSiteError(ValueError):partial=None

MODEL_FAILURE_LIMIT=3
HEARTBEAT_INTERVAL=5
HEARTBEAT_GRACE=35


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
    model_failures=0
    try:
        for index in range(job['position'],len(job['supplier_ids'])):
            with store.fenced(job['id'],owner) as (db,current):
                if current['cancel_requested'] or shutdown.is_set():raise CollectionCancelled()
            row=store.get(job['supplier_ids'][index])
            def log(message):
                if stop.is_set():return
                try:store.progress(job['id'],owner,message,row['name'])
                except LostLease:raise
                except Exception:pass  # Step logs are best-effort; the heartbeat thread decides lease loss.
            log('准备云端访问 '+row['url'])
            try:
                if job['mode']=='demo':
                    if stop.wait(.35):raise CollectionCancelled()
                    result={key:row[key] for key in ['contacts','offers','sources','warnings']}
                else:
                    with tempfile.TemporaryDirectory(prefix='scout-provider-') as folder:
                        config=store.model_service.materialize(folder,snapshot=job['state'].get('_model_config',''))
                        result=collect_site(row['url'],log,stop,job['scope'],config,store.rules() if job['scope']=='all' else [])
                with store.fenced(job['id'],owner) as (db,current):
                    if current['cancel_requested'] or stop.is_set():raise CollectionCancelled()
                    if job['scope']=='all':
                        prior=store.get(row['id']);merged={key:list(value) for key,value in prior['contacts'].items()}
                        for kind,values in result.get('contacts',{}).items():merged[kind]=sorted(set(merged.get(kind,[])+values))
                        store.save_result(row['id'],dict(result,contacts=merged,sources=prior['sources']+result.get('sources',[])))
                    store.save_contacts(row['id'],result)
                    db.execute('UPDATE cloud_jobs SET position=?,updated_at=now() WHERE id=?',(index+1,job['id']))
                model_failures=0
            except ModelTransientSiteError as error:
                with store.fenced(job['id'],owner) as (db,current):
                    if current['cancel_requested'] or stop.is_set():raise CollectionCancelled()
                    partial=dict(error.partial or {});found=any((partial.get('contacts') or {}).values())
                    # Contacts-only save keeps checked_at/status untouched; never 'found', so contacts-scope '继续未完成' still selects this site, ordered after untried ones.
                    store.save_contacts(row['id'],dict(partial,contacts=partial.get('contacts') or {},sources=partial.get('sources') or [],warnings=list(dict.fromkeys((partial.get('warnings') or [])+[str(error)[:400]])),contact_status='incomplete' if found else 'blocked',contact_summary='模型服务暂时不可用，本站稍后重试',scope=job['scope']))
                    db.execute('UPDATE cloud_jobs SET position=?,updated_at=now() WHERE id=?',(index+1,job['id']))
                model_failures+=1
                if model_failures>=MODEL_FAILURE_LIMIT:
                    store.finish_job(job['id'],owner,'failed','模型服务连续 '+str(MODEL_FAILURE_LIMIT)+' 家不可用，批次已暂停；可稍后继续')
                    return
            except AgentServiceError as error:
                with store.fenced(job['id'],owner) as (db,current):
                    store.save_contacts(row['id'],error.partial or {'contacts':{},'sources':[],'warnings':[str(error)],'contact_status':'blocked','contact_summary':'模型服务失败，批次已停止','scope':job['scope']})
                    store.finish_job(job['id'],owner,'failed',str(error))
                return
            except CollectionCancelled:raise
            except LostLease:raise
            except Exception as error:
                with store.fenced(job['id'],owner) as (db,current):
                    if current['cancel_requested'] or stop.is_set():raise CollectionCancelled()
                    store.save_contacts(row['id'],{'contacts':{},'sources':[],'warnings':[str(error)[:500]],'contact_status':'blocked','contact_summary':str(error)[:500],'scope':job['scope']})
                    db.execute('UPDATE cloud_jobs SET position=?,updated_at=now() WHERE id=?',(index+1,job['id']))
        store.finish_job(job['id'],owner,'completed')
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


def main():
    shutdown=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:shutdown.set());signal.signal(signal.SIGINT,lambda *_:shutdown.set())
    store=PostgresStore();idle_since=time.monotonic()
    print('Supplier Scout cloud worker ready',flush=True)
    while not shutdown.is_set():
        try:
            owner=uuid.uuid4().hex
            job=store.claim_job(owner)
            if job:
                execute_job(store.for_user(job['owner']) if job.get('owner') else store,job,owner,shutdown);idle_since=time.monotonic()
            else:
                from cloud_scaling import stop_if_idle
                if time.monotonic()-idle_since>=60 and stop_if_idle(store):break
                shutdown.wait(3)
        except Exception as error:
            # Provider config and DB URLs are never logged.
            print('Worker cycle failed: '+type(error).__name__,flush=True);shutdown.wait(5)

if __name__=='__main__':main()
