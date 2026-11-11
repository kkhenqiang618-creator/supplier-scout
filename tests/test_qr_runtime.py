"""QR fixtures contain only a synthetic example.com URL."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from browser_agent import decode_qr, qr_contact_records
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
PAYLOAD = 'https://example.com/contact?source=qr&text=%E4%BD%A0%E5%A5%BD'

class QRRuntimeTests(unittest.TestCase):
    def test_decoder_reads_png_and_jpeg_by_bytes_even_with_png_extension(self):
        for name in ['contact-qr.png', 'contact-qr.jpg']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'qr.png'; shutil.copyfile(FIXTURES / name, path)
                output = subprocess.run(['node', str(ROOT / 'qr_decode.cjs'), str(path)], capture_output=True, text=True)
                self.assertEqual(output.returncode, 0, output.stderr)
                self.assertEqual(json.loads(output.stdout), [PAYLOAD])

    def test_decoder_rejects_non_image_without_payload_on_stdout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'invalid.png'; path.write_text('<html>not an image</html>')
            output = subprocess.run(['node', str(ROOT / 'qr_decode.cjs'), str(path)], capture_output=True, text=True)
            self.assertNotEqual(output.returncode, 0); self.assertEqual(output.stdout, '')

    def test_linux_screenshot_fallback_decodes_actual_png(self):
        class ScreenshotBrowser:
            def command(self, action):
                self.action = action; shutil.copyfile(FIXTURES / 'contact-qr.png', action['path'])
        browser = ScreenshotBrowser()
        snapshot = {'url': 'https://example.com', 'images': [{'ref': 'i1', 'src': 'data:image/png;base64,...'}]}
        with tempfile.TemporaryDirectory() as folder, patch('sys.platform', 'linux'), patch('browser_agent.subprocess.run', wraps=subprocess.run) as runs:
            self.assertEqual(decode_qr(snapshot, 'i1', browser, folder), [PAYLOAD])
            self.assertEqual(runs.call_args[0][0][0], 'node')
        self.assertEqual(browser.action['type'], 'screenshot')

    def test_linux_download_jpeg_retains_exact_payload_and_original_source(self):
        from io import BytesIO
        src = 'https://example.com/assets/original-contact.jpg'
        snapshot = {'url': 'https://example.com/contact', 'images': [{'ref': 'i2', 'src': src, 'alt': 'Contact support'}]}
        class Download:
            def open(self, url, timeout):
                self.url = url; return BytesIO((FIXTURES / 'contact-qr.jpg').read_bytes())
        download = Download()
        with tempfile.TemporaryDirectory() as folder, patch('sys.platform', 'linux'), patch('browser_agent.validate_url'), patch('browser_agent.urllib.request.build_opener', return_value=download):
            decoded = decode_qr(snapshot, 'i2', None, folder)
        self.assertEqual(decoded, [PAYLOAD]); self.assertEqual(download.url, src)
        contact = qr_contact_records(snapshot, 'i2', ['https://work.weixin.qq.com/ct/raw%2Fvalue?a=1&b=2'])[0]
        self.assertEqual(contact['value'], 'https://work.weixin.qq.com/ct/raw%2Fvalue?a=1&b=2')
        self.assertEqual(contact['qr_source_url'], src)

if __name__ == '__main__': unittest.main()
