"""DeepSeek Search key and keyword table for the local discovery page (P25).

The key is saved the same way as the model key: Windows keeps a DPAPI blob that
only the current Windows user can decrypt, other systems keep a 0600 file. The
key is never returned to the page, exported or written to the log.
"""

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

from model_service import KEY_PREFIX, protect_key, unprotect_key

# The five default searches from P25; every one of them asks for Chinese pages.
DEFAULT_QUERIES = (
    'kimi-k3.1-pro API 中转 lang:zh',
    'kimi-k3.1-pro 倍率 lang:zh',
    'K3.1 Pro API 低价 充值 lang:zh',
    'Kimi K3.1 Pro 私有化部署 B300 lang:zh',
    'new-api kimi-k3.1-pro lang:zh',
)

# Edited on the discovery page; the search round drops these hosts before probing.
DEFAULT_EXCLUDE = (
    'zhihu.com',
    'csdn.net',
    'github.com',
    'gitee.com',
    'bilibili.com',
    'mp.weixin.qq.com',
    'kimi.ai',
    'moonshot.cn',
    'moonshot.ai',
    'youtube.com',
    'x.com',
    'twitter.com',
    'facebook.com',
    'wikipedia.org',
    'baidu.com',
    'juejin.cn',
    'cnblogs.com',
    'segmentfault.com',
    'douyin.com',
    'xiaohongshu.com',
    'jianshu.com',
    'sohu.com',
    'sina.com.cn',
    'qq.com',
    'aliyun.com',
    'tencent.com',
    'volcengine.com',
    'modelscope.cn',
    'huggingface.co',
)


def _lines(value):
    """Keyword table from a textarea (one per line) or a JSON list."""
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple)) else str(value).splitlines()
    result = []
    for item in items:
        text = str(item or '').strip()
        if text and text not in result:
            result.append(text)
    return result


def _entries(value):
    """Exclude list from a comma/newline separated textarea or a JSON list."""
    if value is None:
        return None
    items = (
        [line for item in value for line in str(item or '').replace('，', ',').split(',')]
        if isinstance(value, (list, tuple))
        else str(value).replace('，', ',').split(',')
    )
    result = []
    for item in items:
        text = str(item or '').strip().lstrip('*').lstrip('.').lower()
        if text and text not in result:
            result.append(text)
    return result


class SearchService:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        raw = str(data.get('api_key') or '')
        key, error = unprotect_key(raw)
        data['api_key'] = key
        data['key_protected'] = raw.startswith(KEY_PREFIX)
        if error:
            data['key_error'] = error
        data['queries'] = _lines(data.get('queries'))
        if data['queries'] is None:
            data['queries'] = list(DEFAULT_QUERIES)
        data['exclude'] = _entries(data.get('exclude'))
        if data['exclude'] is None:
            data['exclude'] = list(DEFAULT_EXCLUDE)
        return data

    def public(self):
        data = self.read()
        key_error = data.get('key_error', '')
        return {
            'key_configured': bool(data.get('api_key')),
            'key_protected': bool(data.get('key_protected')),
            'key_error': key_error,
            'configured': bool(data.get('api_key')) and not key_error,
            'queries': data['queries'],
            'exclude': data['exclude'],
        }

    def save(self, data):
        with self.lock:
            old = self.read()
            raw_key = str(data.get('api_key', '')).strip()
            if old.get('key_error') and not raw_key:
                raise ValueError(old['key_error'] + '；请重新填写 DeepSeek 搜索 Key 后再保存')
            queries = _lines(data['queries']) if 'queries' in data else old['queries']
            exclude = _entries(data['exclude']) if 'exclude' in data else old['exclude']
            self._write(
                {
                    'api_key': raw_key or old.get('api_key', ''),
                    'queries': queries,
                    'exclude': exclude,
                    'revision': uuid.uuid4().hex,
                }
            )
        return self.public()

    def _write(self, data):
        data = {key: value for key, value in data.items() if key not in ('key_error', 'key_protected')}
        if data.get('api_key'):
            try:
                data['api_key'] = protect_key(str(data['api_key']))
            except OSError as error:
                raise ValueError('DeepSeek 搜索 Key 无法通过 Windows 凭据加密保存：' + str(error))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=str(self.path.parent), delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
            if os.name != 'nt':
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
