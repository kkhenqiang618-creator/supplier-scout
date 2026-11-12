"""Contact collection failure explanations, also inferred for older stored results."""
import re

# Labels describe observations, never assert that the supplier has no contact channel.
PATTERNS=[
 ('dns','域名解析失败',r'域名无法解析|ERR_NAME_NOT_RESOLVED|Name or service not known|nodename nor servname',True),
 ('tls','网站证书异常',r'ERR_CERT_|SSL:|certificate verify failed|证书',False),
 ('timeout','网站访问超时',r'ERR_TIMED_OUT|连接超时|加载超时|Connection timed out|page.goto: Timeout',True),
 ('http','网站响应异常',r'ERR_HTTP_RESPONSE_CODE_FAILURE|HTTP (?:4|5)\d\d',True),
 ('network','网站连接失败',r'ERR_CONNECTION_|ERR_FAILED|Connection reset|网络连接',True),
 ('robots','页面访问规则限制',r'robots\.txt 不允许',False),
 ('network_policy','地址安全检查未通过',r'禁止访问内网|回环或保留地址|public address|公网',False),
 ('interaction','页面操作被只读保护拦截',r'该操作超出只读|控件编号已过期',False),
 ('navigation','页面跳转或链接检查未完成',r'Execution context was destroyed|未在已观察的页面链接|跳转期间',True),
 ('challenge','停留在网站验证页面',r'网站验证页面|Cloudflare.*等待|安全验证.*等待',True),
 ('loading_timeout','页面仍在加载',r'页面加载等待已达上限',True),
 ('repeated_action','已停止重复无效操作',r'重复操作|连续等待无新内容',True),
 ('budget','本站检查预算已用完',r'单站动作预算|本站检查时间已用完',True),
 ('model','模型服务未完成',r'模型服务.*(?:失败|不可用)|模型本轮未返回有效JSON',True),
 ('render','页面内容未完整显示',r'均未能完整渲染|页面渲染不完整',True),
]

def collection_diagnostics(result,pages=(),fallback_url=''):
    issues=[];seen=set();steps=result.get('steps') or []
    last=steps[-1] if steps else {};default_url=last.get('url') or fallback_url;browser=last.get('browser') or ''
    def add(code,label,detail,url='',retryable=True,http_status=None):
        key=(code,url or default_url)
        if key in seen:return
        seen.add(key);issue={'code':code,'label':label,'detail':str(detail)[:500],'url':url or default_url,'browser':browser,'retryable':retryable}
        if http_status is not None:issue['http_status']=http_status
        issues.append(issue)
    for page in pages:
        status=page.get('http_status');url=page.get('url','')
        if isinstance(status,int) and status>=400:add('http','网站返回 HTTP '+str(status),'入口或页面返回错误响应',url,status>=500,status)
        if page.get('challenge'):add('challenge','停留在网站验证页面','网站验证尚未进入业务页面',url)
        elif page.get('loading_wait_exhausted'):add('loading_timeout','页面仍在加载','页面加载等待已达上限，可稍后继续检查',url)
    for warning in result.get('warnings') or []:
        http=re.search(r'网站返回 HTTP (\d{3})[：:](https?://\S+)',str(warning))
        if http:
            status=int(http.group(1));add('http','网站返回 HTTP '+str(status),warning,http.group(2),status>=500,status);continue
        if '页面加载等待已达上限' in str(warning) and any(i['code']=='loading_timeout' for i in issues):continue
        for code,label,pattern,retryable in PATTERNS:
            if code=='render' and result.get('contact_status')=='found':continue
            if re.search(pattern,str(warning),re.I):
                failed=next((s for s in reversed(steps) if s.get('error')==warning),{})
                add(code,label,warning,url=failed.get('failure_url') or '',retryable=retryable);break
    return issues
