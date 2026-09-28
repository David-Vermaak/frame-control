"""Parallel APK search and source selection. No device access during searches.

Optional store metadata: images {icon, banner, screenshots}, developer,
description, popularity, open_source, requires_meta_services, frame_tested.
Only an explicit frame_tested=True produces a working-on-Frame verdict.
"""
import importlib
import inspect
import json
import os
import pkgutil
import re
import threading
import time
import unicodedata

import frame_host
from apk_sources import SourceError, SourceLimited

_lock = threading.RLock()
_running = {}
_pending = {}  # source id -> newest query waiting for the running one
_status = {}
TIMEOUT = 12


def modules():
    import apk_sources
    found, errors = [], []
    for item in pkgutil.iter_modules(apk_sources.__path__):
        if item.name == 'search' or item.name.startswith('_'):
            continue
        try:
            module = importlib.import_module('apk_sources.' + item.name)
            if getattr(module, 'KIND', None):
                found.append(module)
        except Exception as e:
            errors.append({'id': item.name, 'name': item.name, 'enabled': False,
                           'trust': 'unknown', 'status': 'error', 'error': str(e)})
    if os.environ.get('FRAME_APK_SEARCH_DEMO') == '1':
        found.append(importlib.import_module('apk_sources._demo'))
    return found, errors


def settings_path():
    return frame_host.data_dir('apk-sources', 'enabled.json')


def overrides():
    try:
        return json.loads(settings_path().read_text())
    except FileNotFoundError:
        return {}


def registry():
    result = []
    mods, errors = modules()
    with _lock:
        enabled = overrides()
    for module in mods:
        try:
            for source in module.sources():
                source = dict(source)
                source['enabled'] = enabled.get(source['id'], source.get('enabled', True))
                source.update(_status.get(source['id'], {'status': 'not searched'}))
                result.append((module, source))
        except Exception as e:
            errors.append({'id': module.KIND, 'name': module.KIND, 'enabled': False,
                           'trust': 'unknown', 'status': 'error', 'error': str(e)})
    return result, errors


def sources():
    items, errors = registry()
    return [s for _, s in items] + errors


def resolve(source_id):
    for module, source in registry()[0]:
        if source['id'] == source_id:
            return module, source
    raise SourceError('Unknown source')


def set_enabled(source_id, enabled):
    module, source = resolve(source_id)
    if hasattr(module, 'set_enabled'):
        module.set_enabled(source_id, enabled)  # never call into a source while holding _lock
    with _lock:
        values = overrides()
        values[source_id] = enabled
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(values))
        tmp.replace(path)
    return {'message': source['name'] + (' enabled' if enabled else ' disabled')}


def manage_repo(action, **kwargs):
    mods, _ = modules()
    module = next((m for m in mods if m.KIND == 'fdroid'), None)
    if module is None or not hasattr(module, action):
        raise SourceError('User repositories are not available in this build')
    return getattr(module, action)(**kwargs)


def normalise(name):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', name or '').casefold()))


def fit(entry):
    sdk, abis = entry.get('min_sdk'), entry.get('abis')
    reasons = []
    blocked = (sdk is not None and sdk > 30) or (abis is not None and bool(abis) and 'arm64-v8a' not in abis)
    if sdk is not None and sdk > 30:
        reasons.append('Needs Android API %s; Lepton supports 30' % sdk)
    if abis and 'arm64-v8a' not in abis:
        reasons.append('No arm64-v8a build')
    known = sdk is not None and abis is not None
    hints = ' '.join(str(entry.get(k) or '') for k in ('engine', 'vr_engine', 'vr_hints', 'vr_issues')).lower()
    if 'vrapi' in hints:
        reasons.append('Legacy VrApi requires a translator')
    if 'godot' in hints:
        reasons.append('Older Godot builds can crash on the missing clipboard service')
    if 'openxr' in hints:
        reasons.append('OpenXR candidate; required extensions still need checking')
    if entry.get('vr'):
        reasons.append('VR runtime compatibility is not guaranteed')
    return {'installable': False if blocked else True if known else None,
            'verdict': "Won't install" if blocked else 'Installable' if known else 'Compatibility unknown',
            'reasons': reasons}


def verdict(entry):
    compatibility = fit(entry)
    hints = ' '.join(str(entry.get(k) or '') for k in ('engine', 'vr_engine', 'vr_hints', 'vr_issues')).lower()
    if entry.get('requires_meta_services') is True:
        return {'label': "Needs Meta Quest services; won't run", 'tone': 'blocked'}
    if entry.get('min_sdk') is not None and entry['min_sdk'] > 30:
        return {'label': 'Might not work: needs a newer Android than the Frame has', 'tone': 'blocked'}
    if compatibility['installable'] is False:
        return {'label': "This version isn't made for the Frame", 'tone': 'blocked'}
    if 'vrapi' in hints:
        return {'label': "Made for older Quest headsets; won't run on the Frame", 'tone': 'blocked'}
    if entry.get('frame_tested') is True:
        return {'label': 'Works on the Frame', 'tone': 'works'}
    if compatibility['installable'] is True:
        return {'label': 'Ready to try on the Frame', 'tone': 'ready'}
    return {'label': 'Not yet checked on the Frame', 'tone': 'unknown'}


def decorate(entry):
    from apk_sources import _images
    return dict(entry, fit=fit(entry), verdict=verdict(entry), artwork=_images.artwork(entry))


def details(source_id, entry_id):
    module, source = resolve(source_id)
    return decorate(dict(module.details(source, entry_id), source=source_id,
                         source_name=source['name'], trust=source.get('trust')))


def offer_rank(entry):
    compatible = entry['fit']['installable'] is True
    return (entry.get('downloadable') is True and entry['fit']['installable'] is not False,
            entry.get('verified') is True, compatible,
            str(entry.get('updated') or '') if compatible else '',
            (entry.get('version_code') or 0) if compatible else 0,
            entry.get('trust') == 'official')


def group(entries, query='', vr=None, installable=False):
    groups = {}
    for entry in entries:
        entry = decorate(entry)
        if vr is not None and (entry.get('vr') is True) != vr:  # unknown counts as flat
            continue
        if installable and entry['fit']['installable'] is not True:
            continue
        key = ('package', entry['package']) if entry.get('package') else ('name', normalise(entry.get('name')))
        if not key[1]:
            key = ('id', entry['source'], entry['id'])
        groups.setdefault(key, []).append(entry)
    result = []
    for offers in groups.values():
        offers.sort(key=offer_rank, reverse=True)
        best = offers[0]
        result.append({'name': best.get('name'), 'package': best.get('package'),
                       'summary': best.get('summary'), 'offers': offers})
    q = normalise(query)

    def browse(a):  # a headset store: VR first, then apps with artwork, newest first
        o = a['offers'][0]
        art = o.get('images') or {}
        return (o.get('vr') is not True, not art.get('banner'), not art.get('screenshots'),
                ''.join(chr(0x10ffff - ord(c)) for c in str(o.get('updated') or '')))
    if not q:
        # Unknown fit stays in the VR-first order; only apps known not to install sink.
        result.sort(key=lambda a: (all(o['fit']['installable'] is False for o in a['offers']),) + browse(a))
        return result
    result.sort(key=lambda a: (not any(normalise(o.get('name')) == q for o in a['offers']),
                               not any(o['fit']['installable'] is True for o in a['offers']),
                               not any(normalise(o.get('name')).startswith(q) for o in a['offers']),
                               a['offers'][0].get('vr') is not True,
                               normalise(a['name'])))
    return result


def _launch(module, source, query, limit):
    """One search per source at a time; the newest different query runs next."""
    key = source['id']
    task = {'event': threading.Event(), 'query': (query, limit), 'started': time.monotonic(),
            'job': (module, source)}
    with _lock:
        old = _running.get(key)
        if old and not old['event'].is_set():
            if old['query'] == task['query']:
                return old
            queued = _pending.get(key)
            if queued and queued['query'] == task['query']:
                return queued
            _pending[key] = task
            return task
        _running[key] = task
    _start(key, task)
    return task


def _start(key, task):
    module, source = task['job']
    query, limit = task['query']

    def run():
        queued = None
        try:
            task['entries'] = [dict(e, source=key, source_name=source['name'], trust=source.get('trust'))
                               for e in module.search(source, query, limit=limit) if e.get('free') is True]
            task['stale'] = bool(getattr(module, 'stale', lambda s: False)(source))
        except Exception as e:
            task['error'] = str(e)
            task['limited'] = isinstance(e, SourceLimited)
        finally:
            with _lock:
                queued = _pending.pop(key, None)
                if queued:
                    _running[key] = queued
            task['event'].set()
        if queued:
            _start(key, queued)
    threading.Thread(target=run, daemon=True).start()


def search(query='', vr=None, source=None, installable=False, timeout=TIMEOUT, limit=50):
    items, errors = registry()
    if source and source not in [s['id'] for _, s in items]:
        raise SourceError('Unknown source')
    chosen = [(m, s) for m, s in items if s['enabled'] and (not source or s['id'] == source)]
    # Page-only sources (SideQuest) can't be searched; offer a link to browse them instead.
    elsewhere = [{'name': s['name'], 'url': s['url']} for m, s in chosen if s.get('page_only')]
    tasks = [(s, _launch(m, s, query, limit)) for m, s in chosen if not s.get('page_only')]
    entries, statuses = [], list(errors)
    for s, task in tasks:
        status = {'id': s['id'], 'name': s['name']}
        if not task['event'].wait(max(0, task['started'] + timeout - time.monotonic())):
            # Still working (e.g. first download of a large index); it keeps going and fills the cache.
            status.update(status='loading')
        elif 'error' in task:
            status.update(status='limited' if task.get('limited') else 'error', error=task['error'])
        else:
            status.update(status='ok', stale=task['stale'])
            entries.extend(task['entries'])
        statuses.append(status)
        with _lock:
            _status[s['id']] = {k: v for k, v in status.items() if k not in ('id', 'name')}
    return {'apps': group(entries, query, vr, installable), 'sources': statuses, 'elsewhere': elsewhere}


def warm():
    """Start every enabled source's index download in the background (server start, new repo)."""
    from apk_sources import _web
    _web.prune()
    items, _ = registry()
    for m, s in items:
        if s['enabled'] and not s.get('page_only'):
            _launch(m, s, '', 50)  # same as the first browse, so that search reuses it


def install(source_id, entry_id, version_code=None, progress=None):
    import frame_android
    module, source = resolve(source_id)
    if not source['enabled']:
        raise SourceError('This source is disabled')
    entry = module.details(source, entry_id)
    if entry.get('free') is not True or entry.get('downloadable') is not True:
        raise SourceError('This app must be obtained from its developer page')
    if progress:
        progress('Downloading', None)
    downloaded = module.download(source, entry_id, version_code=version_code)
    obb = downloaded.get('obb') or entry.get('obb') or []
    if obb and not hasattr(frame_android, 'install_obb'):
        raise SourceError('This app needs OBB data; this build cannot install it yet')
    kwargs = {'name': entry.get('name'), 'icon_png': downloaded.get('icon_png') or entry.get('icon_png'),
              'source': source['name']}
    if 'artwork' in inspect.signature(frame_android.install).parameters:
        kwargs['artwork'] = downloaded.get('artwork') or entry.get('artwork')
    if progress:
        progress('Installing', None)
    result = frame_android.install(downloaded['apk'], **kwargs)
    if obb:
        frame_android.install_obb(result['package'], obb)
    return result
