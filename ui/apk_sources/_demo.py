"""Opt-in local UI fixtures: FRAME_APK_SEARCH_DEMO=1. Never downloads."""
from apk_sources import SourceError

KIND = 'demo'


def sources():
    return [{'id': 'demo-' + key, 'kind': KIND, 'name': name + ' (demo)', 'enabled': True,
             'builtin': True, 'trust': trust, 'url': 'https://example.invalid'}
            for key, name, trust in [('fdroid', 'F-Droid', 'community'),
                                     ('sidequest', 'SideQuest', 'official'), ('itch', 'itch.io', 'community')]]


def search(source, query, limit=50):
    if source['id'] == 'demo-itch':
        raise SourceError('Demo: source temporarily unavailable')
    entries = [{'id': 'brush', 'package': 'org.demo.brush', 'name': 'Open Brush',
                'summary': 'Paint in VR — demonstration listing only', 'version': '2.32',
                'version_code': 232, 'min_sdk': 29, 'abis': ['arm64-v8a'], 'vr': True,
                'engine': 'Unity OpenXR', 'size': 125000000, 'updated': '2026-09-25',
                'verified': source['id'] == 'demo-fdroid', 'free': True, 'downloadable': True},
               {'id': 'radio', 'package': 'org.demo.radio', 'name': 'Pocket Radio',
                'summary': 'Listen in a flat Android panel', 'version': '1.0', 'version_code': 1,
                'min_sdk': 24, 'abis': [], 'vr': False, 'size': 3000000, 'free': True,
                'downloadable': False, 'page': 'https://example.invalid/radio'}]
    return [e for e in entries if query.lower() in e['name'].lower()][:limit]


def details(source, entry_id):
    return next(e for e in search(source, '') if e['id'] == entry_id)


def download(source, entry_id, version_code=None):
    raise SourceError('Demo sources cannot download or install apps')
