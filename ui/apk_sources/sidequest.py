"""SideQuest page links only: its terms do not authorise third-party scraping.

No API calls, cached listings or automated downloads. See docs/sidequest.md.
"""
from . import SourceError

KIND = 'sidequest'
URL = 'https://sidequestvr.com'
REASON = ('SideQuest is page-only: its terms restrict scraping and unauthorised '
          'access. Browse and download with SideQuest, then import a developer-provided APK.')


def sources():
    return [{'id': KIND, 'kind': KIND, 'name': 'SideQuest', 'url': URL,
             'builtin': True, 'enabled': True, 'trust': 'community',
             'page_only': True, 'reason': REASON}]


def search(source, query, limit=50):
    # Do not invent catalogue results or interpret a query as a verified free app.
    return []


def details(source, entry_id):
    entry_id = str(entry_id)
    if not entry_id.isascii() or not entry_id.isdecimal() or len(entry_id) > 12:
        raise SourceError('SideQuest listing ids must be numeric')
    return {'source': source['id'], 'id': entry_id, 'package': None,
            'name': 'SideQuest listing ' + entry_id, 'summary': REASON,
            'icon': None, 'page': URL + '/app/' + entry_id, 'version': None,
            'version_code': None, 'min_sdk': None, 'abis': None, 'vr': None,
            'size': None, 'free': None, 'license': None, 'updated': None,
            'downloadable': False, 'versions': [], 'tags': [], 'headsets': [],
            'images': {'icon': None, 'banner': None, 'screenshots': []}}


def download(source, entry_id, version_code=None):
    entry = details(source, entry_id)
    raise SourceError(REASON + ' ' + entry['page'])
