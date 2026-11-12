"""Real-browser regression harness: Moli and the Chrome fallback (headless shell via GuardProxy) against local fixtures, no internet, no model.
Run: python3 tests/browser_regression.py            (unittest; asserts every check that passes in the baseline)
     python3 tests/browser_regression.py --baseline (re-run and rewrite browser_regression_baseline.json)
Not picked up by `unittest discover` (file name has no test_ prefix) because it starts a real browser (~1 min).
Loopback access: Browser(folder, allow_loopback_for_tests=True) swaps --block-private-networks for --block-cidrs
covering every non-loopback private/reserved range (Moli 1.1.23 has no allowlist flag). Production never passes it.
decode_qr's http download path calls validate_url, which rejects 127.0.0.1:<port>; here validate_url is relaxed for the
fixture origin only (and the connected-peer check public_peer is bypassed), so the check measures the download+decode path instead of the SSRF guard.
Chrome checks are the same cases prefixed 'chrome:'; ChromeBrowser(folder, allow_loopback_for_tests=True) lets its GuardProxy pass loopback only,
and 'chrome:proxy.*' checks prove the fixture traffic went through the proxy and that metadata/private/rebinding targets are refused.
"""
import base64
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import browser_agent
from browser_agent import Browser, ChromeBrowser, decode_qr, observed_text
PAGES = Path(__file__).resolve().parent / 'browser_fixtures'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
BASELINE = Path(__file__).resolve().parent / 'browser_regression_baseline.json'
HANG_LIMIT = 60

def runtime_ready():
    try: browser_agent.runtime_paths(); return (ROOT / 'node_modules' / 'playwright' / 'package.json').exists()
    except ValueError: return False

def expected_payload():
    class Copy:
        def command(self, action): shutil.copyfile(FIXTURES / 'contact-qr.png', action['path'])
    with tempfile.TemporaryDirectory() as folder:
        return decode_qr({'url': 'about:blank', 'images': [{'ref': 'i0', 'src': ''}]}, 'i0', Copy(), folder)

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        name = self.path.split('?')[0].lstrip('/')
        if name == 'qr_datauri.html':
            uri = 'data:image/png;base64,' + base64.b64encode((FIXTURES / 'contact-qr.png').read_bytes()).decode()
            body = (PAGES / name).read_text(encoding='utf-8').replace('{{QR_DATA_URI}}', uri).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        if (FIXTURES / name).is_file() and '/' not in name: self.directory = str(FIXTURES)
        return super().do_GET()

def serve():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(PAGES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, 'http://127.0.0.1:%d/' % server.server_address[1]

def timed(browser, action, limit=45):
    box = {}
    def run():
        try: box['result'] = browser.command(action)
        except Exception as error: box['error'] = str(error)
    start = time.monotonic(); thread = threading.Thread(target=run, daemon=True); thread.start(); thread.join(limit)
    elapsed = round(time.monotonic() - start, 2)
    if thread.is_alive(): return None, elapsed, '超时 %ds 未返回' % limit
    return box.get('result'), elapsed, box.get('error')

# (fixture, contact strings that must be observable, modal text expected in dialogs, image predicate for QR)
CASES = [
    ('footer_contacts', ['sales@xinghe-fixture.com', '735812946', 'https://t.me/xinghe_fixture_sales'], None, None),
    ('modal_vue', ['yunchuan_biz2026'], 'yunchuan_biz2026', lambda i: i.get('tag') == 'IMG'),
    ('modal_aria', ['partner@neon-fixture.cn'], 'partner@neon-fixture.cn', None),
    ('qr_canvas', ['扫码联系客服'], None, lambda i: i.get('kind') == 'canvas'),
    ('qr_background', ['扫码添加商务微信'], None, lambda i: i.get('kind') == 'background'),
    ('qr_datauri', ['扫码联系商务客服'], None, lambda i: i.get('kind') == 'img'),
    ('spa_slow', ['jiguang_sales88', 'biz@jiguang-fixture.com'], None, None),
    ('empty', ['本站页面暂无任何内容'], None, None),  # text_length floor 10 is a sanity bound; the sentence itself is asserted
    ('modal_body_class', ['biz_wx_777'], '商务微信：biz_wx_777', None),
    ('splash_anim', ['bizdev@qiming-fixture.com', 'qiming_biz_2026', 'https://t.me/qiming_fixture_biz'], None, None),
]
# Rendered only after a CSS animationend: Moli 1.1.23 never fires it (weak, the real 'stuck on Loading' sites), Chrome does.
WEAK_ON = {'': {'splash_anim'}, 'chrome:': set()}
BACKENDS = [('', Browser), ('chrome:', ChromeBrowser)]
# render_weak = visible text under 150 chars after open+waits (what sends a site to the Chrome fallback). Only footer_contacts and
# splash_anim (once rendered) are above that line; the other fixtures are intentionally tiny, so 'empty' must be flagged.
STRONG = {'footer_contacts', 'splash_anim'}
# True = works on current code (asserted); False = known gap recorded in the baseline, flipped by later work.
WORKS = {}

def check(results, name, ok, detail='', seconds=None):
    results.append({'check': name, 'pass': bool(ok), 'detail': ' '.join(str(detail).split())[:200], **({'seconds': seconds} if seconds is not None else {})})

def run_backend(prefix, factory, base, folder, payload, relaxed, results):
    add = lambda name, *rest, **kw: check(results, prefix + name, *rest, **kw)
    browser = factory(folder, allow_loopback_for_tests=True)
    try:
        for name, contacts, modal, pick in CASES:
            result, seconds, error = timed(browser, {'type': 'open', 'url': base + name + '.html'})
            snap = (result or {}).get('snapshot') or {}
            add(name + '.open', snap and not error, error or snap.get('url'), seconds)
            if name in WEAK_ON[prefix]:
                # Expected weak on this backend: the flag must be raised (that is what sends a real site to Chrome); text is not asserted.
                add(name + '.render_weak_flagged', snap.get('render_weak') is True, len(snap.get('text', '')))
                result, seconds, error = timed(browser, {'type': 'wait'})
                add(name + '.still_weak_after_wait', ((result or {}).get('snapshot') or {}).get('render_weak') is True, error or len(((result or {}).get('snapshot') or {}).get('text', '')), seconds)
                continue
            text = observed_text(snap) if snap else ''
            add(name + '.text_length', len(snap.get('text', '')) >= (10 if name == 'empty' else 40), len(snap.get('text', '')))
            if name in STRONG: add(name + '.not_render_weak', snap and not snap.get('render_weak'), snap.get('render_weak'))
            if name == 'empty': add('empty.render_weak_flagged', snap.get('render_weak') is True, snap.get('render_weak'))
            if name == 'modal_body_class': add('modal_body_class.body_not_dialog', not any(d.startswith('主页内容') for d in snap.get('dialogs', [])), [d[:30] for d in snap.get('dialogs', [])])
            if name == 'modal_vue': add('modal_vue.dialog_deduped', len(snap.get('dialogs', [])) == 1, len(snap.get('dialogs', [])))
            if name == 'footer_contacts': add('footer_contacts.control_role', any(c.get('role') == 'link' for c in snap.get('controls', [])), [c.get('role') for c in snap.get('controls', [])][:5])
            for value in contacts: add(name + '.observed:' + value, value in text)
            if modal: add(name + '.dialog_captured', any(modal in d for d in snap.get('dialogs', [])), len(snap.get('dialogs', [])))
            images = snap.get('images', [])
            if pick:
                add(name + '.image_count', images, len(images))
                image = next((i for i in images if pick(i)), None)
                if image:
                    start = time.monotonic()
                    try:
                        with patch('browser_agent.validate_url', relaxed), patch('engine.validate_url', relaxed), patch('browser_agent.public_peer', lambda sock: sock): decoded = decode_qr(snap, image['ref'], browser, folder)
                        add(name + '.qr_decoded', decoded == payload, decoded, round(time.monotonic() - start, 2))
                    except Exception as error: add(name + '.qr_decoded', False, error, round(time.monotonic() - start, 2))
                else: add(name + '.qr_decoded', False, '无可用图片编号')
            if name == 'spa_slow':
                result, seconds, error = timed(browser, {'type': 'wait'})
                later = observed_text((result or {}).get('snapshot') or {})
                add('spa_slow.after_wait_observed', all(v in later for v in contacts), error or '', seconds)
        if prefix == 'chrome:': proxy_checks(browser, base, add)
    finally: browser.close()
    hang_dir = Path(folder) / 'hang'; hang_dir.mkdir()
    browser = factory(hang_dir, allow_loopback_for_tests=True)
    try:
        result, seconds, error = timed(browser, {'type': 'open', 'url': base + 'hang.html'})
        snap = (result or {}).get('snapshot') or {}
        ref = next((c['ref'] for c in snap.get('controls', []) if c.get('label') == '查看公告'), None)
        add('hang.open', ref, error or ref, seconds)
        if ref:
            result, seconds, error = timed(browser, {'type': 'click', 'ref': ref}, HANG_LIMIT)
            add('hang.click_returns_within_15s', seconds < 15 and result is not None, error or 'ok', seconds)
            # The worker's 2.5s probe plus its bounded evaluate return a page_unresponsive snapshot; Moli's ~10s V8 watchdog is only the fallback.
            add('hang.click_returns_within_5s', seconds < 5 and result is not None, error or 'ok', seconds)
            add('hang.click_flagged_unresponsive', ((result or {}).get('snapshot') or {}).get('page_unresponsive') is True, ((result or {}).get('snapshot') or {}).get('unresponsive_reason'))
            result, seconds, error = timed(browser, {'type': 'snapshot'}, 20) if result is not None else (None, 0, '点击未返回，跳过')
            add('hang.snapshot_returns_within_15s', seconds < 15 and result is not None, error or 'ok', seconds)
    finally:
        start = time.monotonic(); browser.close()
        add('hang.close_within_10s', time.monotonic() - start < 10, '', round(time.monotonic() - start, 2))
    # A worker that stops answering must surface as '浏览器无响应' within Browser.command's read timeout, not stall the site.
    stall_dir = Path(folder) / 'stall'; stall_dir.mkdir()
    browser = factory(stall_dir, allow_loopback_for_tests=True)
    try:
        os.kill(browser.worker.pid, signal.SIGSTOP)
        start = time.monotonic()
        try: browser.command({'type': 'snapshot'}, timeout=3); error = 'returned'
        except ValueError as raised: error = str(raised)
        add('stall.read_timeout_raises', error == '浏览器无响应', error, round(time.monotonic() - start, 2))
    finally: browser.close()

def proxy_checks(browser, base, add):
    """Real Chrome through its GuardProxy: fixture requests must appear in the proxy log; metadata, private and rebinding targets are refused."""
    port = base.rstrip('/').rsplit(':', 1)[1]
    add('proxy.loopback_via_proxy', any(t.startswith(base) for _, t, _ in browser.proxy.allowed), [t for _, t, _ in list(browser.proxy.allowed)[:2]])
    calls = {}
    def resolver(host, port_, **kw):
        # rebind.fixture-test: loopback on the first lookup, 10.0.0.5 afterwards; private.fixture-test: always 10.0.0.5.
        calls[host] = calls.get(host, 0) + 1
        ip = '127.0.0.1' if host == 'rebind.fixture-test' and calls[host] == 1 else '10.0.0.5' if host.endswith('.fixture-test') else None
        if ip is None: return __import__('socket').getaddrinfo(host, port_, **kw)
        return [(2, 1, 6, '', (ip, port_))]
    browser.proxy.resolver = resolver
    for name, url, expect in [('metadata', 'http://169.254.169.254/latest/meta-data/', None), ('private_dns', 'http://private.fixture-test/footer_contacts.html', None),
                              ('ipv4_mapped', 'http://[::ffff:10.0.0.1]/', None), ('rebind_pinned', 'http://rebind.fixture-test:' + port + '/footer_contacts.html', 'sales@xinghe-fixture.com')]:
        result, seconds, error = timed(browser, {'type': 'open', 'url': url})
        text = observed_text((result or {}).get('snapshot') or {})
        ok = (expect in text) if expect else ('sales@' not in text and not any(t == url for _, t, _ in browser.proxy.allowed))
        add('proxy.' + name, ok, error or text[:60], seconds)
    add('proxy.denials_logged', any('169.254.169.254' in t for t, _ in browser.proxy.denied) and any('private.fixture-test' in t for t, _ in browser.proxy.denied), [t[:40] for t, _ in list(browser.proxy.denied)[-4:]])
    add('proxy.rebind_resolved_once', calls.get('rebind.fixture-test') == 1, calls.get('rebind.fixture-test'))

def run_all():
    payload = expected_payload(); server, base = serve(); results = []
    origin = base.rstrip('/'); real_validate = browser_agent.validate_url
    relaxed = lambda url: url if url.startswith(origin + '/') else real_validate(url)
    try:
        with tempfile.TemporaryDirectory(prefix='scout-regression-') as folder:
            for prefix, factory in BACKENDS:
                sub = Path(folder) / (prefix.rstrip(':') or 'moli'); sub.mkdir()
                run_backend(prefix, factory, base, str(sub), payload, relaxed, results)
    finally: server.shutdown()
    return payload, results

def table(results):
    rows = ['%-52s %-6s %-8s %s' % ('check', 'pass', 'seconds', 'detail')]
    for r in results: rows.append('%-52s %-6s %-8s %s' % (r['check'], 'PASS' if r['pass'] else 'FAIL', r.get('seconds', ''), r['detail'][:70]))
    return '\n'.join(rows)

def load_works():
    if BASELINE.exists(): WORKS.update({r['check']: r['pass'] for r in json.loads(BASELINE.read_text(encoding='utf-8'))['checks']})

@unittest.skipUnless(runtime_ready(), '需要 runtime/moli 与 node_modules/playwright')
class BrowserRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls): load_works(); cls.payload, cls.results = run_all(); print('\n' + table(cls.results))
    def test_checks_that_work_today_still_work(self):
        for r in self.results:
            if WORKS.get(r['check']):
                with self.subTest(check=r['check']): self.assertTrue(r['pass'], r['detail'])
        fixed = [r['check'] for r in self.results if r['pass'] and WORKS.get(r['check']) is False]
        if fixed: print('基线中失败、现已通过：', ', '.join(fixed))

if __name__ == '__main__':
    if '--baseline' in sys.argv:
        if not runtime_ready(): sys.exit('需要 runtime/moli 与 node_modules/playwright')
        payload, results = run_all(); print(table(results))
        BASELINE.write_text(json.dumps({'generated': time.strftime('%Y-%m-%d %H:%M:%S'), 'moli': 'runtime/moli', 'chrome': 'chromium-headless-shell (Playwright default or SCOUT_CHROME_BIN)', 'expected_qr_payload': payload,
            'note': 'pass=true 的检查由 tests/browser_regression.py 断言；pass=false 为当前已知缺口，后续改进应翻转为 true 并重写基线。',
            'checks': results}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    else: unittest.main()
