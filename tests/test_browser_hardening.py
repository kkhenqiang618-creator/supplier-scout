"""Browser hardening without a real browser: QR source order, data: URI limits, read timeout, render_weak status, prompt budget."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import browser_agent
from browser_agent import Browser, BrowserDeadError, data_uri_bytes, decode_qr, observed_text, prompt_context, run_agent
from model_service import ModelService
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
PAYLOAD = 'https://example.com/contact?source=qr&text=%E4%BD%A0%E5%A5%BD'
QR_URI = 'data:image/png;base64,' + base64.b64encode((FIXTURES / 'contact-qr.png').read_bytes()).decode()
BLANK_URI = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\0' * 32).decode()

class Recorder:
    def __init__(self, image=None): self.actions = []; self.image = image or {}
    def command(self, action):
        self.actions.append(action['type'])
        if action['type'] == 'image_data': return {'ok': True, 'image': self.image}
        raise ValueError('截图不应被调用')

def linux_decoder():
    return patch('sys.platform', 'linux')

class QRSourceOrderTests(unittest.TestCase):
    def test_data_uri_decodes_bytes_without_browser(self):
        with tempfile.TemporaryDirectory() as folder, linux_decoder():
            browser = Recorder()
            self.assertEqual(decode_qr({'images': [{'ref': 'i0', 'kind': 'img', 'src': QR_URI}]}, 'i0', browser, folder), [PAYLOAD])
        self.assertEqual(browser.actions, [])

    def test_oversized_data_uri_rejected_before_decoding(self):
        with self.assertRaisesRegex(ValueError, '过大'): data_uri_bytes('data:image/png;base64,' + 'A' * (4 * 1024 * 1024 + 8))
        self.assertIsNone(data_uri_bytes('data:image/svg+xml,<svg/>')); self.assertIsNone(data_uri_bytes('data:image/png;base64,@@@'))

    def test_canvas_uses_image_data_then_recorded_source_via_safe_download(self):
        image = {'kind': 'canvas', 'data_uri': BLANK_URI, 'sources': ['https://cdn.example.com/qr.png']}
        browser = Recorder(image); opened = []
        class Download:
            def open(self, url, timeout): opened.append(url); return BytesIO((FIXTURES / 'contact-qr.png').read_bytes())
        with tempfile.TemporaryDirectory() as folder, linux_decoder(), patch('browser_agent.validate_url') as checked, patch('browser_agent.urllib.request.build_opener', return_value=Download()):
            decoded = decode_qr({'images': [{'ref': 'i3', 'kind': 'canvas', 'src': '', 'tag': 'CANVAS'}]}, 'i3', browser, folder)
        self.assertEqual(decoded, [PAYLOAD]); self.assertEqual(browser.actions, ['image_data'])
        self.assertEqual(opened, ['https://cdn.example.com/qr.png']); checked.assert_called_with('https://cdn.example.com/qr.png')

    def test_background_downloads_in_python_and_private_url_never_fetched(self):
        snapshot = {'images': [{'ref': 'i1', 'kind': 'background', 'src': 'http://10.0.0.5/qr.png'}]}
        class Screenshot(Recorder):
            def command(self, action):
                self.actions.append(action['type'])
                if action['type'] == 'screenshot': Path(action['path']).write_bytes((FIXTURES / 'contact-qr.png').read_bytes()); return {'ok': True}
                return {'ok': True, 'image': {'kind': 'background', 'url': 'http://10.0.0.5/qr.png'}}
        browser = Screenshot()
        with tempfile.TemporaryDirectory() as folder, linux_decoder(), patch('browser_agent.urllib.request.build_opener', side_effect=AssertionError('不应下载内网地址')):
            self.assertEqual(decode_qr(snapshot, 'i1', browser, folder), [PAYLOAD])
        self.assertEqual(browser.actions, ['image_data', 'screenshot'])

    def test_dead_browser_is_not_swallowed(self):
        class Dead:
            def command(self, action): raise BrowserDeadError('浏览器无响应')
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(BrowserDeadError):
            decode_qr({'images': [{'ref': 'i0', 'kind': 'canvas', 'src': ''}]}, 'i0', Dead(), folder)

class ReadTimeoutTests(unittest.TestCase):
    def test_silent_worker_raises_and_is_killed(self):
        browser = Browser.__new__(Browser)
        browser.worker = subprocess.Popen([sys.executable, '-c', 'import sys,time\nfor line in sys.stdin: time.sleep(60)'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        import queue, threading
        browser.replies = queue.Queue(); threading.Thread(target=Browser._read, args=(browser.worker.stdout, browser.replies), daemon=True).start()
        try:
            start = time.monotonic()
            with self.assertRaisesRegex(ValueError, '浏览器无响应'): browser.command({'type': 'snapshot'}, timeout=0.5)
            self.assertLess(time.monotonic() - start, 5); self.assertIsNotNone(browser.worker.poll())
            with self.assertRaisesRegex(ValueError, '浏览器连接中断'): browser.command({'type': 'snapshot'})
        finally:
            if browser.worker.poll() is None: browser.worker.kill()
            browser.worker.wait(); browser.worker.stdout.close(); browser.worker.stdin.close()

    def test_worker_bounds_every_command(self):
        source = (ROOT / 'agent_browser.cjs').read_text(encoding='utf-8')
        self.assertLess(int(source.split('COMMAND_BUDGET=')[1].split(',')[0]) / 1000, browser_agent.COMMAND_TIMEOUT)
        for guard in ['该操作超出只读采集范围', '仅允许填写站内搜索框', '只记录该联系链接，不启动外部应用', "browser.contexts()[0]||await browser.newContext()"]: self.assertIn(guard, source)
        # The only proxy is the Chrome fallback's loopback GuardProxy; the Moli (cdp) path has none and nothing page-controlled picks the server.
        self.assertEqual(source.count('proxy:{'), 1); self.assertIn("proxy:{server:'http://127.0.0.1:'+target}", source)
        self.assertIn("delete process.env.PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", source)

class RenderWeakTests(unittest.TestCase):
    def run_site(self, snapshots, answers):
        snapshots = list(snapshots); commands = []
        class FakeBrowser:
            def __init__(self, folder): pass
            def command(self, action):
                commands.append(action)
                item = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
                if isinstance(item, Exception): raise item
                return {'snapshot': item}
            def close(self): pass
        answers = list(answers)
        with tempfile.TemporaryDirectory() as folder:
            service = ModelService(os.path.join(folder, 'model.json')); service.save({'base_url': 'https://model.example/v1', 'model': 'fixture'}); data = service.read(); data['tested'] = True; service._write(data)
            with patch('browser_agent.validate_url'), patch('engine.fetch', return_value=('User-agent: *\nAllow: /', 'https://example.com/robots.txt')), patch.object(service, 'complete', side_effect=lambda m: answers.pop(0)):
                return run_agent('https://example.com', 'contacts', service, [], browser_factory=FakeBrowser, max_steps=4), commands

    def test_weak_render_without_contacts_is_blocked_with_reason(self):
        weak = {'url': 'https://example.com/', 'text': 'Loading', 'controls': [], 'images': [], 'render_weak': True, 'goto_timeout': True}
        result, _ = self.run_site([weak], [json.dumps({'action': {'type': 'finish'}})])
        self.assertEqual(result['contact_status'], 'blocked'); self.assertTrue(result['render_weak'])
        self.assertIn('Chrome', result['contact_summary']); self.assertTrue(any(w.startswith(browser_agent.RENDER_WEAK_WARNING) for w in result['warnings']))

    def test_weak_subpage_after_good_homepage_is_not_a_site_failure(self):
        good = {'url': 'https://example.com/', 'text': '正文' * 100, 'controls': [], 'images': []}
        weak = {'url': 'https://example.com/', 'text': '', 'controls': [], 'images': [], 'render_weak': True}
        result, _ = self.run_site([good, weak], [json.dumps({'action': {'type': 'wait'}}), json.dumps({'action': {'type': 'finish'}})])
        self.assertEqual(result['contact_status'], 'incomplete'); self.assertNotIn('render_weak', result)

    def test_weak_render_with_contacts_keeps_found(self):
        weak = {'url': 'https://example.com/', 'text': '商务邮箱 sales@example.com', 'controls': [], 'images': [], 'render_weak': True}
        record = {'type': 'email', 'value': 'sales@example.com', 'source_url': 'https://example.com/', 'evidence': '商务邮箱 sales@example.com'}
        result, _ = self.run_site([weak], [json.dumps({'contacts': [record], 'action': {'type': 'finish'}})])
        self.assertEqual(result['contact_status'], 'found')

    def test_dead_browser_ends_site_without_spending_more_steps(self):
        good = {'url': 'https://example.com/', 'text': '正文' * 100, 'controls': [], 'images': []}
        result, commands = self.run_site([good, BrowserDeadError('浏览器无响应')], [json.dumps({'action': {'type': 'wait'}})] * 4)
        self.assertEqual(len(commands), 2); self.assertEqual(result['contact_status'], 'blocked'); self.assertIn('浏览器无响应', result['warnings'])

class SnapshotShapeTests(unittest.TestCase):
    def test_prompt_budget_with_kinds_and_new_keys(self):
        uri = 'data:image/png;base64,' + 'A' * 100000
        images = [{'ref': 'i' + str(i), 'kind': ['img', 'canvas', 'background'][i % 3], 'src': uri if i % 3 != 1 else '', 'alt': '图', 'context': 'x' * 250, 'tag': 'DIV'} for i in range(160)]
        page = {'url': 'https://example.com/', 'text': '正文' * 20000, 'tail': '尾' * 10000, 'footer': '页尾', 'dialogs': ['公告' * 4000] * 8, 'controls': [{'ref': 'c' + str(i), 'role': 'link', 'name': '名' * 150, 'label': '', 'href': '/p/' + 'x' * 1000} for i in range(400)], 'images': images, 'render_weak': True, 'page_unresponsive': True, 'unresponsive_reason': '页面快照超时'}
        prompt = json.dumps(prompt_context({'page': page, 'history': [], 'known_contacts': [], 'known_offers': []}), ensure_ascii=False)
        self.assertLessEqual(len(prompt), browser_agent.PROMPT_BUDGET); self.assertNotIn('AAAA', prompt); self.assertIn('"kind"', prompt); self.assertIn('render_weak', prompt)

    def test_control_name_is_observable(self):
        self.assertIn('联系客服', observed_text({'controls': [{'ref': 'c0', 'label': '', 'name': '联系客服'}]}))

class HardeningGuardTests(unittest.TestCase):
    def test_private_peer_after_connect_is_refused(self):
        class Sock:
            closed = False
            def __init__(self, ip): self.ip = ip
            def getpeername(self): return (self.ip, 443)
            def close(self): self.closed = True
        for ip in ['127.0.0.1', '169.254.169.254', '10.0.0.5', '::1']:
            sock = Sock(ip)
            with self.assertRaisesRegex(ValueError, '内网'): browser_agent.public_peer(sock)
            self.assertTrue(sock.closed)
        self.assertEqual(browser_agent.public_peer(Sock('93.184.216.34')).ip, '93.184.216.34')

    def test_download_uses_peer_checked_handlers(self):
        built = []
        def opener(*handlers): built.extend(type(h) for h in handlers); raise ValueError('stop')
        with patch('browser_agent.validate_url'), patch('browser_agent.urllib.request.build_opener', side_effect=opener), self.assertRaises(ValueError):
            browser_agent.download_image('https://cdn.example.com/qr.png')
        self.assertIn(browser_agent.PeerHTTPHandler, built); self.assertIn(browser_agent.PeerHTTPSHandler, built)

    def test_oversized_worker_reply_refused_before_parsing(self):
        import queue
        browser = Browser.__new__(Browser)
        class Alive:
            stdin = BytesIO()
            def poll(self): return None
        browser.worker = Alive(); browser.worker.stdin = type('W', (), {'write': lambda self, v: None, 'flush': lambda self: None})()
        browser.replies = queue.Queue(); browser.replies.put('{"ok":true,"x":"' + 'A' * (browser_agent.REPLY_MAX + 1) + '"}\n')
        with patch('browser_agent.json.loads', side_effect=AssertionError('不应解析')), self.assertRaisesRegex(ValueError, '过大'): browser.command({'type': 'image_data', 'ref': 'i0'})

    def test_worker_caps_page_written_draw_list_and_streams_fetch(self):
        source = (ROOT / 'agent_browser.cjs').read_text(encoding='utf-8')
        self.assertNotIn("sources:JSON.parse(e.getAttribute('data-scout-draw')||'[]')", source)
        self.assertIn("Array.isArray(list)?list.filter(s=>typeof s==='string'&&s.length<4500000).slice(0,4)", source)
        self.assertIn('response.body.getReader()', source); self.assertNotIn('response.arrayBuffer()', source)
        self.assertIn('e!==document.body&&e!==document.documentElement', source)

if __name__ == '__main__': unittest.main()
