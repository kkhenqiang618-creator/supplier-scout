"""SSRF guard for the Chrome fallback: every Chrome request goes through this loopback-only HTTP proxy.
The proxy resolves each target once, refuses the whole request when any address is not public (engine.is_public_ip),
then connects to that first pinned IP only, so DNS rebinding between check and connect has nothing to hit."""
import collections
import select
import socket
import socketserver
import threading
import urllib.parse
from engine import is_public_ip

MAX_CONNECTIONS=48;MAX_HEADER=16*1024;MAX_BODY=8*1024*1024;IDLE=30;CONNECT_TIMEOUT=10;PORTS={80,443}
HOP={'connection','keep-alive','proxy-connection','proxy-authorization','proxy-authenticate','te','trailer','transfer-encoding','upgrade'}

class Denied(ValueError):pass

def resolve_public(host,port,*,resolver=socket.getaddrinfo,allow_loopback=False):
    """One resolution, all-or-nothing: returns the first address to pin the connection to."""
    host=str(host or '').strip('[]')
    if not host or not isinstance(port,int):raise Denied('目标地址无效')
    try:rows=resolver(host,port,type=socket.SOCK_STREAM)
    except (OSError,UnicodeError):raise Denied('域名无法解析')
    addresses=[row[4][0] for row in rows or []]
    if not addresses:raise Denied('域名无法解析')
    import ipaddress
    loopback=lambda a:ipaddress.ip_address(a.split('%',1)[0]).is_loopback
    # Test-only keyword: loopback (any port) for local fixtures; other private ranges stay refused.
    if allow_loopback is True and all(loopback(a) for a in addresses):return addresses[0]
    if port not in PORTS:raise Denied('只允许 80/443 端口')
    if any(not is_public_ip(a) for a in addresses):raise Denied('禁止访问内网、回环或保留地址')
    return addresses[0]

class GuardProxy:
    def __init__(self,*,allow_loopback_for_tests=False,resolver=socket.getaddrinfo):
        self.allow_loopback=allow_loopback_for_tests is True;self.resolver=resolver
        self.denied=collections.deque(maxlen=50);self.allowed=collections.deque(maxlen=200)
        self.slots=threading.BoundedSemaphore(MAX_CONNECTIONS);self.sockets=set();self.lock=threading.Lock()
        proxy=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):proxy._handle(self.request)
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads=True;allow_reuse_address=True
        self.server=Server(('127.0.0.1',0),Handler);self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.2},daemon=True);self.thread.start()
    def close(self):
        self.server.shutdown();self.server.server_close()
        with self.lock:open_sockets=list(self.sockets)
        for sock in open_sockets:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:sock.close()
            except OSError:pass
    def _track(self,sock,add=True):
        with self.lock:(self.sockets.add if add else self.sockets.discard)(sock)
    def _refuse(self,client,code,reason,target=''):
        self.denied.append((target,reason))
        body=reason.encode('utf-8')
        try:client.sendall(b'HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'%(code,{400:b'Bad Request',403:b'Forbidden',411:b'Length Required',413:b'Payload Too Large',431:b'Request Header Fields Too Large',502:b'Bad Gateway',503:b'Service Unavailable'}[code],len(body))+body)
        except OSError:pass
    def _handle(self,client):
        if not self.slots.acquire(blocking=False):self._refuse(client,503,'代理连接数已达上限');return
        upstream=None;self._track(client)
        try:
            client.settimeout(IDLE);data=b''
            while b'\r\n\r\n' not in data:
                if len(data)>MAX_HEADER:return self._refuse(client,431,'请求头过大')
                chunk=client.recv(4096)
                if not chunk:return
                data+=chunk
            head,_,rest=data.partition(b'\r\n\r\n')
            if len(head)>MAX_HEADER:return self._refuse(client,431,'请求头过大')
            lines=head.decode('latin-1').split('\r\n');parts=lines[0].split(' ')
            if len(parts)!=3 or not parts[2].startswith('HTTP/'):return self._refuse(client,400,'请求格式无效')
            method,target,version=parts;headers=[]
            for line in lines[1:]:
                name,sep,value=line.partition(':')
                if not sep or not name.strip() or name!=name.strip():return self._refuse(client,400,'请求头格式无效')
                headers.append((name,value.strip()))
            if method.upper()=='CONNECT':
                host,sep,port=target.rpartition(':')
                if not sep or not port.isdigit():return self._refuse(client,400,'CONNECT 目标无效',target)
                try:ip=resolve_public(host,int(port),resolver=self.resolver,allow_loopback=self.allow_loopback)
                except Denied as error:return self._refuse(client,403,str(error),target)
                try:upstream=socket.create_connection((ip,int(port)),timeout=CONNECT_TIMEOUT)
                except OSError:return self._refuse(client,502,'无法连接目标站点',target)
                self._track(upstream);self.allowed.append(('CONNECT',target,ip))
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                if rest:upstream.sendall(rest)
                return self._pipe(client,upstream)
            split=urllib.parse.urlsplit(target)
            if split.scheme.lower()!='http' or not split.hostname or split.username or split.password:return self._refuse(client,403,'只允许 http:// 绝对地址',target)
            try:port=split.port or 80
            except ValueError:return self._refuse(client,400,'端口无效',target)
            try:ip=resolve_public(split.hostname,port,resolver=self.resolver,allow_loopback=self.allow_loopback)
            except Denied as error:return self._refuse(client,403,str(error),target)
            names={n.lower() for n,_ in headers}
            if 'transfer-encoding' in names:return self._refuse(client,411,'不支持分块上传的请求体',target)
            length=next((v for n,v in headers if n.lower()=='content-length'),'0')
            if not length.isdigit():return self._refuse(client,400,'Content-Length 无效',target)
            if int(length)>MAX_BODY:return self._refuse(client,413,'请求体过大',target)
            body=rest
            while len(body)<int(length):
                chunk=client.recv(65536)
                if not chunk:return
                body+=chunk
            dropped=HOP|{v.strip().lower() for n,v in headers if n.lower()=='connection' for v in v.split(',')}
            path=(split.path or '/')+('?'+split.query if split.query else '')
            kept=[(n,v) for n,v in headers if n.lower() not in dropped]
            if 'host' not in names:kept.insert(0,('Host',split.netloc))
            request=(method+' '+path+' '+version+'\r\n'+''.join(n+': '+v+'\r\n' for n,v in kept)+'Connection: close\r\n\r\n').encode('latin-1')+body[:int(length)]
            try:upstream=socket.create_connection((ip,port),timeout=CONNECT_TIMEOUT)
            except OSError:return self._refuse(client,502,'无法连接目标站点',target)
            self._track(upstream);self.allowed.append((method,target,ip))
            upstream.sendall(request)
            # Connection: close upstream; relay the raw response one way until the server closes, so a reused client connection never reaches this host.
            return self._pipe(client,upstream,one_way=True)
        except (OSError,UnicodeError):return
        finally:
            for sock in [upstream,client]:
                if sock is None:continue
                self._track(sock,False)
                try:sock.close()
                except OSError:pass
            self.slots.release()
    def _pipe(self,a,b,one_way=False):
        peers={a:b,b:a};sources=[b] if one_way else [a,b]
        while True:
            ready,_,_=select.select(sources,[],[],IDLE)
            if not ready:return
            for sock in ready:
                data=sock.recv(65536)
                if not data:return
                peers[sock].sendall(data)
