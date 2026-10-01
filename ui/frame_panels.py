"""Panel switcher. Runs on the Frame, either piped over SSH or installed with
--open for its loopback-only headset page. Uses Valve's shipped vrcmd and
Chromium, not an overlay app. Spatial layout limitations: docs/panels.md.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

VRCMD = '/opt/steamvr/bin/linuxarm64/vrcmd'
PANEL_ID = 2000999030
PANEL_KEY = 'valve.steam.desktopgame.' + str(PANEL_ID)
KEY = re.compile(r'[A-Za-z0-9_.:-]{1,200}\Z')


class PanelError(Exception):
    pass


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=10,
                           env={**os.environ, 'DISPLAY': ':0', 'LC_ALL': 'C.UTF-8'})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PanelError('The panel service did not answer: ' + str(e))
    if p.returncode:
        raise PanelError((p.stderr or p.stdout).strip()[-400:] or 'Panel command failed')
    return p.stdout


def parse_overlays(text):
    """Only main dashboard panels, never their thumbnails, layers or cursors.
    vrcmd output verified on SteamVR 2.18.1, BUILD_ID 20260925.6191901.
    """
    if '---- OVERLAYS ----' not in text:
        raise PanelError('SteamVR did not return its panel list. Is the headset awake?')
    panels = []
    for line in text.splitlines():
        m = re.fullmatch(r"'([^']+)' -- '(.*)', (.*?) VROverlayType_Dashboard_Main\s*", line)
        if m and KEY.fullmatch(m[1]):
            panels.append({'key': m[1], 'title': 'Panel switcher' if m[1] == PANEL_KEY else m[2] or m[1],
                           'visible': 'not_visible' not in m[3]})
    return panels


def state():
    return {'panels': parse_overlays(run([VRCMD, '--overlays']))}


def focus(key):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise PanelError('Choose an open panel.')
    if key not in {p['key'] for p in state()['panels']}:
        raise PanelError('That panel has closed. Refresh the list.')
    run([VRCMD, '--showdashboard', key])
    # vrcmd acknowledges dispatch, not final focus (a game or the user can
    # switch again). Do not report a focus success without observing it.
    return {'requested': key, 'message': 'Asked SteamVR to show the panel.'}


PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Panel switcher [fc-panels]</title>
<style>body{background:#101b27;color:#eee;font:24px system-ui;margin:36px;max-width:1000px}
h1{font-size:36px}button{font:inherit;padding:16px 24px;border:1px solid #546574;border-radius:10px;
background:#23384b;color:white;cursor:pointer}button:focus-visible{outline:4px solid #66c0f4}
#panels{display:grid;gap:14px;margin:24px 0}#panels button{text-align:left}p{color:#bac8d5}</style>
<h1>Panel switcher</h1><p>Choose a panel to show it. Open this panel again from Steam's dashboard.</p>
<button id="refresh">Refresh</button> <button id="close">Close switcher</button>
<p id="status" role="status"></p><div id="panels"></div>
<script>
const token=location.hash.slice(1)||sessionStorage.getItem('panelKey')||'';
if(token)sessionStorage.setItem('panelKey',token);history.replaceState(null,'',location.pathname);
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',
headers:{'X-Panel-Key':token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
const s=await r.json();if(!r.ok)throw Error(s.error||'Panel request failed');return s;}
const status=document.getElementById('status');
async function refresh(){try{const s=await api('/panels');const list=document.getElementById('panels');list.replaceChildren();
for(const p of s.panels){const b=document.createElement('button');b.textContent=p.title;
b.onclick=async()=>{b.disabled=true;try{const r=await api('/focus',{key:p.key});status.textContent=r.message;}
catch(e){status.textContent=e.message;}finally{b.disabled=false;}};list.append(b);}
status.textContent=s.panels.length?'':'No open panels.';}catch(e){status.textContent=e.message;}}
document.getElementById('refresh').onclick=refresh;
document.getElementById('close').onclick=async()=>{try{await api('/close',{});window.close();}catch(e){status.textContent=e.message;}};
refresh();
</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # never log the page's access key

    def reply(self, code, value, html=False):
        data = value.encode() if html else json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        host = '127.0.0.1:' + str(self.server.server_port)
        origin = self.headers.get('Origin')
        return (self.headers.get('Host') == host and
                (origin is None or origin == 'http://' + host) and
                hmac.compare_digest(self.headers.get('X-Panel-Key', '').encode(), self.server.key.encode()))

    def do_GET(self):
        if self.path == '/':
            return self.reply(200, PAGE, html=True)  # no data or access key in the page
        if not self.allowed():
            return self.reply(403, {'error': 'Open the switcher from Frame Control.'})
        try:
            if self.path == '/panels':
                return self.reply(200, state())
            self.reply(404, {'error': 'Not found'})
        except PanelError as e:
            self.reply(502, {'error': str(e)})

    def do_POST(self):
        if not self.allowed():
            return self.reply(403, {'error': 'Forbidden'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1024:
                raise ValueError('Invalid request size')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Expected an object')
            if self.path == '/focus':
                return self.reply(200, focus(body.get('key')))
            if self.path == '/close':
                self.server.closing = True
                return self.reply(200, {'closed': True})
            self.reply(404, {'error': 'Not found'})
        except (ValueError, PanelError) as e:
            self.reply(400, {'error': str(e)})


def serve():
    """Own only our Chromium profile and process group. No changes to Steam,
    SteamVR, other Chromium sessions, or global power settings.
    """
    import fcntl  # only on the Frame; module/tests also import on Windows
    folder = Path.home() / '.local/share/frame-control/panels'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps(focus(PANEL_KEY)), flush=True)
            return
        chrome = Path.home() / 'chromium-xr/chrome'
        if chrome.is_file():
            command = [str(chrome)]
            profile = folder / 'chromium'
        elif Path('/usr/bin/chromium').is_file():
            command = ['/usr/bin/chromium']
            profile = folder / 'chromium'
        elif subprocess.run(['flatpak', 'info', 'org.chromium.Chromium'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
            command = ['flatpak', 'run', 'org.chromium.Chromium']
            profile = Path.home() / '.var/app/org.chromium.Chromium/data/frame-panel-switcher'
        else:
            raise PanelError('The headset switcher needs Chromium. The companion switcher still works.')
        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.key = secrets.token_urlsafe(32)
        server.closing = False
        server.timeout = .5
        url = 'http://127.0.0.1:%d/#%s' % (server.server_port, server.key)
        env = {**os.environ, 'DISPLAY': ':0'}
        env.pop('WAYLAND_DISPLAY', None)
        browser = subprocess.Popen([*command, '--ozone-platform=x11',
                                    '--user-data-dir=' + str(profile),
                                    '--no-first-run', '--no-default-browser-check',
                                    '--password-store=basic', '--window-size=1200,800', '--app=' + url],
                                   env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop(signum, frame):
            server.closing = True
        signal.signal(signal.SIGTERM, stop)
        win = None
        try:
            deadline = time.monotonic() + 30
            while not win and time.monotonic() < deadline and browser.poll() is None:
                server.handle_request()  # Chromium must fetch the page before it has a title
                for line in run(['xwininfo', '-root', '-children']).splitlines():
                    m = re.match(r'\s*(0x[0-9a-fA-F]+) .*\[fc-panels\]', line)
                    if m:
                        win = m[1]
                        break
            if not win:
                raise PanelError('The switcher window did not appear within 30 seconds.')
            run(['xprop', '-id', win, '-f', 'STEAM_GAME', '32c', '-set', 'STEAM_GAME', str(PANEL_ID)])
            print(json.dumps({'message': 'Opened the panel switcher in the headset.'}), flush=True)
            # Caller reads exactly one line, then disconnects; no more stdout.
            while not server.closing and browser.poll() is None:
                server.handle_request()
                # Closing the last app window need not exit Chromium.
                if win not in run(['xwininfo', '-root', '-children']):
                    break
        finally:
            server.server_close()
            if browser.poll() is None:
                os.killpg(browser.pid, signal.SIGTERM)
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(browser.pid, signal.SIGKILL)
                    browser.wait()


def open_switcher():
    # This command runs from an installed path, never from the SSH stdin copy.
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    line = proc.stdout.readline()
    proc.stdout.close()
    if not line:
        raise PanelError('The headset switcher could not start.')
    result = json.loads(line)
    if 'error' in result:
        raise PanelError(result['error'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--focus')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    try:
        if args.serve:
            serve()
        else:
            print(json.dumps(open_switcher() if args.open else focus(args.focus) if args.focus else state()))
    except (PanelError, OSError) as e:
        print(json.dumps({'error': str(e)}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
