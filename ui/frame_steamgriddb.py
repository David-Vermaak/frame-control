"""Optional SteamGridDB artwork. Credentials stay on the host, never in app metadata."""
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

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


def _get(path, key):
    request = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + key,
                                                         'User-Agent': 'FrameControl/1.0'})
    # Do not carry the credential to redirects or include it in error messages.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
        data = response.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError('SteamGridDB response too large')
    result = json.loads(data)
    if not result.get('success') or not isinstance(result.get('data'), list):
        raise ValueError('SteamGridDB lookup failed')
    return result['data']


def _name(value):
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


def lookup(name):
    """Best-voted art per slot for an exact title match; unrelated games are never guessed."""
    key = api_key()
    if not key:
        return {}, []
    try:
        matches = _get('/search/autocomplete/' + urllib.parse.quote(name, safe=''), key)
        names = {_name(name), _name(re.sub(r'\s+VR$', '', name, flags=re.I))}
        game = next((g for g in matches if _name(g.get('name')) in names), None)
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
                records = _get('/' + kind + '/game/' + str(gid) + '?' + urllib.parse.urlencode(query), key)
                records = [r for r in records if r.get('url', '').startswith('https://') and not r.get('nsfw')]
                if dimensions:
                    w, h = map(int, dimensions.split('x'))
                    records = [r for r in records if (r.get('width'), r.get('height')) == (w, h)]
                records.sort(key=lambda r: (int(r.get('score') or 0), int(r.get('upvotes') or 0)), reverse=True)
                if records:
                    result[slot] = records[0]['url']
            except (OSError, ValueError, KeyError, TypeError):
                warnings.append('SteamGridDB ' + slot + ' unavailable; using source or generated art')
        return result, warnings
    except (OSError, ValueError, KeyError, TypeError):
        return {}, ['SteamGridDB unavailable; using source or generated art']
