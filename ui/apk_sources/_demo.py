"""Opt-in local store fixtures: FRAME_APK_SEARCH_DEMO=1. Never downloads."""
import json
from pathlib import Path
from apk_sources import SourceError

KIND = 'demo'
FIXTURES = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apk-search'


def sources():
    return [{'id': 'demo-' + key, 'kind': KIND, 'name': name, 'enabled': True,
             'builtin': True, 'trust': trust, 'url': 'https://example.invalid'}
            for key, name, trust in [('github', 'GitHub', 'official'), ('fdroid', 'F-Droid', 'community'),
                                     ('sidequest', 'SideQuest', 'official'), ('itch', 'itch.io', 'community')]]


def search(source, query, limit=50):
    if source['id'] == 'demo-itch':
        raise SourceError('Source temporarily unavailable')
    entries = json.loads((FIXTURES / 'store.json').read_text())
    if source['id'] == 'demo-sidequest':
        entries = [e for e in entries if e['vr']]
    if source['id'] == 'demo-fdroid':
        entries = [e for e in entries if not e['vr']]
    return [dict(e, verified=source['id'] in ('demo-github', 'demo-fdroid')) for e in entries
            if query.lower() in (e['name'] + ' ' + e['summary']).lower()][:limit]


def details(source, entry_id):
    return next(e for e in search(source, '') if e['id'] == entry_id)


def download(source, entry_id, version_code=None):
    raise SourceError('Preview sources cannot download or install apps')
