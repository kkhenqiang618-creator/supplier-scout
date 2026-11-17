"""Read-only discovery of resellers that offer Kimi K3.1 Pro (P25).

Nothing here logs in, registers or posts: the discovery page probes the hosts it
already knows, asks the DeepSeek Search API for pages that resell K3.1 Pro, and
follows the outbound links of the saved source pages one level deep. Every host
is then read once through its public ``/api/status`` and ``/api/pricing``.

The module is deliberately free of store access so the network layer can be
replaced in tests; ``fetch_text`` and ``search_request`` are the only egress
points.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

from engine import SafeRedirect, extract_page, validate_url

UA = 'SupplierScout/0.1 (public-page research)'
TIMEOUT = 10
MAX_HOSTS = 200
MAX_BYTES = 400_000
MAX_PARALLEL = 4

# new-api / one-api expose these two read-only endpoints; no account is involved.
API_STATUS = '/api/status'
API_PRICING = '/api/pricing'

# Model families we look for: "kimi-k3.1-pro" and its variants, optionally with a
# seller prefix such as "[某某]kimi-k3.1-pro". "pro" and "pro-highspeed" stay
# separate entries because they are priced differently.
K31_ALIAS = re.compile(r'^kimi[-_ ]?k3\.1[-_ ]?pro(?![a-z])', re.I)
K31_PREFIX = re.compile(r'^\[[^\]]{1,40}\]\s*')

# Homepage wording that hints at self-hosted / bare-metal capacity. It is only a
# label for the human to ask about, never a filter.
SELFHOST_WORDS = ('B300', 'B200', 'H200', '自部署', '私有化', '自有算力', '智算中心', '算力集群')
SELFHOST_LABEL = '疑似自部署，需询问'

PRICE_BASIS = '站内标价（按 new-api 倍率换算，未含充值比例和分组倍率）'
STATUS_FIELDS = ('system_name', 'price', 'quota_per_unit')

# DeepSeek Search (P25 c): one billable call per keyword, at most 20 per round.
SEARCH_ENDPOINT = 'https://api.deepseek.com/v1/tools/search'
SEARCH_COUNT = 50
SEARCH_MAX_CALLS = 20
SEARCH_TIMEOUT = 60
SEARCH_QUERY_LIMIT = 128
SEARCH_LABEL = '搜索：'

# One level of snowballing from saved source pages named in P25 (b).
SNOWBALL_MAX_PAGES = 12

# Absolute URLs written in page text or returned by the search API.
TEXT_URL = re.compile(r'https?://[^\s<>"\'`\]\[(){}，。；：！？、]+', re.I)


def fetch_text(url):
    """One public GET (10s) returning decoded text; JSON is decoded by the caller."""
    validate_url(url)
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': UA,
            'Accept': 'text/html,application/json,text/plain',
        },
    )
    from browser_egress import collection_opener

    with collection_opener(SafeRedirect) as opener:
        with opener.open(request, timeout=TIMEOUT) as response:
            ctype = response.headers.get_content_type()
            if ctype not in (
                'text/html',
                'text/plain',
                'application/xhtml+xml',
                'application/json',
            ):
                raise ValueError('来源页不是网页或 JSON')
            charset = response.headers.get_content_charset() or 'utf-8'
            raw = response.read(MAX_BYTES + 1)
            final = response.url
    if len(raw) > MAX_BYTES:
        raise ValueError('页面超过 400KB 限制')
    try:
        return raw.decode(charset, errors='replace'), final
    except LookupError:
        return raw.decode('utf-8', errors='replace'), final


def origin_of(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme + '://' + parts.netloc


def normalize_source(value):
    """Accept a full URL or a bare domain and return a validated http(s) URL."""
    text = str(value or '').strip()
    if not text:
        raise ValueError('请填写来源网址或域名')
    if '://' not in text:
        text = 'https://' + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('来源网址无效')
    url = parts.scheme + '://' + parts.netloc + (parts.path or '/')
    if parts.query:
        url += '?' + parts.query
    validate_url(url)
    return url


def canonical_host(url):
    """Host used for de-duplication: lowercase, no leading ``www.``."""
    host = (urllib.parse.urlsplit(url).hostname or '').lower().rstrip('.')
    return host[4:] if host.startswith('www.') else host


def hosts_from_page(html, base_url):
    """Outbound link hosts of one page, in page order, de-duplicated and capped."""
    source_host = canonical_host(base_url)
    hosts, seen = [], set()
    for href in re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html, re.I):
        href = href.strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        target = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(target)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            continue
        host = canonical_host(target)
        if not host or host == source_host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
        if len(hosts) >= MAX_HOSTS:
            break
    return hosts


def selfhost_hits(text):
    """Return (label, sentence) for the first self-hosting hint, else ('', '')."""
    body = str(text or '')
    upper = body.upper()
    for word in SELFHOST_WORDS:
        index = upper.find(word.upper())
        if index < 0:
            continue
        start = max(body.rfind(ch, 0, index) for ch in '。！？!?\n') + 1
        ends = [body.find(ch, index) for ch in '。！？!?\n']
        ends = [end for end in ends if end >= 0]
        end = min(ends) + 1 if ends else len(body)
        return SELFHOST_LABEL, re.sub(r'\s+', ' ', body[start:end]).strip()[:300]
    return '', ''


def matches_k31(model_name):
    name = K31_PREFIX.sub('', str(model_name or '').strip())
    return bool(K31_ALIAS.match(name))


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float('inf'), float('-inf')) else None


def unit_prices(entry):
    """Input = model_ratio × 2 USD / 1M tokens; output = input × completion_ratio."""
    ratio = _number(entry.get('model_ratio'))
    if ratio is None:
        return None
    unit_input = round(ratio * 2, 6)
    completion = _number(entry.get('completion_ratio'))
    return {
        'input_price': unit_input,
        'output_price': round(unit_input * completion, 6) if completion is not None else None,
        'currency': 'USD',
        'unit': '百万 token',
        'basis': PRICE_BASIS,
    }


def k31_entries(pricing):
    """Keep only K3.1 Pro model rows, verbatim fields plus the derived unit prices."""
    entries = []
    for entry in pricing if isinstance(pricing, list) else []:
        if not isinstance(entry, dict) or not matches_k31(entry.get('model_name')):
            continue
        saved = {
            key: entry.get(key)
            for key in (
                'model_name',
                'model_ratio',
                'completion_ratio',
                'cache_ratio',
                'model_price',
                'quota_type',
                'enable_groups',
            )
            if key in entry
        }
        saved['unit_price'] = unit_prices(entry)
        saved['raw'] = entry
        entries.append(saved)
    return entries


def status_details(payload):
    """Small readable summary of a new-api /api/status answer (raw kept separately)."""
    from browser_agent import api_text, unwrap_api

    inner = unwrap_api(payload)
    data = inner if isinstance(inner, dict) else {}
    details = {
        'system_name': data.get('system_name') or '',
        'price': data.get('price'),
        'quota_per_unit': data.get('quota_per_unit'),
        'announcement': api_text(data),
    }
    return details


def robots_for(origin):
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(origin + '/robots.txt')
    try:
        body, _ = fetch_text(origin + '/robots.txt')
        parser.parse(body.splitlines())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429) or error.code >= 500:
            raise ValueError('robots.txt 无法确认允许访问，稍后重试') from None
        parser.parse([])
    except ValueError:
        raise ValueError('robots.txt 无法确认允许访问，稍后重试') from None
    return parser


def _get_json(url):
    body, final = fetch_text(url)
    payload = json.loads(body)
    return payload, final


def discover(source):
    """Fetch the pasted page once and return its outbound hosts."""
    url = normalize_source(source)
    robots = robots_for(origin_of(url))
    if not robots.can_fetch(UA.split('/')[0], url):
        raise ValueError('robots.txt 不允许访问该来源页')
    body, final = fetch_text(url)
    return {'source_url': final, 'hosts': hosts_from_page(body, final)}


def hosts_from_text(text, base_url=''):
    """Hosts of absolute URLs written inside text (search results or page excerpts)."""
    hosts, seen = [], set()
    for match in TEXT_URL.findall(str(text or '')):
        target = urllib.parse.urljoin(base_url, match) if base_url else match
        parts = urllib.parse.urlsplit(target)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            continue
        host = canonical_host(target)
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
        if len(hosts) >= MAX_HOSTS:
            break
    return hosts


def is_excluded(host, exclude):
    """True when the host is one of the common non-supplier sites (or a subdomain)."""
    name = str(host or '').lower()
    for item in exclude or ():
        domain = str(item or '').strip().lower().lstrip('*').lstrip('.')
        if domain and (name == domain or name.endswith('.' + domain)):
            return True
    return False


def _http_detail(error):
    try:
        raw = error.read(8192).decode('utf-8', errors='replace')
        detail = json.loads(raw).get('error', {})
        return str(detail.get('message', detail) if isinstance(detail, dict) else detail)[:200]
    except Exception:
        return str(getattr(error, 'reason', '') or '')


def search_request(query, api_key, count=SEARCH_COUNT):
    """One DeepSeek Search call; the only search egress point (replaced in tests)."""
    text = str(query or '').strip()
    if not text:
        raise ValueError('关键词不能为空')
    if len(text) > SEARCH_QUERY_LIMIT:
        raise ValueError('关键词不能超过 128 个字符：' + text[:20] + '…')
    body = json.dumps({'query': text, 'count': int(count), 'need_content': True})
    request = urllib.request.Request(
        SEARCH_ENDPOINT,
        data=body.encode('utf-8'),
        headers={
            'Authorization': 'Bearer ' + api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    from browser_egress import collection_opener

    try:
        with collection_opener(SafeRedirect) as opener:
            with opener.open(request, timeout=SEARCH_TIMEOUT) as response:
                return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            # The service echoes a masked key; do not show any of it.
            raise ValueError('DeepSeek 搜索失败（HTTP 401）：请到「能力接入」核对 DeepSeek 搜索 Key') from None
        raise ValueError(
            'DeepSeek 搜索失败（HTTP %d）：%s' % (error.code, _http_detail(error))
        ) from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, 'reason', error)
        raise ValueError('DeepSeek 搜索失败：' + (str(reason)[:150] or '网络错误')) from None


def search_round(queries, api_key, count=SEARCH_COUNT, max_calls=SEARCH_MAX_CALLS, exclude=()):
    """Run the keyword table (at most 20 searches) and collect hosts with their source."""
    found, seen, errors = [], set(), []
    calls = 0
    for query in queries or []:
        if calls >= max_calls:
            break
        text = str(query or '').strip()
        if not text:
            continue
        calls += 1
        try:
            payload = search_request(text, api_key, count)
        except ValueError as error:
            errors.append(str(error))
            continue
        results = payload.get('results') if isinstance(payload, dict) else None
        label = SEARCH_LABEL + text[:60]
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, dict):
                continue
            page = str(item.get('url') or '').strip()
            hosts = hosts_from_text(page)
            hosts.extend(hosts_from_text(item.get('content') or '', page))
            for host in hosts:
                if host in seen or is_excluded(host, exclude):
                    continue
                if len(found) >= MAX_HOSTS:
                    break
                seen.add(host)
                found.append({'host': host, 'found_by': label, 'source_url': page})
    return {'calls': calls, 'hosts': found, 'errors': errors}


def snowball_hosts(pages, exclude=(), max_pages=SNOWBALL_MAX_PAGES):
    """Fetch up to 12 saved source pages once and collect their outbound hosts."""
    unique = []
    for page in pages or []:
        page = str(page or '').strip()
        if page and page not in unique:
            unique.append(page)
        if len(unique) >= max_pages:
            break

    def one(page):
        try:
            return discover(page)
        except Exception as error:
            return {'error': str(error) or error.__class__.__name__}

    hosts, seen, errors, done = [], set(), [], 0
    if not unique:
        return {'pages': 0, 'hosts': hosts, 'errors': errors}
    workers = max(1, min(MAX_PARALLEL, len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for page, result in zip(unique, executor.map(one, unique)):
            if 'error' in result:
                errors.append(page + '：' + result['error'])
                continue
            done += 1
            for host in result.get('hosts') or []:
                if host in seen or is_excluded(host, exclude):
                    continue
                seen.add(host)
                hosts.append({'host': host, 'found_by': '滚雪球：' + page[:80], 'source_url': page})
                if len(hosts) >= MAX_HOSTS:
                    return {'pages': done, 'hosts': hosts, 'errors': errors}
    return {'pages': done, 'hosts': hosts, 'errors': errors}


def probe_host(host):
    """One read-only probe of a candidate host; never raises."""
    origin = 'https://' + host
    result = {
        'host': host,
        'ok': False,
        'error': '',
        'is_newapi': False,
        'k31': [],
        'selfhost_label': '',
        'selfhost_sentence': '',
        'system_name': '',
        'price': None,
        'quota_per_unit': None,
        'announcement': '',
        'group_ratio': None,
        'status_raw': None,
        'probed_at': '',
    }
    try:
        robots = robots_for(origin)
        page_url = origin + '/'
        if robots.can_fetch(UA.split('/')[0], page_url):
            body, final = fetch_text(page_url)
            label, sentence = selfhost_hits(extract_page(body, final)['text'])
            result['selfhost_label'] = label
            result['selfhost_sentence'] = sentence
        status_url = origin + API_STATUS
        if not robots.can_fetch(UA.split('/')[0], status_url):
            raise ValueError('robots.txt 不允许访问 /api/status')
        try:
            status, _ = _get_json(status_url)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429) or error.code >= 500:
                raise ValueError('状态接口暂时无法访问（HTTP ' + str(error.code) + '）') from None
            status = None
        except (ValueError, json.JSONDecodeError):
            status = None
        if status is None:
            result.update(ok=True, probed_at=_now())
            return result
        result['status_raw'] = status
        details = status_details(status)
        result.update(details)
        result['is_newapi'] = bool(details['system_name'] or _is_newapi(status))
        if not result['is_newapi']:
            result.update(ok=True, probed_at=_now())
            return result
        try:
            pricing, _ = _get_json(origin + API_PRICING)
        except (urllib.error.HTTPError, ValueError, json.JSONDecodeError):
            pricing = None
        if pricing is not None:
            result['group_ratio'] = (
                pricing.get('group_ratio') if isinstance(pricing, dict) else None
            )
            from browser_agent import unwrap_api

            entries = unwrap_api(pricing)
            result['k31'] = k31_entries(entries if isinstance(entries, list) else [])
        result.update(ok=True, probed_at=_now())
    except Exception as error:  # one bad host must not abort a 200-host round
        result['error'] = str(error) or error.__class__.__name__
    return result


def _is_newapi(payload):
    from browser_agent import is_newapi, unwrap_api

    try:
        return is_newapi(unwrap_api(payload))
    except Exception:
        return False


def _now():
    from collection import now

    return now()


def probe_hosts(hosts, max_parallel=MAX_PARALLEL):
    """Probe up to 200 hosts with at most four concurrent GET-only workers."""
    unique = []
    seen = set()
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            unique.append(host)
        if len(unique) >= MAX_HOSTS:
            break
    if not unique:
        return []
    workers = max(1, min(MAX_PARALLEL, int(max_parallel or MAX_PARALLEL), len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(probe_host, unique))
