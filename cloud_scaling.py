"""Run the browser daemon only while jobs exist; avoid idle Hobby memory usage."""
import json
import contextvars
from pathlib import Path
import os
import urllib.request
import urllib.error

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):raise ValueError('云端浏览器管理接口发生重定向')

REQUEST_TOKEN=contextvars.ContextVar('scout_vercel_oidc',default='')

def token():
    explicit=os.environ.get('SCOUT_VERCEL_SCALER_TOKEN')
    if explicit:return explicit
    current=REQUEST_TOKEN.get()
    if current:return current
    path=os.environ.get('VERCEL_OIDC_TOKEN_FILE')
    if path:
        try:return Path(path).read_text().strip()
        except OSError:pass
    return os.environ.get('VERCEL_OIDC_TOKEN','')

# Daemon region; must match vercel.json daemons.browser-worker.instances. Override with SCOUT_DAEMON_REGION (e.g. hkg1) only together with vercel.json.
DAEMON_REGION=os.environ.get('SCOUT_DAEMON_REGION') or 'sin1'

def enabled():return bool(token())

def set_instances(count):
    if not enabled():return False
    project=os.environ['SCOUT_VERCEL_PROJECT_ID'];team=os.environ['SCOUT_VERCEL_TEAM_ID']
    url='https://api.vercel.com/v1/projects/'+project+'/daemons/browser-worker/instances?teamId='+team
    request=urllib.request.Request(url,data=json.dumps({DAEMON_REGION:count}).encode(),method='PUT',headers={'Authorization':'Bearer '+token(),'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=12) as response:result=json.load(response)
        if not result.get('ok'):raise ValueError('云端浏览器启动请求未通过')
        return True
    except urllib.error.HTTPError as error:raise ValueError('云端浏览器未能启动/停止（HTTP '+str(error.code)+'），请检查云端运行配置') from None
    except (urllib.error.URLError,TimeoutError):raise ValueError('云端浏览器启动/停止请求超时，请稍后重试') from None

def stop_if_idle(store):
    if not enabled():return False
    with store.database.transaction(write=True) as db:
        if db.execute("SELECT id FROM cloud_jobs WHERE status IN ('queued','running','stopping')").fetchone():return False
        return set_instances(0)
