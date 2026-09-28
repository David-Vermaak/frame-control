"""Agent actions and one-use human approvals. No model SDK or network calls here."""
import hashlib
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time


class Approvals:
    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()

    def request(self, action):
        with self.lock:
            now = time.monotonic()
            self.pending = {k: v for k, v in self.pending.items() if v['expires'] > now}
            if len(self.pending) >= 100:
                raise ValueError('Too many pending approvals; wait five minutes')
            token = secrets.token_urlsafe(24)
            self.pending[token] = {'action': action, 'approved': False, 'expires': now + 300}
        return {'confirmation': token, 'action': action, 'approvalPath': '/assistant#confirm=' + token,
                'message': 'Ask the user to review and approve this action in Frame Control, then retry with confirmation. Expires in five minutes.'}

    def entry(self, token):
        entry = self.pending.get(token)
        if not entry or entry['expires'] <= time.monotonic():
            raise ValueError('Approval expired or unknown; request a new one')
        return entry

    def inspect(self, token):
        with self.lock:
            entry = self.entry(token)
            return {'action': entry['action'], 'approved': entry['approved']}

    def decide(self, token, accept):
        with self.lock:
            entry = self.entry(token)
            if accept is True:
                entry['approved'] = True
            else:
                del self.pending[token]
        return {'message': 'Approved for one use' if accept is True else 'Rejected'}

    def consume(self, token, action):
        with self.lock:
            entry = self.entry(token)
            if entry['action'] != action or not entry['approved']:
                raise ValueError('This exact action needs approval in Frame Control')
            del self.pending[token]  # consume before starting, including on failure


approvals = Approvals()


def validate(name, args):
    fields = {
        'launch': {'appid'}, 'install': {'id'}, 'uninstall': {'id'},
        'send_text': {'text'}, 'send_file': {'path'}, 'panel': {'id'},
        'power': {'action'}, 'keep_awake': {'action'},
    }
    if name not in fields or not isinstance(args, dict) or set(args) != fields[name]:
        raise ValueError('Unknown action or arguments')
    if any(not isinstance(v, str) or not v or len(v) > 65536 for v in args.values()):
        raise ValueError('Arguments must be nonempty strings (maximum 65536 characters)')
    if name == 'power' and args['action'] not in ('suspend', 'reboot', 'poweroff'):
        raise ValueError('Unknown power action')
    if name == 'keep_awake' and args['action'] not in ('on', 'off', 'status'):
        raise ValueError('Expected on, off or status')
    action = {'name': name, 'arguments': dict(args)}
    if name == 'send_file':
        path = Path(args['path']).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 16 * 1024**2:
            raise ValueError('Choose a regular file of at most 16 MiB')
        # Bind approval to bytes, not just a mutable filename.
        with path.open('rb') as stream:
            data = stream.read(16 * 1024**2 + 1)
        if len(data) > 16 * 1024**2:
            raise ValueError('File grew beyond 16 MiB')
        action['arguments']['path'] = str(path)
        action['sha256'] = hashlib.sha256(data).hexdigest()
        action['bytes'] = len(data)
    return action


def call(server, body):
    name, args = body.get('name'), body.get('arguments', {})
    action = validate(name, args)
    if name in ('install', 'uninstall', 'panel') and not server.FLATPAK_ID.fullmatch(args['id']):
        raise ValueError('Expected a Flatpak application ID')
    if name == 'launch' and not server.APPID.fullmatch(args['appid']):
        raise ValueError('Expected a Steam app ID')
    if name == 'keep_awake' and args['action'] == 'status':
        return keep_awake(server, 'status')
    token = body.get('confirmation')
    if not token:
        return approvals.request(action)
    approvals.consume(token, action)
    if name == 'launch':
        return server.launch(args)
    if name in ('install', 'uninstall'):
        return server.flatpak({**args, 'action': name})
    if name == 'send_text':
        return server.clipboard(args)
    if name == 'send_file':
        # Stage the reviewed bytes before the existing transfer helper reads them.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='frame-agent-') as tmp:
            source = Path(action['arguments']['path'])
            with source.open('rb') as stream:
                data = stream.read(16 * 1024**2 + 1)
            if hashlib.sha256(data).hexdigest() != action['sha256']:
                raise ValueError('File changed after approval')
            staged = Path(tmp) / source.name
            staged.write_bytes(data)
            return {'message': server.push_file(staged)}
    if name == 'power':
        if server.LOCAL:
            raise ValueError('Use the Frame Control power controls to enter the password; MCP never takes passwords')
        return server.open_thing({'what': args['action']})
    if name == 'keep_awake':
        return keep_awake(server, args['action'])
    return run_script(server, 'panel-on-frame.sh', [args['id']])


def run_script(server, name, args):
    script = server.HERE.parent / 'scripts' / name
    if not script.exists() or not shutil.which('zsh') or server.LOCAL:
        raise ValueError(name + ' requires a computer with zsh and the matching script installed')
    result = subprocess.run(['zsh', str(script), *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Script failed')
    return {'message': result.stdout.strip()}


def keep_awake(server, action):
    # PR #16 owns this interface. Never silently change timers or claim a lease.
    return run_script(server, 'keep-awake.sh', [action])
