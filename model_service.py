"""User-chosen Chat Completions provider; configuration and keys stay local."""
import base64
import json
import os
import random
import re
import socket
import ssl
import tempfile
import time
import threading
import uuid
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlsplit

class ModelConfigError(ValueError):
    """Fatal for a batch: credentials, URL, model name or redirect must be fixed by the user."""
class ModelTransientError(ValueError):
    """Provider/network failure that ends one site, not the batch; .retry_after seconds (or None), .empty for a blank reply (retried once, then ModelConfigError), .retryable False for oversized/rejected page prompts."""
    def __init__(self,message,retry_after=None,empty=False,retryable=True):super().__init__(message);self.retry_after=retry_after;self.empty=empty;self.retryable=retryable

PAGE_REJECTED=re.compile(r'context[ _-]?length|context window|maximum context|too many tokens|token limit|max_tokens|prompt is too long|input is too long|content[ _-]?(policy|filter|management)|moderation|safety|sensitive|敏感|违规|上下文|长度超',re.I)
RETRY_DELAYS=[2,6];RETRY_AFTER_CAP=30;RETRY_WAIT_CAP=40  # Total sleep stays bounded; the parent stop path kills the worker process group.

def retry_after(headers):
    try:value=float((headers or {}).get('Retry-After') or '')
    except (TypeError,ValueError,AttributeError):return None
    return max(0.0,min(RETRY_AFTER_CAP,value))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,message,headers,newurl):
        raise ValueError('模型接口发生重定向；请填写最终 API URL 后重试')

KEY_PREFIX='dpapi:'
KEY_UNREADABLE='本机保存的 API Key 无法解密（可能更换了 Windows 用户或电脑），请重新填写'

def _dpapi(data,protect):
    """Windows CryptProtectData/CryptUnprotectData: only the saving Windows user can read the blob."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_char))]
    crypt32=ctypes.WinDLL('crypt32',use_last_error=True);kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
    buffer=ctypes.create_string_buffer(data,len(data))
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_char)));result=Blob()
    call=crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    call.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    call.restype=wintypes.BOOL
    if not call(ctypes.byref(source),None,None,None,None,0x1,ctypes.byref(result)):raise OSError(ctypes.get_last_error(),'Windows 凭据加密失败')
    try:return ctypes.string_at(result.data,result.size)
    finally:
        kernel32.LocalFree.argtypes=[ctypes.c_void_p];kernel32.LocalFree(result.data)

def protect_key(key):
    """On Windows, store the key as a DPAPI blob: os.chmod(0o600) is a no-op there (it only toggles the read-only bit)."""
    if os.name!='nt' or not key:return key
    return KEY_PREFIX+base64.b64encode(_dpapi(key.encode('utf-8'),True)).decode('ascii')

def unprotect_key(value):
    if not value.startswith(KEY_PREFIX):return value,''
    try:return _dpapi(base64.b64decode(value[len(KEY_PREFIX):]),False).decode('utf-8'),''
    except Exception:return '',KEY_UNREADABLE

def origin(url):
    parsed=urlsplit(url)
    return parsed.scheme.lower(),(parsed.hostname or '').lower(),parsed.port or (443 if parsed.scheme=='https' else 80)

def api_base(url):
    return url.rstrip('/')+('/v1' if urlsplit(url).path in ['', '/'] else '')

class ModelService:
    def __init__(self,path): self.path=Path(path);self.lock=threading.RLock()
    def read(self):
        with self.lock:
            data=json.loads(self.path.read_text()) if self.path.exists() else {}
        raw=str(data.get('api_key') or '');key,error=unprotect_key(raw)
        data['api_key']=key;data['key_protected']=raw.startswith(KEY_PREFIX)
        if error:data['key_error']=error
        return data
    def public(self):
        data=self.read()
        return {'base_url':data.get('base_url',''),'model':data.get('model',''),'key_configured':bool(data.get('api_key')),'configured':bool(data.get('base_url') and data.get('model')),'tested':data.get('tested',False),'key_protected':bool(data.get('key_protected')),'key_error':data.get('key_error','')}
    def save(self,data):
        url=str(data.get('base_url','')).strip().rstrip('/');model=str(data.get('model','')).strip();parts=urlsplit(url)
        if parts.scheme not in ['http','https'] or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment: raise ValueError('请填写模型服务的 API 地址，例如 https://服务域名/v1')
        if not model: raise ValueError('请填写调用模型名称')
        with self.lock:
            old=self.read();same_origin=origin(url)==origin(old.get('base_url',''))
            key=str(data.get('api_key','')).strip() or (old.get('api_key','') if same_origin else '')
            self._write({'base_url':api_base(url),'model':model,'api_key':key,'tested':False,'revision':uuid.uuid4().hex})
            return self.public()
    def models(self,config=None):
        data=config if config is not None else self.read()
        base=api_base(data.get('base_url',''))
        if base.endswith('/chat/completions'):base=base[:-len('/chat/completions')]
        headers={'Authorization':'Bearer '+data['api_key']} if data.get('api_key') else {}
        request=urllib.request.Request(base+'/models',headers=headers)
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=15) as response:result=json.load(response)
        return [item['id'] for item in result.get('data',[]) if isinstance(item,dict) and isinstance(item.get('id'),str)]
    def complete(self,messages,config=None,attempts=3):
        """Transient errors are retried (empty/non-JSON body only once); raises ModelConfigError or ModelTransientError, both ValueError."""
        waited=0.0
        for attempt in range(attempts):
            try:return self._complete_once(messages,config)
            except ModelTransientError as error:
                if error.empty and (attempt>=1 or attempt+1>=attempts):raise ModelConfigError(str(error)) from error  # Blank twice: reasoning/output parameters, not an outage.
                if attempt+1>=attempts or not error.retryable or waited>=RETRY_WAIT_CAP:raise
                delay=error.retry_after if error.retry_after is not None else RETRY_DELAYS[min(attempt,len(RETRY_DELAYS)-1)]*random.uniform(.8,1.2)
                delay=min(delay,RETRY_WAIT_CAP-waited);waited+=delay;time.sleep(delay)
    def _complete_once(self,messages,config=None):
        data=dict(config if config is not None else self.read())
        if not data.get('base_url') or not data.get('model'): raise ModelConfigError('请先在模型服务设置中填写 API URL、模型名和密钥')
        endpoint=api_base(data['base_url']);endpoint=endpoint if endpoint.endswith('/chat/completions') else endpoint+'/chat/completions'
        headers={'Content-Type':'application/json'}
        if data.get('api_key'):headers['Authorization']='Bearer '+data['api_key']
        request=urllib.request.Request(endpoint,data=json.dumps({'model':data['model'],'messages':messages}).encode(),headers=headers)
        empty=False
        try:
            with urllib.request.build_opener(NoRedirect()).open(request,timeout=45) as response:
                try:result=json.load(response);content=result['choices'][0]['message'].get('content')
                except (ValueError,KeyError,IndexError,TypeError,AttributeError):raise ConnectionError('模型返回内容不是有效的 JSON 响应')
            if not isinstance(content,str) or not content.strip():empty=True;raise ValueError('模型返回空正文，需检查推理与输出参数')
            return content
        except Exception as error:
            message=str(error);kind=ModelTransientError if empty else ModelConfigError;wait=None;retryable=True
            if isinstance(error,urllib.error.HTTPError):
                raw=error.read(8192).decode('utf-8',errors='replace')
                try:
                    detail=json.loads(raw).get('error',{})
                    detail=detail.get('message',str(detail)) if isinstance(detail,dict) else str(detail)
                except (ValueError,AttributeError):detail=''
                message='HTTP '+str(error.code)+'：'+(detail or error.reason)
                if 'No available accounts' in detail:message+='。该模型无可用账号；请核对服务商的准确模型名（区分大小写），或联系服务商检查模型线路'
                elif error.code==401:message+='。请核对 API Key'
                elif error.code==404:message+='。请核对 API URL，通常以 /v1 结尾'
                if 'No available accounts' not in detail and (error.code in [408,409,425,429] or error.code>=500):kind=ModelTransientError;wait=retry_after(error.headers)
                elif error.code==413 or (error.code in [400,422] and PAGE_REJECTED.search(detail or '')):kind=ModelTransientError;retryable=False  # This page's prompt is too large or rejected; end the site, keep the batch.
            elif isinstance(error,(socket.timeout,TimeoutError,ssl.SSLError,urllib.error.URLError,OSError,ConnectionError)) or type(error).__module__=='http.client':kind=ModelTransientError
            if data.get('api_key'):message=message.replace(data['api_key'],'[密钥已隐藏]')
            if kind is ModelTransientError:raise ModelTransientError('模型服务调用失败：'+message[:300],wait,empty,retryable)
            raise ModelConfigError('模型服务调用失败：'+message[:300])
    def test(self):
        config=self.read()
        original_model=config.get('model')
        try:
            available=self.models(config)
            matches=[name for name in available if name.casefold()==str(original_model).casefold()]
            if original_model not in available and len(matches)==1:config=dict(config,model=matches[0])
        except Exception:pass  # Some compatible services do not expose /models.
        answer=self.complete([{'role':'user','content':'只回复 OK，验证连接。'}],config=config,attempts=1)  # Interactive path stays within the 120s function limit.
        return self._finish_test(config,original_model,answer)
    def _finish_test(self,config,original_model,answer):
        with self.lock:
            current=self.read()
            if current.get('revision')!=config.get('revision'):raise ValueError('测试期间配置已变更，请为当前配置重新测试连接')
            current.update(tested=True,model=config['model'],base_url=api_base(config['base_url']));self._write(current)
        return {'ok':True,'reply':answer[:200],'model':config['model'],'model_adjusted':original_model!=config['model']}
    def _write(self,data):
        data={key:value for key,value in data.items() if key not in ('key_error','key_protected')}
        if data.get('api_key'):
            try:data['api_key']=protect_key(str(data['api_key']))
            except OSError as error:raise ValueError('API Key 无法通过 Windows 凭据加密保存：'+str(error))
        self.path.parent.mkdir(parents=True,exist_ok=True);temporary=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=str(self.path.parent),delete=False) as stream:
                temporary=Path(stream.name);json.dump(data,stream,ensure_ascii=False)
            if os.name!='nt':os.chmod(temporary,0o600)  # Windows ignores the mode; DPAPI above protects the value instead.
            os.replace(temporary,self.path)
        finally:
            if temporary and temporary.exists():temporary.unlink()
