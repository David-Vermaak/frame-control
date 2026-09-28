"""Free Android VR RSS listings. Download pages stay in the publisher's UI."""
import html, re, urllib.parse, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from . import SourceError, _web

KIND = 'itch'
FEEDS = ('openxr', 'oculus-quest')


def sources():
    return [{'id': KIND if tag == 'openxr' else 'itch-quest', 'kind': KIND,
             'name': 'itch.io (' + tag + ')', 'url': 'https://itch.io', 'tag': tag,
             'builtin': True, 'enabled': True, 'trust': 'community'} for tag in FEEDS]


def _parse(source, data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SourceError('Invalid itch.io RSS feed') from e
    entries = []
    for item in root.findall('./channel/item'):
        page = item.findtext('link') or ''
        p = urllib.parse.urlsplit(page)
        if p.scheme != 'https' or not (p.hostname or '').endswith('.itch.io') or p.username or ':' in p.netloc:
            continue
        if item.findtext('price') != '$0.00' or item.findtext('platforms/android') != 'yes':
            continue
        cover = item.findtext('imageurl') or None
        if cover and not cover.startswith('https://img.itch.zone/'):
            cover = None
        updated = None
        try:
            updated = parsedate_to_datetime(item.findtext('updateDate')).date().isoformat()
        except (ValueError, TypeError, AttributeError):
            pass
        entries.append({'source': source['id'], 'id': page, 'package': None,
                        'name': item.findtext('plainTitle') or item.findtext('title') or page,
                        'summary': html.unescape(re.sub('<[^>]*>', '', item.findtext('description') or '')).strip(),
                        'icon': cover, 'images': {'icon': cover, 'banner': cover, 'screenshots': []},
                        'page': page, 'version': None, 'version_code': None, 'min_sdk': None,
                        'abis': None, 'vr': True, 'size': None, 'free': True, 'license': None,
                        'updated': updated, 'downloadable': False})
    return entries


def search(source, query, limit=50):
    limit = max(0, min(int(limit), 100))
    if not limit:
        return []
    entries = {}
    tag = source.get('tag', 'openxr')
    if tag not in FEEDS:
        raise SourceError('Unsupported itch.io feed')
    url = 'https://itch.io/games/free/platform-android/tag-' + tag + '.xml'
    for entry in _parse(source, _web.read(url, ('itch.io',))):
        entries.setdefault(entry['id'], entry)
    words = query.lower().split()
    return [e for e in entries.values() if all(w in (e['name'] + ' ' + e['summary']).lower()
                                               for w in words)][:limit]


def details(source, entry_id):
    entry = next((e for e in search(source, '', 100) if e['id'] == entry_id), None)
    if not entry:
        raise SourceError('Game is not in the current free Android VR feed; open its publisher page')
    return {**entry, 'versions': []}


def download(source, entry_id, version_code=None):
    raise SourceError('Open the itch.io publisher page to download; automated download pages are disallowed by robots rules')
