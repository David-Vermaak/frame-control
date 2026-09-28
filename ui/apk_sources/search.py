"""Parallel APK search and source selection. No device access during searches."""
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
from apk_sources import SourceError

_lock = threading.RLock()
_running = {}
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
    with _lock:
        if hasattr(module, 'set_enabled'):
            module.set_enabled(source_id, enabled)
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
        entry = dict(entry, fit=fit(entry))
        if vr is not None and entry.get('vr') is not vr:
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
    result.sort(key=lambda a: (not any(normalise(o.get('name')) == q for o in a['offers']),
                               not any(o['fit']['installable'] is True for o in a['offers']),
                               not any(normalise(o.get('name')).startswith(q) for o in a['offers']),
                               normalise(a['name'])))
    return result


def _launch(module, source, query, limit):
    key = source['id']
    with _lock:
        old = _running.get(key)
        if old and not old['event'].is_set():
            return old if old['query'] == query else None
        task = {'event': threading.Event(), 'query': query, 'started': time.monotonic()}
        _running[key] = task
    def run():
        try:
            task['entries'] = [dict(e, source=key, source_name=source['name'], trust=source.get('trust'))
                               for e in module.search(source, query, limit=limit) if e.get('free') is True]
        except Exception as e:
            task['error'] = str(e)
        finally:
            task['event'].set()
    threading.Thread(target=run, daemon=True).start()
    return task


def search(query='', vr=None, source=None, installable=False, timeout=TIMEOUT, limit=50):
    items, errors = registry()
    if source and source not in [s['id'] for _, s in items]:
        raise SourceError('Unknown source')
    tasks = [(s, _launch(m, s, query, limit)) for m, s in items
             if s['enabled'] and (not source or s['id'] == source)]
    entries, statuses = [], list(errors)
    for s, task in tasks:
        status = {'id': s['id'], 'name': s['name']}
        if task is None or not task['event'].wait(max(0, task['started'] + timeout - time.monotonic())):
            status.update(status='timed out')
        elif 'error' in task:
            status.update(status='error', error=task['error'])
        else:
            status.update(status='ok')
            entries.extend(task['entries'])
        statuses.append(status)
        with _lock:
            _status[s['id']] = {k: v for k, v in status.items() if k not in ('id', 'name')}
    return {'apps': group(entries, query, vr, installable), 'sources': statuses}


def install(source_id, entry_id, version_code=None):
    import frame_android
    module, source = resolve(source_id)
    if not source['enabled']:
        raise SourceError('This source is disabled')
    entry = module.details(source, entry_id)
    if entry.get('free') is not True or entry.get('downloadable') is not True:
        raise SourceError('This app must be obtained from its developer page')
    downloaded = module.download(source, entry_id, version_code=version_code)
    obb = downloaded.get('obb') or entry.get('obb') or []
    if obb and not hasattr(frame_android, 'install_obb'):
        raise SourceError('This app needs OBB data; this build cannot install it yet')
    kwargs = {'name': entry.get('name'), 'icon_png': downloaded.get('icon_png') or entry.get('icon_png'),
              'source': source['name']}
    if 'artwork' in inspect.signature(frame_android.install).parameters:
        kwargs['artwork'] = downloaded.get('artwork') or entry.get('artwork')
    result = frame_android.install(downloaded['apk'], **kwargs)
    if obb:
        frame_android.install_obb(result['package'], obb)
    return result
