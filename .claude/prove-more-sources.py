import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'ui'))
from apk_sources import github, itch, _web
proof = Path.cwd() / '.claude' / 'proof-cache'
proof.mkdir(exist_ok=True)
_web.cache = lambda: str(proof)
token = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, check=True).stdout.strip()
os.environ['FRAME_GITHUB_TOKEN'] = token
s = github.sources()[0]
entries = github.search(s, 'hello')
print('GitHub search:', json.dumps(entries))
r = github.download(s, entries[0]['id'])
print('GitHub download:', json.dumps(r))
p = subprocess.run(['python3', 'ui/frame_android.py', 'info', r['apk']])
print('info exit:', p.returncode)
assert p.returncode == 0
entries = itch.search(itch.sources()[0], 'off nominal')
print('itch.io search:', json.dumps(entries))
assert entries and entries[0]['downloadable'] is False
try:
    itch.download(itch.sources()[0], entries[0]['id'])
except Exception as e:
    print('itch.io download correctly refused:', e)
