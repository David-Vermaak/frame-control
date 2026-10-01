"""Local store preview. No device access; installation progress is simulated.

FRAME_APK_SEARCH_DEMO=1 python3 tests/search_preview.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import server
from apk_sources import _demo, _images, search


class Preview(server.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/android':
            self.send_json({'apps': []})
            return
        if path == '/api/android/reports':
            self.send_json({'reports': [], 'shared': False})
            return
        if path not in ('/', '/index.html', '/api/search', '/api/sources', '/api/sources/details', '/api/job', '/api/host') and not path.startswith('/source-image/'):
            self.send_json({'error': 'Headset disconnected', 'offline': True}, 503)
            return
        super().do_GET()

    def do_POST(self):
        if not self.local_request():
            return
        if urlparse(self.path).path == '/api/sources/install':
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            def work(report):
                for percent in (12, 28, 43, 67, 89):
                    report('Downloading', percent)
                    time.sleep(2)
                report('Installing', None)
                time.sleep(3)
                return {'package': 'org.preview.' + body['id'], 'message': 'Preview installation complete'}
            self.send_json(server.start_job('Preview installation', work, progress=True))
            return
        if urlparse(self.path).path == '/api/sources':
            super().do_POST()
            return
        self.send_json({'error': 'Device access disabled in store preview'}, 403)


if __name__ == '__main__':
    if os.environ.get('FRAME_APK_SEARCH_DEMO') != '1':
        sys.exit('Set FRAME_APK_SEARCH_DEMO=1')
    for name, url in json.loads((_demo.FIXTURES / 'artwork' / 'urls.json').read_text()).items():
        _images.remember(url, (_demo.FIXTURES / 'artwork' / name).read_bytes())
    with tempfile.TemporaryDirectory(prefix='frame-store-preview-') as tmp:
        search.settings_path = lambda: Path(tmp) / 'enabled.json'
        server.ThreadingHTTPServer(('127.0.0.1', 8795), Preview).serve_forever()
