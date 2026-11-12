"""Conservative, evidence-based extraction. Page content is data, never code/instructions."""
import ipaddress
import math
import re
import socket
import time
import os
import json
import subprocess
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

DEFAULT_RULES = [
    {'model':'deepseek-v3','label':'DeepSeek V3','min_ratio':0.3,'max_ratio':0.4,'review_margin':0.1,'currency':'CNY','unit':'1M tokens','reference_input':2.0,'note':'演示基准，请按实际版本及官方价格修改'},
    {'model':'deepseek-r1','label':'DeepSeek R1','min_ratio':0.3,'max_ratio':0.4,'review_margin':0.1,'currency':'CNY','unit':'1M tokens','reference_input':4.0,'note':'演示基准，请按实际版本及官方价格修改'},
    {'model':'gpt-4o','label':'GPT-4o','min_ratio':0.4,'max_ratio':0.55,'review_margin':0.1,'currency':'USD','unit':'1M tokens','reference_input':2.5,'note':'演示基准，请按实际版本及官方价格修改'},
]
ALIASES = {'deepseek-v3':r'deepseek[\s_-]*v3(?![\w.])','deepseek-r1':r'deepseek[\s_-]*r1(?![\w.])','gpt-4o':r'gpt[\s_-]*4o(?![\w.-])'}

def validate_rules(rules):
    if not isinstance(rules,list) or not rules or len(rules)>30: raise ValueError('规则须为 1–30 项 JSON 数组')
    names=set()
    for rule in rules:
        if not isinstance(rule,dict) or not str(rule.get('model','')).strip(): raise ValueError('每条规则必须有 model')
        if rule['model'] in names: raise ValueError('模型规则不能重复')
        names.add(rule['model'])
        for field in ['resource_thresholds','reference_by_resource']:
            mapping=rule.get(field,{})
            if not isinstance(mapping,dict): raise ValueError('资源形态折扣须为映射')
            for name,value in mapping.items():
                if not name or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=1: raise ValueError('资源形态折扣须在 0–1 之间')
        for key in ['min_ratio','max_ratio','review_margin','reference_input']:
            value=rule.get(key)
            if key in ['max_ratio','reference_input'] and value is None: continue
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0: raise ValueError('规则 '+key+' 必须是非负有限数')
        if rule.get('max_ratio') is not None and not 0 <= rule['min_ratio'] <= rule['max_ratio'] <= 1: raise ValueError('询价门槛须在 0–1 之间；没有门槛时填 null')
        if rule.get('reference_input') is not None and (rule['reference_input']<=0 or rule.get('currency') not in ['CNY','USD'] or rule.get('unit')!='1M tokens'): raise ValueError('价格基准须为正数，并提供 CNY/USD 和 1M tokens；不确定基准时填 null')
    return rules

def evaluate(offer,rules):
    rule=next((r for r in rules if r['model']==offer.get('model')),None)
    result=dict(offer,status='review',reason='缺少可比较的报价或模型规则')
    if not rule: return result
    ratio=offer.get('ratio')
    if ratio is None and offer.get('input_price') is not None:
        if offer.get('currency')!=rule['currency'] or offer.get('unit')!=rule['unit']:
            return dict(result,reason='币种或计价单位与基准不一致，待人工核对')
        if not rule.get('reference_input'): return dict(result,reason='已记录实际价格，尚无可比较的官方价格基准')
        ratio=offer['input_price']/rule['reference_input']
    if isinstance(ratio,bool) or not isinstance(ratio,(int,float)) or not math.isfinite(ratio) or ratio<=0: return result
    result['ratio']=round(ratio,6)
    form=offer.get('resource_form') or offer.get('resource_type')
    form={'self_deployed':'自部署','official_transfer':'官转','official_key':'官key'}.get(form,form)
    if rule.get('resource_forms') and form not in rule['resource_forms']: return dict(result,reason='本模型限定资源形态，网页尚未确认相应形态')
    lo,hi=rule['min_ratio'],rule['max_ratio']
    if rule.get('resource_thresholds'):
        form=offer.get('resource_form') or offer.get('resource_type')
        form={'self_deployed':'自部署','official_transfer':'官转','official_key':'官key'}.get(form,form)
        if form not in rule['resource_thresholds']: return dict(result,reason='资源形态未确认；自部署≤4折、官转≤5.5折，不按型号猜测')
        hi=rule['resource_thresholds'][form]
        result['target_ratio']=hi
    if hi is None: return dict(result,reason='A 部分未设置本模型的询价门槛，仅记录报价')
    if lo-1e-9<=ratio<=hi+1e-9: result.update(status='qualified',reason='折扣落在目标区间；仍需核实服务和报价条件')
    elif ratio<lo: result.update(reason='低于目标区间下限，需核实异常低价与适用条件')
    elif ratio<=hi+rule.get('review_margin',.1)+1e-9: result.update(reason='接近目标上限，进入人工复核')
    else: result.update(status='skipped',reason='折扣高于目标与复核区间')
    return result

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.links=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ['script','style','noscript','svg']: self.hidden+=1
        if tag in ['p','div','tr','li','br','h1','h2','h3','section']: self.parts.append('\n')
        attrs=dict(attrs)
        if tag=='a' and attrs.get('href') and not self.hidden:
            self.links.append(attrs['href'])
            if attrs['href'].startswith(('mailto:','tel:','https://t.me/','https://discord.gg/')): self.parts.append(' '+attrs['href']+' ')
    def handle_endtag(self,tag):
        if tag in ['script','style','noscript','svg']: self.hidden=max(0,self.hidden-1)
        if tag in ['p','div','tr','li','section']: self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data+' ')

def extract_page(html,source):
    parser=PageParser(); parser.feed(html)
    text='\n'.join(re.sub(r'\s+',' ',line).strip() for line in ''.join(parser.parts).splitlines() if line.strip())
    def matches(pattern): return sorted(set(re.findall(pattern,text,re.I)))[:30]
    contacts={
        'email':matches(r'(?<![\w.+-])([\w.+-]+@[\w.-]+\.[a-z]{2,24})'),
        'wechat':matches(r'(?:微信|wechat|weixin)\s*[:：号]?\s*([a-zA-Z][a-zA-Z0-9_-]{5,25})'),
        'qq':matches(r'\bQQ\s*[:：号]?\s*([1-9][0-9]{4,11})'),
        'phone':matches(r'(?<!\d)(1[3-9]\d{9})(?!\d)'),
        'telegram':matches(r'(https?://t\.me/[a-zA-Z0-9_+/-]+)'),
        'discord':matches(r'(https?://(?:discord\.gg|discord\.com/invite)/[a-zA-Z0-9-]+)'),
    }
    offers=[]
    for line in text.splitlines():
        found=[model for model,pattern in ALIASES.items() if re.search(pattern,line,re.I)]
        if len(found)!=1: continue
        discounts=re.findall(r'(?<![\d.])(\d(?:\.\d+)?)\s*折',line)
        if len(discounts)==1 and not re.search(r'起步|起售|低至|最低|最高|折\s*起|起[，,。\s]|限时|首充|返利',line):
            offers.append({'model':found[0],'ratio':float(discounts[0])/10,'basis':'published_discount','evidence':line[:600],'source':source})
    return {'text':text[:80000],'contacts':contacts,'offers':offers,'links':parser.links,'source':source}

NAT64=[ipaddress.ip_network('64:ff9b::/96'),ipaddress.ip_network('64:ff9b:1::/48')]
# IPv4-compatible (::a.b.c.d, deprecated) and SIIT-translated (::ffff:0:a.b.c.d) forms also embed an IPv4 address that is_global ignores.
EMBEDDED=[ipaddress.ip_network('::/96'),ipaddress.ip_network('::ffff:0:0:0/96')]
def is_public_ip(address):
    """is_global misses addresses that tunnel to a private IPv4 (6to4 2002:a00:1::, NAT64 64:ff9b::a00:1); unwrap or refuse those first."""
    try: ip=ipaddress.ip_address(str(address).split('%',1)[0])
    except ValueError: return False
    if ip.version==6:
        if ip.ipv4_mapped: ip=ip.ipv4_mapped
        elif ip.sixtofour: ip=ip.sixtofour
        elif any(ip in net for net in NAT64+EMBEDDED): return False
    # is_global still admits multicast (224/4, ff00::/8) and deprecated site-local fec0::/10.
    return ip.is_global and not ip.is_multicast and not getattr(ip,'is_site_local',False)

def validate_url(url):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme not in ['http','https'] or not parsed.hostname or parsed.username or parsed.password: raise ValueError('只允许无账号密码的公开 HTTP/HTTPS 地址')
    if parsed.port and parsed.port not in [80,443]: raise ValueError('只允许 80/443 端口')
    if parsed.hostname.lower()=='localhost' or parsed.hostname.endswith(('.local','.internal','.example')): raise ValueError('该域名不是公开网站')
    from browser_egress import configured_upstream
    upstream=configured_upstream()
    try: addresses=(upstream.resolve if upstream else socket.getaddrinfo)(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError: raise ValueError('域名无法解析（请检查采集网络出口）') from None
    if not addresses or any(not is_public_ip(row[4][0]) for row in addresses): raise ValueError('禁止访问内网、回环或保留地址')
    return url

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url):
    validate_url(url)
    req=urllib.request.Request(url,headers={'User-Agent':'SupplierScout/0.1 (public-page research)','Accept':'text/html,text/plain'})
    from browser_egress import collection_opener
    with collection_opener(SafeRedirect) as opener:
        with opener.open(req,timeout=12) as response:
            if response.headers.get_content_type() not in ['text/html','text/plain','application/xhtml+xml']: raise ValueError('页面不是可解析的 HTML/文本')
            raw=response.read(1500001)
            if len(raw)>1500000: raise ValueError('页面超过 1.5MB 限制')
            return raw.decode(response.headers.get_content_charset() or 'utf-8',errors='replace'),response.url

def render_page(url):
    validate_url(url)
    root=Path(__file__).resolve().parent
    module=os.environ.get('SCOUT_PLAYWRIGHT_MODULE')
    if not module:
        candidates=[root/'node_modules'/'playwright']+list((Path.home()/'.npm'/'_npx').glob('*/node_modules/playwright'))
        module=next((str(p) for p in candidates if (p/'package.json').exists()),None)
    if not module: raise ValueError('浏览器采集依赖未安装，当前只能读取普通网页')
    env=dict(os.environ,SCOUT_PLAYWRIGHT_MODULE=module)
    result=subprocess.run(['node',str(root/'browser_fetch.cjs'),url],capture_output=True,text=True,encoding='utf-8',timeout=28,env=env)
    if result.returncode: raise ValueError('浏览器加载失败：'+result.stderr[:300])
    data=json.loads(result.stdout); validate_url(data['url'])
    return data['html'],data['url']

def crawl(url,log=lambda message:None):
    log('检查网页访问规则 robots.txt')
    validate_url(url)
    base=urllib.parse.urlsplit(url); origin=base.scheme+'://'+base.netloc
    robots=urllib.robotparser.RobotFileParser(); robots.set_url(origin+'/robots.txt')
    try:
        body,_=fetch(origin+'/robots.txt'); robots.parse(body.splitlines())
    except urllib.error.HTTPError as e:
        if e.code in [401,403,429] or e.code>=500: raise ValueError('robots.txt 无法确认允许访问，稍后重试')
        robots.parse([])
    except Exception as e: raise ValueError('无法核实 robots.txt：'+str(e))
    queue=[url]; visited=set(); pages=[]; warnings=[]
    while queue and len(visited)<3:
        current=queue.pop(0)
        if current in visited: continue
        visited.add(current)
        if not robots.can_fetch('SupplierScout',current): warnings.append('robots.txt 不允许：'+current); continue
        log('访问 '+current)
        try:
            body,final=fetch(current); page=extract_page(body,final); method='普通网页'
            if len(page['text'])<180 and '<script' in body.lower():
                log('普通网页内容不足，正在使用浏览器加载 '+final)
                try:
                    rendered,rendered_url=render_page(final)
                    page=extract_page(rendered,rendered_url); final=rendered_url; method='浏览器加载'
                except Exception as error: warnings.append(str(error))
            page['method']=method; pages.append(page)
            log('读取 '+str(len(page['text']))+' 字，发现 '+str(sum(len(v) for v in page['contacts'].values()))+' 条联系方式、'+str(len(page['offers']))+' 条明确折扣')
            for href in page['links']:
                link=urllib.parse.urljoin(final,href).split('#')[0]; parsed=urllib.parse.urlsplit(link)
                if parsed.netloc in [base.netloc,urllib.parse.urlsplit(final).netloc] and parsed.scheme in ['http','https'] and re.search(r'contact|about|pricing|price|models|docs|联系我们|价格',link,re.I) and link not in visited and link not in queue: queue.append(link)
        except Exception as e: warnings.append(current+'：'+str(e))
        if queue and len(visited)<3: time.sleep(.5)
    if not pages: raise ValueError('未获取到页面；'+'；'.join(warnings))
    contacts={k:sorted(set(v for page in pages for v in page['contacts'][k])) for k in pages[0]['contacts']}
    offers=[offer for page in pages for offer in page['offers']]
    return {'contacts':contacts,'offers':offers,'sources':[{'url':p['source'],'excerpt':p['text'][:10000],'method':p.get('method','普通网页'),'text_length':len(p['text'])} for p in pages], 'warnings':warnings}
