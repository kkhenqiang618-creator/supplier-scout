"""Explicit, observed website migration evidence. Never guess a replacement hostname."""
import ipaddress
import re
from urllib.parse import urljoin,urlsplit,urlunsplit
from engine import validate_url

MIGRATION=re.compile(r'域名(?:升级|迁移|变更|更换|更新)|官网(?:迁移|搬迁)|new\s+domain|(?:site|website)\s+(?:has\s+)?moved',re.I)
ENTRY_LABEL=re.compile(r'新域名|新(?:的)?(?:官网|地址|入口)|前往新|new\s+(?:domain|site|address)|visit\s+new',re.I)

def canonical_url(value):
    p=urlsplit(str(value));return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or '/',p.query,''))

def safe_candidate(value,source):
    try:
        p=urlsplit(value);origin=urlsplit(source)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:return False
        if origin.scheme=='https' and p.scheme!='https':return False
        if p.port not in (None,80,443):return False
        host=p.hostname.lower()
        if host=='localhost' or host.endswith(('.localhost','.local','.internal')):return False
        try:
            if not ipaddress.ip_address(host).is_global:return False
        except ValueError:pass
        return True
    except ValueError:return False

def migration_candidates(snapshot):
    source=snapshot.get('url','');text='\n'.join([snapshot.get('text',''),*snapshot.get('dialogs',[])])
    if not MIGRATION.search(text):return []
    found={}
    def add(target,label,context):
        url=urljoin(source,str(target or ''))
        if not safe_candidate(url,source) or canonical_url(url)==canonical_url(source):return
        evidence=text[:1800]+'\n'+str(label or '')+'\n'+str(context or '')
        found[canonical_url(url)]={'url':url,'source_url':source,'evidence':evidence[:2200],'method':'official_notice'}
    for control in snapshot.get('controls',[]):
        href=control.get('href');label=' '.join(str(control.get(k) or '') for k in ['label','aria','title','name']);context=str(control.get('context') or '')
        # A normal docs/login link in a notice's parent is not a replacement website.
        if href and (ENTRY_LABEL.search(label) or re.search(r'https?://|\.[a-z]{2,}',label,re.I) and ENTRY_LABEL.search(context)):
            add(href,label,context)
    for hint in snapshot.get('redirect_hints',[]):
        if hint.get('kind')=='meta_refresh':add(hint.get('url'),'meta refresh',text)
    return list(found.values())

def verified_destination(snapshot,target):
    try:
        final=urlsplit(snapshot.get('url',''));wanted=urlsplit(target)
        error_page=re.compile(r'^(?:error\b|404\b|50[0234]\b|service unavailable|access denied|just a moment|抱歉|网站服务暂时不可用|正在维护|网站维护)',re.I)
        soft_error=any(error_page.search(str(snapshot.get(key,'')).strip()[:400]) for key in ['title','text'])
        return (not soft_error and safe_candidate(snapshot.get('url',''),target) and final.hostname==wanted.hostname and
                not snapshot.get('challenge') and not snapshot.get('page_unresponsive') and not snapshot.get('loading_pending') and
                not snapshot.get('goto_timeout') and 0<int(snapshot.get('http_status') or 200)<400 and
                len(str(snapshot.get('text','')).strip())>=40 and not migration_candidates(snapshot))
    except (ValueError,TypeError):return False

def validate_entry_url(value):
    if not isinstance(value,str) or len(value)>2048 or not safe_candidate(value,value):raise ValueError('新网址须为公开的 HTTP(S) 页面，不接受内网、凭据或特殊端口')
    validate_url(value)
    return canonical_url(value)
