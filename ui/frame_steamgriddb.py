"""Optional SteamGridDB artwork. Credentials stay on the host, never in app metadata."""
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse

import frame_host

API = 'https://www.steamgriddb.com/api/v2'
MAX_JSON = 2 * 1024 * 1024


def settings_path():
    return frame_host.data_dir('artwork-settings.json')


def api_key():
    env = os.environ.get('STEAMGRIDDB_API_KEY') or os.environ.get('FRAME_STEAMGRIDDB_API_KEY')
    if env:
        return env.strip()
    try:
        return str(json.loads(settings_path().read_text()).get('steamgriddb_api_key') or '')
    except (OSError, ValueError, AttributeError):
        return ''


def settings():
    return {'steamgriddb_configured': bool(api_key()),
            'environment': bool(os.environ.get('STEAMGRIDDB_API_KEY') or os.environ.get('FRAME_STEAMGRIDDB_API_KEY'))}


def save_settings(body):
    key = body.get('steamgriddb_api_key')
    if not isinstance(key, str) or len(key) > 200 or (key and not re.fullmatch(r'[A-Za-z0-9_-]+', key)):
        raise ValueError('enter a valid SteamGridDB API key, or an empty value to remove it')
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.artwork-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'steamgriddb_api_key': key}, f)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
    return settings()


def _get(path, key, deadline=None):
    from apk_sources import _images
    # No redirects: the credential never goes anywhere but the API. Errors never contain it.
    data = _images.get(API + path, {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, redirects=0,
                       deadline=time.monotonic() + 12 if deadline is None else min(deadline, time.monotonic() + 12),
                       limit=MAX_JSON)
    result = json.loads(data)
    if not isinstance(result, dict) or not result.get('success') or not isinstance(result.get('data'), list):
        raise ValueError('SteamGridDB lookup failed')
    return result['data']


def _name(value):
    # Letters and digits of any script, so a CJK title never normalises to ''.
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value or '')).casefold() if c.isalnum())


def lookup(name, deadline=None):
    """Best-voted art per slot for an exact title match; unrelated games are never guessed."""
    key = api_key()
    if not key:
        return {}, []
    try:
        names = {_name(name), _name(re.sub(r'\s+VR$', '', name, flags=re.I))} - {''}
        if not names:
            return {}, []
        matches = _get('/search/autocomplete/' + urllib.parse.quote(name, safe=''), key, deadline)
        game = next((g for g in matches if isinstance(g, dict) and _name(g.get('name')) in names), None)
        if not game:
            return {}, []
        gid = int(game['id'])
        result, warnings = {}, []
        for slot, kind, dimensions in [('grid', 'grids', '600x900'), ('wide', 'grids', '920x430'),
                                       ('hero', 'heroes', ''), ('logo', 'logos', ''), ('icon', 'icons', '')]:
            try:
                query = {'types': 'static', 'nsfw': 'false', 'humor': 'false', 'mimes': 'image/png,image/jpeg'}
                if dimensions:
                    query['dimensions'] = dimensions
                records = _get('/' + kind + '/game/' + str(gid) + '?' + urllib.parse.urlencode(query), key, deadline)
                records = [r for r in records if isinstance(r, dict) and str(r.get('url', '')).startswith('https://')
                           and not r.get('nsfw')]
                if dimensions:
                    w, h = map(int, dimensions.split('x'))
                    records = [r for r in records if (r.get('width'), r.get('height')) == (w, h)]
                records.sort(key=lambda r: (int(r.get('score') or 0), int(r.get('upvotes') or 0)), reverse=True)
                if records:
                    result[slot] = records[0]['url']
            except Exception:  # HTTPException, odd JSON: this slot falls back
                warnings.append('SteamGridDB ' + slot + ' unavailable; using source or generated art')
        return result, warnings
    except Exception:
        return {}, ['SteamGridDB unavailable; using source or generated art']
