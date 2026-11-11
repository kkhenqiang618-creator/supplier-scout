"""Vercel daemon: durable Postgres jobs, isolated Moli processes, fenced commits."""
import signal
import tempfile
import threading
import time
import uuid
from psycopg.types.json import Jsonb
from cloud_store import PostgresStore, LostLease
from server import collect_site, CollectionCancelled, AgentServiceError


def execute_job(store,job,owner,shutdown):
    stop=threading.Event();lost=threading.Event();finished=threading.Event()
    def renew():
        while not finished.wait(5):
            try:
                if store.heartbeat(job['id'],owner) or shutdown.is_set():stop.set()
            except Exception:lost.set();stop.set();return
    heartbeat=threading.Thread(target=renew,daemon=True);heartbeat.start()
    try:
        for index in range(job['position'],len(job['supplier_ids'])):
            with store.fenced(job['id'],owner) as (db,current):
                if current['cancel_requested'] or shutdown.is_set():raise CollectionCancelled()
            row=store.get(job['supplier_ids'][index])
            store.progress(job['id'],owner,'准备云端访问 '+row['url'],row['name'])
            def log(message):
                if stop.is_set():return
                store.progress(job['id'],owner,message,row['name'])
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
                    db.execute('UPDATE cloud_jobs SET status=?,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE id=?',(status,job['id']))
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
