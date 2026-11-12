"""Explicit browser upstream, remote public DNS and IP-pinned HTTP CONNECT.

Only operator configuration selects the proxy. Never let the upstream resolve
page-controlled names: checked IPs are the tunnel targets, TLS retains SNI.
"""
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from functools import lru_cache
from engine import is_public_ip

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise OSError('采集代理 DNS 服务意外跳转')

class FixedProxy(urllib.request.ProxyHandler):
    """Explicit operator route cannot be bypassed by NO_PROXY from the shell."""
    def proxy_open(self,req,proxy,type):
        req.set_proxy(urllib.parse.urlsplit(proxy).netloc,'http')

class Upstream:
    def __init__(self,url):
        try:
            p=urllib.parse.urlsplit(url)
            valid=p.scheme=='http' and p.hostname and p.port and not (p.username or p.password or p.query or p.fragment or p.path not in ['', '/'])
            if not valid:raise ValueError()
        except (ValueError,TypeError):raise ValueError('采集代理配置无效：使用无账号密码的 http://主机:端口') from None
        self.url=url;self.endpoint=(p.hostname,p.port);self.cache={};self.lock=threading.Lock()
    def _query(self,host,kind):
        url='https://cloudflare-dns.com/dns-query?'+urllib.parse.urlencode({'name':host,'type':kind})
        opener=urllib.request.build_opener(FixedProxy({'https':self.url}),NoRedirect())
        req=urllib.request.Request(url,headers={'Accept':'application/dns-json'})
        with opener.open(req,timeout=5) as response:
            raw=response.read(65537)
        if len(raw)>65536:raise OSError('采集代理 DNS 响应过大')
        data=json.loads(raw)
        if data.get('Status')!=0:raise OSError('采集代理 DNS 查询失败')
        return [a.get('data','') for a in data.get('Answer',[]) if a.get('type')==kind]
    def resolve(self,host,port,**kwargs):
        host=host.strip('[]').lower().rstrip('.')
        try:ips=[str(ipaddress.ip_address(host))]
        except ValueError:
            if host=='localhost' or host.endswith(('.local','.internal')):raise OSError('禁止访问内网域名')
            with self.lock:cached=self.cache.get(host)
            if cached and cached[0]>time.monotonic():ips=cached[1]
            else:
                try:ips=self._query(host,1) or self._query(host,28)
                except Exception:raise OSError('采集代理 DNS 查询失败') from None
                if not ips or any(not is_public_ip(ip) for ip in ips):raise OSError('禁止访问内网、回环或保留地址')
                with self.lock:
                    if len(self.cache)>512:self.cache.clear()
                    self.cache[host]=(time.monotonic()+60,ips)
        if not ips or any(not is_public_ip(ip) for ip in ips):raise OSError('禁止访问内网、回环或保留地址')
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,port)) for ip in ips]
    def connect(self,ip,port):
        if not is_public_ip(ip) or port not in [80,443]:raise OSError('禁止访问目标地址')
        sock=None
        try:
            sock=socket.create_connection(self.endpoint,timeout=10)
            target=('['+ip+']' if ':' in ip else ip)+':'+str(port)
            sock.sendall(('CONNECT '+target+' HTTP/1.1\r\nHost: '+target+'\r\n\r\n').encode('ascii'))
            head=b'';deadline=time.monotonic()+10
            while not head.endswith(b'\r\n\r\n'):
                if len(head)>=16384 or time.monotonic()>=deadline:raise OSError()
                sock.settimeout(max(.01,deadline-time.monotonic()));chunk=sock.recv(1)
                if not chunk:raise OSError()
                head+=chunk
            if head.split(b'\r\n',1)[0].split()[1]!=b'200':raise OSError()
            sock.settimeout(30);return sock
        except Exception:
            if sock:sock.close()
            raise OSError('采集代理连接失败，请检查配置的网络出口') from None

@lru_cache(maxsize=8)
def _upstream(value):return Upstream(value)

def configured_upstream():
    value=os.environ.get('SCOUT_BROWSER_PROXY_URL','').strip()
    return _upstream(value) if value else None

@contextmanager
def collection_opener(*direct_handlers):
    """Auxiliary fetches share the browser route; direct mode preserves handlers."""
    upstream=configured_upstream()
    if upstream is None:
        yield urllib.request.build_opener(*direct_handlers);return
    from egress_proxy import GuardProxy
    from engine import SafeRedirect
    guard=GuardProxy(upstream=upstream)
    try:
        endpoint='http://127.0.0.1:'+str(guard.port)
        yield urllib.request.build_opener(FixedProxy({'http':endpoint,'https':endpoint}),SafeRedirect())
    finally:guard.close()
