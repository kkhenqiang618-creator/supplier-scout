"""Read-only discovery of resellers that offer Kimi K3.1 Pro (P25).

Nothing here logs in, registers or posts. One round runs in the order P25 asks
for: (a) probe every saved supplier host, (c) ask the DeepSeek Search API for
pages that resell K3.1 Pro, then (b) mine the source texts already saved with the
suppliers and the ``/api/status`` text of new-api hosts for outbound links. No
saved page is fetched a second time.

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
# Alias matching follows the saved kimi-k3.1 rule (see rules_aliases); this list is only
# the fallback when the rule carries no aliases.
K31_ALIASES = ('Kimi K3.1', 'K3.1', 'kimi-k3.1', 'kimi-k3.1-pro', 'kimi-k3.1-pro-highspeed')
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
    """One public GET (10s) through the shared egress; JSON is decoded by the caller.

    Reuses ``browser_agent.fetch_public_api`` so the address checks that the
    browser code already applies (peer IP, redirects) are not duplicated here.
    Tests replace this function.
    """
    from browser_agent import fetch_public_api

    return fetch_public_api(url, timeout=TIMEOUT)


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


def _norm_model(value):
    text = K31_PREFIX.sub('', str(value or '').strip()).lower()
    return re.sub(r'[\s_]+', '-', text)


def rules_aliases(rules):
    """The kimi-k3.1 aliases from the saved rules; the built-in list is the fallback."""
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if str(rule.get('model') or '').strip().lower() != 'kimi-k3.1':
            continue
        aliases = [str(alias).strip() for alias in rule.get('aliases') or [] if str(alias).strip()]
        if aliases:
            return tuple(aliases)
        break
    return K31_ALIASES


def _alias_extensions(normalized):
    """Variants that may trail an alias: "pro" from "kimi-k3.1-pro", and so on."""
    extensions = set()
    for short in normalized:
        for longer in normalized:
            if longer.startswith(short + '-'):
                extensions.add(longer[len(short) + 1 :])
    return extensions


def matches_k31(model_name, aliases=K31_ALIASES):
    """True when the name is one of the rule's aliases, or that alias plus a known variant."""
    normalized = sorted(
        {_norm_model(alias) for alias in aliases or () if _norm_model(alias)}, key=len, reverse=True
    )
    if not normalized:
        return False
    extensions = _alias_extensions(normalized)
    text = _norm_model(model_name)
    for needle in normalized:
        start = 0
        while True:
            index = text.find(needle, start)
            if index < 0:
                break
            before = text[index - 1] if index else ''
            rest = text[index + len(needle) :]
            if not before.isalnum() and _alias_ends(rest, extensions):
                return True
            start = index + 1
    return False


def _alias_ends(rest, extensions):
    if not rest:
        return True
    if not rest.startswith('-'):
        return False
    tail = rest[1:]
    return any(tail == variant or tail.startswith(variant + '-') for variant in extensions)


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


def k31_entries(pricing, aliases=K31_ALIASES):
    """Keep only K3.1 Pro model rows, verbatim fields plus the derived unit prices."""
    entries = []
    for entry in pricing if isinstance(pricing, list) else []:
        if not isinstance(entry, dict) or not matches_k31(entry.get('model_name'), aliases):
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
    """Readable summary of a /api/status answer; the raw payload is kept separately."""

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


def _network_reason(error):
    """A short, human-readable reason for a failed fetch; never blank."""
    detail = str(getattr(error, 'reason', '') or error).strip()
    if 'Tunnel connection failed: 403' in detail:
        return '采集代理拒绝访问该地址（域名无法解析或不是公开地址）'
    if 'Tunnel connection failed' in detail:
        return '本机代理连接失败，请检查系统代理设置（' + detail[:100] + '）'
    return detail[:150] or error.__class__.__name__


def robots_for(origin):
    """Read robots.txt, naming what actually failed instead of one blanket message.

    Only 401/403/429/5xx mean "not confirmed, retry later"; a 404 keeps the
    permissive default, and anything else reports the real reason (P33).
    """
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(origin + '/robots.txt')
    try:
        body, _ = fetch_text(origin + '/robots.txt')
        parser.parse(body.splitlines())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429) or error.code >= 500:
            raise ValueError(
                'robots.txt 无法确认允许访问（HTTP ' + str(error.code) + '），稍后重试'
            ) from None
        parser.parse([])
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ValueError('robots.txt 无法确认允许访问：' + _network_reason(error)) from None
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
            raise ValueError(
                'DeepSeek 搜索失败（HTTP 401）：请到「能力接入」核对 DeepSeek 搜索 Key'
            ) from None
        raise ValueError('DeepSeek 搜索失败（HTTP %d）：%s' % (error.code, _http_detail(error))) from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, 'reason', error)
        raise ValueError('DeepSeek 搜索失败：' + (str(reason)[:150] or '网络错误')) from None


def search_round(queries, api_key, count=SEARCH_COUNT, max_calls=SEARCH_MAX_CALLS, exclude=(), progress=None):
    """Run the keyword table (at most 20 searches) and collect hosts with their source."""
    found, seen, errors = [], set(), []
    calls = 0
    total = min(len(queries or []), max_calls)
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
        if progress:
            progress(calls - 1, total, SEARCH_LABEL + text[:60])
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
    if progress:
        progress(calls, total, '')
    return {'calls': calls, 'hosts': found, 'errors': errors}


def snowball_hosts(texts, exclude=(), progress=None):
    """Outbound hosts written in saved texts; nothing is fetched again (P25 b).

    ``texts`` items are ``{'label', 'url', 'text'}`` built from the source excerpts
    stored with each supplier and from the ``/api/status`` footer/about text of the
    new-api hosts probed in the same round.
    """
    hosts, seen, scanned = [], set(), 0
    items = [item for item in texts or [] if str((item or {}).get('text') or '').strip()]
    total = len(items)
    for item in items:
        label = str(item.get('label') or '')
        url = str(item.get('url') or '')
        for host in hosts_from_text(item.get('text')):
            if host in seen or is_excluded(host, exclude):
                continue
            seen.add(host)
            hosts.append({'host': host, 'found_by': label[:200], 'source_url': url})
            if len(hosts) >= MAX_HOSTS:
                break
        scanned += 1
        if progress:
            progress(scanned, total, '')
        if len(hosts) >= MAX_HOSTS:
            break
    return {'sources': scanned, 'hosts': hosts, 'errors': []}


def _status_texts(probes):
    """Text mined from the saved /api/status payload for the snowball step.

    Only ``status_raw`` is stored on a probe (P31), so the footer, about and
    announcement text is read back from it instead of being copied.
    """
    from browser_agent import api_text, unwrap_api

    items = []
    for probe in probes:
        raw = probe.get('status_raw')
        if not raw:
            continue
        inner = unwrap_api(raw)
        data = inner if isinstance(inner, dict) else {}
        parts = [
            str(data.get('footer_html') or ''),
            str(data.get('about') or ''),
            api_text(data),
        ]
        text = '\n'.join(part for part in parts if part)
        if text.strip():
            items.append(
                {
                    'label': '滚雪球：' + str(probe.get('host') or '') + API_STATUS,
                    'url': 'https://' + str(probe.get('host') or ''),
                    'text': text,
                }
            )
    return items


def run_round(
    suppliers=(),
    known_hosts=(),
    pasted=(),
    queries=(),
    api_key='',
    key_error='',
    exclude=(),
    aliases=K31_ALIASES,
    progress=None,
):
    """One a→c→b round; returns the probes to store and a summary for the page.

    ``suppliers`` items are ``{'host', 'url', 'sources': [excerpt, ...]}``. Every
    supplier host is probed (a), then the keyword table is searched (c) when a key
    is configured, then the saved source texts and the status pages are mined for
    outbound links (b) and only those outbound hosts are fetched. ``pasted`` holds
    source URLs the user typed in by hand; those pages are fetched once.
    """

    def step(stage, done=0, total=0, note=''):
        if progress:
            progress(stage, done, total, note)

    items = [item for item in suppliers or [] if isinstance(item, dict) and item.get('host')]
    supplier_by_host = {}
    for item in items:
        supplier_by_host.setdefault(item['host'], item)
    known = set(known_hosts or ())
    todo = list(supplier_by_host) + [host for host in sorted(known) if host not in supplier_by_host]

    # (a) probe every saved supplier URL, and refresh the candidates already listed
    probed = probe_hosts(
        todo,
        aliases,
        limit=None,
        progress=lambda done, total, note: step('探测已有名单', done, total, note),
    )
    probes, supplier_probed = [], 0
    for probe in probed:
        host = probe['host']
        item = supplier_by_host.get(host)
        if item is not None:
            probe['found_by'] = '已有名单'
            probe['source_url'] = str(item.get('url') or '')
            supplier_probed += 1
            # A supplier with nothing to see does not need a row in the candidate list.
            if not (probe.get('is_newapi') or probe.get('k31') or probe.get('selfhost_label')):
                continue
        probes.append(probe)

    # (c) keyword search
    skipped, errors, found, search_calls = [], [], [], 0
    if key_error:
        skipped.append('搜索：本机保存的 Key 无法解密，请重新填写')
    elif not api_key:
        skipped.append('搜索：未接入 DeepSeek 搜索 Key，已跳过')
    elif not queries:
        skipped.append('搜索：关键词表为空，已跳过')
    else:
        result = search_round(
            queries,
            api_key,
            exclude=exclude,
            progress=lambda done, total, note: step('搜索关键词', done, total, note),
        )
        found.extend(result['hosts'])
        errors.extend(result['errors'])
        search_calls = result['calls']

    # (b) mine what is already saved: supplier source excerpts + status page text
    step('整理已存来源', 0, 0, '')
    texts = []
    for item in items:
        url = str(item.get('url') or '')
        for text in item.get('sources') or []:
            if str(text or '').strip():
                texts.append({'label': '滚雪球：' + url[:80], 'url': url, 'text': str(text)})
    texts.extend(_status_texts(probed))
    snowball = snowball_hosts(
        texts,
        exclude=exclude,
        progress=lambda done, total, note: step('整理已存来源', done, total, note),
    )

    # hand-pasted pages are the user's own input; fetch each once
    pasted_hosts = []
    pasted_urls = [str(value).strip() for value in pasted or [] if str(value).strip()]
    paste_errors = []
    for index, source in enumerate(pasted_urls):
        step('粘贴来源页', index, len(pasted_urls), source[:80])
        try:
            page = discover(source)
        except (ValueError, OSError) as error:
            paste_errors.append(str(error)[:200] or error.__class__.__name__)
            continue
        for host in page['hosts']:
            pasted_hosts.append(
                {'host': host, 'found_by': '粘贴：' + source[:120], 'source_url': page['source_url']}
            )
    # A dead pasted page is reported, but it must not throw away the probes and
    # searches that already succeeded in this round (P31).
    for message in paste_errors:
        errors.append('粘贴来源页：' + (message or '打不开'))

    # merge the new hosts, drop suppliers and hosts that were already probed
    targets, seen, origins = [], set(), {}
    for item in found + snowball['hosts'] + pasted_hosts:
        host = item['host']
        if (
            not host
            or host in supplier_by_host
            or host in known
            or host in seen
            or is_excluded(host, exclude)
        ):
            continue
        seen.add(host)
        origins[host] = item
        targets.append(host)
        if len(targets) >= MAX_HOSTS:
            break

    for probe in probe_hosts(
        targets,
        aliases,
        progress=lambda done, total, note: step('探测新主机', done, total, note),
    ):
        origin = origins.get(probe['host'], {})
        probe['found_by'] = origin.get('found_by', '')
        probe['source_url'] = origin.get('source_url', '')
        probes.append(probe)

    summary = {
        'probed': supplier_probed,
        'search_calls': search_calls,
        'search_hosts': len(found),
        'snowball_sources': snowball['sources'],
        'snowball_hosts': len(snowball['hosts']),
        'pasted_hosts': len(pasted_hosts),
        'found_hosts': len(found) + len(snowball['hosts']) + len(pasted_hosts),
        'new_hosts': len([host for host in targets if host not in known]),
        'skipped': skipped,
        'errors': errors[:10],
    }
    return {'probes': probes, 'summary': summary}


def probe_host(host, aliases=K31_ALIASES):
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
            result['group_ratio'] = pricing.get('group_ratio') if isinstance(pricing, dict) else None
            from browser_agent import unwrap_api

            entries = unwrap_api(pricing)
            result['k31'] = k31_entries(entries if isinstance(entries, list) else [], aliases)
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


def probe_hosts(hosts, aliases=K31_ALIASES, max_parallel=MAX_PARALLEL, progress=None, limit=MAX_HOSTS):
    """Probe hosts with at most four concurrent GET-only workers.

    Newly discovered hosts are capped at ``limit``; the saved supplier list is probed
    in full (``limit=None``) because P25 asks for every supplier URL.
    """
    unique = []
    seen = set()
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            unique.append(host)
        if limit is not None and len(unique) >= limit:
            break
    if not unique:
        return []
    workers = max(1, min(MAX_PARALLEL, int(max_parallel or MAX_PARALLEL), len(unique)))
    results, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(lambda host: probe_host(host, aliases), unique):
            results.append(result)
            done += 1
            if progress:
                progress(done, len(unique), '探测：' + unique[done - 1])
    return results
