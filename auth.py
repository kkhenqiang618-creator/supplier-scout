"""Optional HTTP authentication guard; no default account or registration.

Integration: class Handler(AuthHandlerMixin, BusinessHandler); set server.auth to
AuthManager.from_env(required=True, session_store=postgres_sessions) in production,
or from_env(secure_cookie=False) for optional local authentication.

Session stores implement create(username, token_hash, expires_at), get(token_hash)
-> {'username': str, 'expires_at': Unix-seconds number} or None, and delete(token_hash).
Only SHA-256 token digests are persisted. Inject a shared limiter in cloud runtimes:
allow(key)->bool, failed(key), clear(key); the default limiter is process-local.
"""
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from collections import OrderedDict
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

PBKDF2_ITERATIONS = 600000
COOKIE_NAME = 'scout_session'
MAX_LOGIN_BYTES = 8192


def hash_password(password):
    """Return a salted PBKDF2-SHA256 verifier, compatible with Python 3.9+."""
    if not isinstance(password, str) or not password or len(password.encode('utf-8')) > 4096:
        raise ValueError('Password must contain 1–4096 UTF-8 bytes')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return 'pbkdf2_sha256${}${}${}'.format(PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def _parse_hash(value):
    try:
        algorithm, iterations, salt, digest = value.split('$')
        iterations = int(iterations)
        salt = base64.b64decode(salt, validate=True)
        digest = base64.b64decode(digest, validate=True)
        if algorithm != 'pbkdf2_sha256' or not 600000 <= iterations <= 2000000 or len(salt) < 16 or len(digest) != 32:
            raise ValueError()
        return iterations, salt, digest
    except (ValueError, TypeError, AttributeError):
        raise ValueError('SCOUT_AUTH_PASSWORD_HASH must be a hash_password() verifier') from None


class MemorySessionStore:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self, username, token_hash, expires_at):
        with self._lock:
            now = time.time()
            self._sessions = {key: value for key, value in self._sessions.items() if value['expires_at'] > now}
            self._sessions[token_hash] = {'username': username, 'expires_at': expires_at}

    def get(self, token_hash):
        with self._lock:
            value = self._sessions.get(token_hash)
            return dict(value) if value else None

    def delete(self, token_hash):
        with self._lock:
            self._sessions.pop(token_hash, None)


class MemoryLoginLimiter:
    """Five failures per client within fifteen minutes, bounded memory."""
    def __init__(self, clock=time.time):
        self.clock = clock
        self._attempts = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key):
        with self._lock:
            count, until = self._attempts.get(key, (0, 0))
            return until <= self.clock() or count < 5

    def failed(self, key):
        with self._lock:
            now = self.clock()
            count, until = self._attempts.get(key, (0, 0))
            self._attempts[key] = (count + 1, until) if until > now else (1, now + 900)
            self._attempts.move_to_end(key)
            while len(self._attempts) > 10000:
                self._attempts.popitem(last=False)

    def clear(self, key):
        with self._lock:
            self._attempts.pop(key, None)


class AuthManager:
    def __init__(self, username, password_hash, *, session_store=None, secure_cookie=True,
                 session_ttl=43200, allowed_origin=None, limiter=None, clock=time.time):
        if not isinstance(username, str) or not username.strip():
            raise ValueError('SCOUT_AUTH_USERNAME is required')
        self.username = username
        self._verifier = _parse_hash(password_hash)
        self._verifiers = {username:self._verifier}
        self.session_store = session_store if session_store is not None else MemorySessionStore()
        self.secure_cookie = secure_cookie
        self.session_ttl = int(session_ttl)
        if self.session_ttl < 1:
            raise ValueError('Session TTL must be positive')
        self.allowed_origin = allowed_origin
        if allowed_origin and (urlsplit(allowed_origin).scheme not in ('http', 'https') or urlsplit(allowed_origin).netloc == '' or urlsplit(allowed_origin).path not in ('', '/')):
            raise ValueError('allowed_origin must be an HTTP(S) origin')
        self.limiter = limiter if limiter is not None else MemoryLoginLimiter(clock)
        self.clock = clock

    @classmethod
    def from_env(cls, required=False, **kwargs):
        users_json = os.environ.get('SCOUT_AUTH_USERS_JSON')
        if users_json is not None:
            try:
                users = json.loads(users_json)
                if not isinstance(users,dict) or not users or any(not isinstance(name,str) or not name.strip() or len(name)>256 for name in users):raise ValueError()
                parsed = {name:_parse_hash(value) for name,value in users.items()}
            except (ValueError,TypeError):raise ValueError('SCOUT_AUTH_USERS_JSON must map usernames to password hashes') from None
            name = next(iter(users))
            if 'allowed_origin' not in kwargs and os.environ.get('SCOUT_AUTH_ORIGIN'):kwargs['allowed_origin']=os.environ['SCOUT_AUTH_ORIGIN']
            manager=cls(name,users[name],**kwargs);manager._verifiers=parsed
            return manager
        username = os.environ.get('SCOUT_AUTH_USERNAME', '')
        password_hash = os.environ.get('SCOUT_AUTH_PASSWORD_HASH', '')
        if not username and not password_hash and not required:
            return None
        if not username or not password_hash:
            raise ValueError('SCOUT_AUTH_USERNAME and SCOUT_AUTH_PASSWORD_HASH are required together')
        if 'allowed_origin' not in kwargs and os.environ.get('SCOUT_AUTH_ORIGIN'):
            kwargs['allowed_origin'] = os.environ['SCOUT_AUTH_ORIGIN']
        return cls(username, password_hash, **kwargs)

    def verify(self, username, password):
        iterations, salt, expected = self._verifiers.get(username,self._verifier)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
        return hmac.compare_digest(digest, expected) & (username in self._verifiers)

    def create_session(self, username=None):
        username = self.username if username is None else username
        if username not in self._verifiers:raise ValueError('未知账号')
        token = secrets.token_urlsafe(32)
        self.session_store.create(username, self.token_hash(token), self.clock() + self.session_ttl)
        return token

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) != 43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in token):
            return None
        digest = self.token_hash(token)
        session = self.session_store.get(digest)
        if not session:
            return None
        try:
            valid = session['username'] in self._verifiers and math.isfinite(float(session['expires_at'])) and float(session['expires_at']) > self.clock()
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            self.session_store.delete(digest)
            return None
        return session

    def cookie(self, token='', clear=False):
        cookie = SimpleCookie()
        cookie[COOKIE_NAME] = token
        cookie[COOKIE_NAME]['path'] = '/'
        cookie[COOKIE_NAME]['httponly'] = True
        cookie[COOKIE_NAME]['samesite'] = 'Lax'
        cookie[COOKIE_NAME]['max-age'] = 0 if clear else self.session_ttl
        if self.secure_cookie:
            cookie[COOKIE_NAME]['secure'] = True
        return cookie.output(header='').strip()


def safe_next(value):
    if not isinstance(value, str) or not value.startswith('/'):
        return '/'
    decoded = value
    for _ in range(4):
        decoded = unquote(decoded)
    if decoded.startswith('//') or '\\' in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return '/'
    if urlsplit(decoded).scheme or urlsplit(decoded).netloc:
        return '/'
    return value


class AuthHandlerMixin:
    """Place first in HTTP handler MRO; server.auth=None preserves local behavior."""
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        super().end_headers()

    def _auth_reply(self, status, data=None, headers=None, content_type='application/json; charset=utf-8'):
        body = b'' if data is None else data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _auth_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ''
        except CookieError:
            return ''

    def _auth_origin_ok(self, auth):
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        origin = self.headers.get('Origin')
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in ('https', 'http') or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            return False
        expected = auth.allowed_origin or ('https://' if auth.secure_cookie else 'http://') + self.headers.get('Host', '')
        return origin == expected.rstrip('/')

    def _auth_guard(self, auth):
        session = auth.authenticate(self._auth_token())
        if session:
            self.auth_session = session
            return True
        if urlsplit(self.path).path.startswith('/api/'):
            self._auth_reply(401, {'error':'请先登录'})
        else:
            self._auth_reply(303, headers={'Location':'/login?' + urlencode({'next':safe_next(self.path)})})
        return False

    def do_GET(self):
        auth = getattr(self.server, 'auth', None)
        if auth is None:
            return super().do_GET()
        path = urlsplit(self.path).path
        public = {'/login':'login.html', '/login.html':'login.html', '/login.js':'login.js', '/login.css':'login.css'}
        if path in public:
            filename = public[path]
            mime = {'html':'text/html', 'js':'application/javascript', 'css':'text/css'}[filename.rsplit('.',1)[1]]
            return self._auth_reply(200, (Path(__file__).parent/'static'/filename).read_bytes(), {'Content-Security-Policy':"default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}, mime + '; charset=utf-8')
        if path == '/favicon.ico':
            return self._auth_reply(204)
        if not self._auth_guard(auth):
            return
        if path == '/api/auth/session':
            return self._auth_reply(200, {'username':self.auth_session['username']})
        return super().do_GET()

    def do_POST(self):
        auth = getattr(self.server, 'auth', None)
        if auth is None:
            return super().do_POST()
        if not self._auth_origin_ok(auth):
            return self._auth_reply(403, {'error':'请求来源不受信任'})
        path = urlsplit(self.path).path
        if path == '/api/auth/login':
            if self.headers.get('Transfer-Encoding'):
                return self._auth_reply(400, {'error':'不支持的请求编码'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self._auth_reply(400, {'error':'无效的请求长度'})
            if length < 1:
                return self._auth_reply(400, {'error':'请输入用户名和密码'})
            if length > MAX_LOGIN_BYTES:
                return self._auth_reply(413, {'error':'请求内容过大'})
            if self.headers.get_content_type() != 'application/json':
                return self._auth_reply(415, {'error':'请使用 JSON 请求'})
            key = self.client_address[0]
            if not auth.limiter.allow(key):
                return self._auth_reply(429, {'error':'尝试次数过多，请在 15 分钟后重试'}, {'Retry-After':'900'})
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get('username'), str) or not isinstance(data.get('password'), str) or len(data['username']) > 256 or len(data['password'].encode('utf-8')) > 4096:
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self._auth_reply(400, {'error':'请输入有效的用户名和密码'})
            if not auth.verify(data['username'], data['password']):
                auth.limiter.failed(key)
                return self._auth_reply(401, {'error':'用户名或密码不正确'})
            auth.limiter.clear(key)
            old_token = self._auth_token()
            if auth.authenticate(old_token):
                auth.session_store.delete(auth.token_hash(old_token))
            token = auth.create_session(data['username'])
            return self._auth_reply(200, {'ok':True, 'next':safe_next(data.get('next','/'))}, {'Set-Cookie':auth.cookie(token)})
        if not self._auth_guard(auth):
            return
        if path == '/api/auth/logout':
            auth.session_store.delete(auth.token_hash(self._auth_token()))
            return self._auth_reply(200, {'ok':True}, {'Set-Cookie':auth.cookie(clear=True)})
        return super().do_POST()
