"""Site collection subprocess protocol and the helpers both stores share."""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from browser_agent import AgentServiceError, ModelTransientSiteError

ROOT=Path(__file__).resolve().parent

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def encode(value): return json.dumps(value,ensure_ascii=False,allow_nan=False)

def log_exception(where):
    """Keep the stack trace server-side; clients only get a generic 500 message."""
    try: traceback.print_exc(file=sys.stderr); sys.stderr.write('500 in '+where+'\n'); sys.stderr.flush()
    except Exception: pass

class CollectionCancelled(Exception): pass
class BatchRuntimeError(ValueError):
    """Browser runtime (Moli/Playwright) missing or refusing to start: stop the batch instead of marking every site blocked."""

# Hard per-site cut-off; stays above browser_agent's soft deadline - reserve + worst step tail for each scope (see the budget comment there).
SITE_HARD_TIMEOUT={'all':840,'contacts':480}
def _terminate_tree(process,job=None):
    """Bounded cleanup; always reap the worker, and signal its POSIX descendants."""
    if process.pid<=0:raise ValueError('Invalid collection process id')
    if os.name=='nt':
        if job is not None:
            job.close()
            process.wait(timeout=.5)
            return
        if process.poll() is None:
            subprocess.run(['taskkill','/T','/F','/PID',str(process.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2,check=True)
        process.wait(timeout=.5)
        return
    try:os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:process.wait(timeout=.5)
    except subprocess.TimeoutExpired:pass
    # The parent may have exited while browser descendants still hold the group.
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    process.wait(timeout=.5)

def collect_site(url,log,stop_event,scope="all",model_path=None,rules=None):
    if stop_event.is_set(): raise CollectionCancelled()
    command=[sys.executable,str(ROOT/'collection_worker.py'),url,scope,str(model_path or ROOT/'data'/'model-service.json'),encode(rules or [])]
    options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP,'stdin':subprocess.PIPE} if os.name=='nt' else {'start_new_session':True}
    if os.name=='nt':command.append('--parent-gate')
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',**options)
    job=None
    if os.name=='nt':
        try:
            from windows_job import WindowsJob
            job=WindowsJob(process)
            process.stdin.write('ready\n');process.stdin.flush();process.stdin.close()
        except Exception as error:
            if job is not None:job.close()
            if process.poll() is None:process.kill()
            process.wait(timeout=1);process.stdin.close();process.stdout.close()
            raise BatchRuntimeError('Windows采集进程保护初始化失败，批次未开始') from error
    messages=queue.Queue()
    def read_output():
        try:
            for line in process.stdout: messages.put(line)
        finally:
            process.stdout.close()
            messages.put(None)
    threading.Thread(target=read_output,daemon=True).start()
    limit=SITE_HARD_TIMEOUT.get(scope,SITE_HARD_TIMEOUT['all']);deadline=time.monotonic()+limit;exited_at=None
    try:
        while True:
            if stop_event.is_set(): raise CollectionCancelled()
            if time.monotonic()>deadline: raise ValueError('单站 Agent 超过 '+str(limit//60)+' 分钟，已停止；可稍后单独继续')
            try: line=messages.get(timeout=.1)
            except queue.Empty:
                if process.poll() is not None:
                    if exited_at is None:exited_at=time.monotonic()
                    elif time.monotonic()-exited_at>=.5:raise ValueError('采集进程意外结束，请查看该网站后重试')
                continue
            if line is None: raise ValueError('采集进程意外结束，请查看该网站后重试')
            try: message=json.loads(line)
            except ValueError: continue
            if message.get('kind')=='log': log(message['message'])
            elif message.get('kind')=='result':
                grace=time.monotonic()+2
                while process.poll() is None and not stop_event.is_set():
                    remaining=grace-time.monotonic()
                    if remaining<=0:break
                    try:process.wait(timeout=min(.05,remaining))
                    except subprocess.TimeoutExpired:pass
                return message['data']
            elif message.get('kind')=='error':
                if message.get('runtime'): raise BatchRuntimeError(message['message'])
                if message.get('fatal'): raise AgentServiceError(message['message'],message.get('partial'))
                if message.get('model_transient'): raise ModelTransientSiteError(message['message'],message.get('partial'))
                raise ValueError(message['message'])
    finally:
        try:_terminate_tree(process,job)
        except Exception as error:
            # Cleanup cannot replace an already received result or the stop/timeout.
            try:log('采集进程清理未完成：'+type(error).__name__)
            except Exception:pass

QUOTE_CHANGED_WARNING='报价已变化，建议重新复核'

def manual_decision(row):
    return str(row.get('reason') or '').startswith('人工复核：')

def merge_sources(*groups):
    merged=[];known=set()
    for group in groups:
        for item in group:
            key=(item.get('url'),item.get('excerpt'))
            if key not in known:merged.append(item);known.add(key)
    return merged

def merge_contacts(*groups):
    """Merge contact lists by kind, dropping repeats that differ only by case and keeping the first spelling and order."""
    merged={}
    for group in groups:
        for kind,values in (group or {}).items():
            kept=merged.setdefault(kind,[])
            known={str(value).casefold() for value in kept}
            for value in values or []:
                folded=str(value).casefold()
                if folded not in known:kept.append(value);known.add(folded)
    return merged

def quote_fingerprint(offers):
    # Evidence refreshes and evaluation outcomes are not commercial changes.
    fields=('model','model_name','ratio','input_price','output_price','cache_price','currency','unit','group','group_multiplier','resource_form','resource_type','price_text','notes','conditions')
    return {json.dumps({key:offer[key] for key in fields if offer.get(key) is not None},sort_keys=True,ensure_ascii=False) for offer in offers}
