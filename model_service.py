"""User-chosen Chat Completions provider; configuration and keys stay local."""
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlsplit

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,message,headers,newurl):
        raise ValueError('模型接口发生重定向；请填写最终 API URL 后重试')

def origin(url):
    parsed=urlsplit(url)
    return parsed.scheme.lower(),(parsed.hostname or '').lower(),parsed.port or (443 if parsed.scheme=='https' else 80)

def api_base(url):
    return url.rstrip('/')+('/v1' if urlsplit(url).path in ['', '/'] else '')

class ModelService:
    def __init__(self,path): self.path=Path(path);self.lock=threading.RLock()
    def read(self):
        with self.lock:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
    def public(self):
        data=self.read()
        return {'base_url':data.get('base_url',''),'model':data.get('model',''),'key_configured':bool(data.get('api_key')),'configured':bool(data.get('base_url') and data.get('model')),'tested':data.get('tested',False)}
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
    def complete(self,messages,config=None):
        data=dict(config if config is not None else self.read())
        if not data.get('base_url') or not data.get('model'): raise ValueError('请先在模型服务设置中填写 API URL、模型名和密钥')
        endpoint=api_base(data['base_url']);endpoint=endpoint if endpoint.endswith('/chat/completions') else endpoint+'/chat/completions'
        headers={'Content-Type':'application/json'}
        if data.get('api_key'):headers['Authorization']='Bearer '+data['api_key']
        request=urllib.request.Request(endpoint,data=json.dumps({'model':data['model'],'messages':messages}).encode(),headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request,timeout=45) as response:result=json.load(response)
            content=result['choices'][0]['message'].get('content')
            if not isinstance(content,str) or not content.strip():raise ValueError('模型返回空正文，需检查推理与输出参数')
            return content
        except Exception as error:
            message=str(error)
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
            if data.get('api_key'):message=message.replace(data['api_key'],'[密钥已隐藏]')
            raise ValueError('模型服务调用失败：'+message[:300])
    def test(self):
        config=self.read()
        original_model=config.get('model')
        try:
            available=self.models(config)
            matches=[name for name in available if name.casefold()==str(original_model).casefold()]
            if original_model not in available and len(matches)==1:config=dict(config,model=matches[0])
        except Exception:pass  # Some compatible services do not expose /models.
        answer=self.complete([{'role':'user','content':'只回复 OK，验证连接。'}],config=config)
        return self._finish_test(config,original_model,answer)
    def _finish_test(self,config,original_model,answer):
        with self.lock:
            current=self.read()
            if current.get('revision')!=config.get('revision'):raise ValueError('测试期间配置已变更，请为当前配置重新测试连接')
            current.update(tested=True,model=config['model'],base_url=api_base(config['base_url']));self._write(current)
        return {'ok':True,'reply':answer[:200],'model':config['model'],'model_adjusted':original_model!=config['model']}
    def _write(self,data):
        self.path.parent.mkdir(parents=True,exist_ok=True);temporary=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=str(self.path.parent),delete=False) as stream:
                temporary=Path(stream.name);json.dump(data,stream,ensure_ascii=False)
            os.chmod(temporary,0o600);os.replace(temporary,self.path)
        finally:
            if temporary and temporary.exists():temporary.unlink()
