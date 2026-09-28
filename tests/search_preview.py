"""Local demo server for UI checks; rejects every device endpoint.

FRAME_APK_SEARCH_DEMO=1 python3 tests/search_preview.py
"""
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import server


class Preview(server.Handler):
    def do_GET(self):
        if urlparse(self.path).path not in ('/', '/index.html', '/api/search', '/api/sources', '/api/job', '/api/host'):
            self.send_json({'error': 'Device access disabled in search preview'}, 503)
            return
        super().do_GET()

    def do_POST(self):
        self.send_json({'error': 'Writes disabled in search preview'}, 403)


if __name__ == '__main__':
    if os.environ.get('FRAME_APK_SEARCH_DEMO') != '1':
        sys.exit('Set FRAME_APK_SEARCH_DEMO=1')
    server.ThreadingHTTPServer(('127.0.0.1', 8795), Preview).serve_forever()
