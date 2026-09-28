"""Frame Control's compatibility database: a private Lakebed capsule
(compat-db/, https://frame-compat.lakebed.app) that only this app can read or
write, using a key from $FRAME_CONTROL_KEY or the macOS Keychain (service
frame-control-compat-db, account app-key). Without one (anyone but the
maintainer), reports stay local.

New reports go to a local outbox first and are sent from there, so nothing is
lost offline. A mirror of every report is kept for offline reads. Both live in
frame_host.data_dir('compat-db'). Python stdlib only.

CLI: python3 ui/frame_compat_db.py {count|export FILE|import FILE|flush}
(import restores a backup; reports already in the database are skipped.)
"""
import json, os, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid

import frame_host

URL = os.environ.get('FRAME_COMPAT_DB_URL', 'https://frame-compat.lakebed.app')
KEYCHAIN = ('frame-control-compat-db', 'app-key')
STATE = str(frame_host.data_dir('compat-db'))
OUTBOX = os.path.join(STATE, 'compat-outbox.jsonl')
MIRROR = os.path.join(STATE, 'compat-mirror.json')
FIELDS = ('package', 'version', 'result', 'rating', 'notes', 'via', 'date', 'steamos', 'lepton', 'runtime',
          'label', 'source')
TTL = 60  # seconds a fetched copy is reused
_lock = threading.Lock()
_load_lock = threading.Lock()  # refreshing the cached reports (load); separate from _lock, which flush takes
_mem = {'at': 0, 'reports': None, 'source': None}


class DBError(RuntimeError):
    pass


def key():
    k = os.environ.get('FRAME_CONTROL_KEY')
    if k:
        return k
    p = None
    if frame_host.MAC:
        p = subprocess.run(['security', 'find-generic-password', '-s', KEYCHAIN[0], '-a', KEYCHAIN[1], '-w'],
                           capture_output=True, text=True)
    if p is None or p.returncode != 0 or not p.stdout.strip():
        raise DBError('No compatibility-database key (set FRAME_CONTROL_KEY, or on macOS the Keychain '
                      f'item service {KEYCHAIN[0]}, account {KEYCHAIN[1]})')
    return p.stdout.strip()


def shared():
    """Whether reports reach the shared database. Without the key (anyone but the
    maintainer), reports stay in this computer's outbox and ratings come from the catalogue."""
    try:
        key()
        return True
    except DBError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would copy the key header to the new host."""
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _request(path, body=None, timeout=20):
    req = urllib.request.Request(URL + path, method='POST' if body is not None else 'GET',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'x-frame-control-key': key(), 'content-type': 'application/json',
                                          'user-agent': 'FrameControl/1'})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise DBError(f'compatibility database said HTTP {e.code}')
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise DBError(f"can't reach the compatibility database: {e}")


def _from_row(row):
    r = {k: row.get(k) for k in FIELDS if k != 'date'}
    r['date'] = row.get('reportedAt')
    r['id'] = row.get('clientId') or row.get('id')
    return r


def fetch_all():
    """Every report from the database (paged), deduplicated."""
    seen, out, since = set(), [], ''
    for _ in range(200):
        page = _request('/v1/reports?since=' + urllib.parse.quote(since))
        for row in page.get('reports', []):
            if row.get('id') and row['id'] not in seen:
                seen.add(row['id'])
                out.append(_from_row(row))
        if not page.get('next') or page['next'] == since:
            break
        since = page['next']
    return out


RESULTS = ('runs', 'crashes', 'install_failed', 'instance_failed')
RATINGS = ('works', 'issues', 'broken')


def problem(r):
    """Why the server would reject this report, or None. Mirrors compat-db/server/index.ts."""
    if not isinstance(r, dict):
        return 'not an object'
    for k in ('package', 'id', 'date'):
        if not r.get(k) or not isinstance(r[k], str):
            return f'missing {k}'
    if r.get('result') not in (None, '', *RESULTS):
        return f"bad result {r['result']!r}"
    if r.get('rating') not in (None, '', *RATINGS):
        return f"bad rating {r['rating']!r}"
    return None


def _quarantine(lines, why):
    """Keep what can't be sent, with the reason, instead of dropping it."""
    os.makedirs(STATE, exist_ok=True)
    with open(OUTBOX + '.rejected', 'a') as f:
        for line in lines:
            f.write(json.dumps({'why': why, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'line': line}) + '\n')


def _outbox():
    """Queued reports. Unreadable or invalid lines move to the .rejected file."""
    if not os.path.exists(OUTBOX):
        return []
    good, bad = [], []
    with open(OUTBOX) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                bad.append((line.rstrip('\n'), 'unreadable JSON'))
                continue
            why = problem(r)
            (bad.append((line.rstrip('\n'), why)) if why else good.append(r))
    if bad:
        for line, why in bad:
            _quarantine([line], why)
        _write_outbox(good)
    return good


def _write_outbox(rows):
    with open(OUTBOX + '.tmp', 'w') as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    os.replace(OUTBOX + '.tmp', OUTBOX)


def flush():
    """Send queued reports. Sent ones leave the outbox; ones the server rejects go to
    the .rejected file; on a network error the rest stay queued. Returns how many are left."""
    with _lock:
        pending = _outbox()
        while pending:
            batch = pending[:100]
            res = _request('/v1/reports', {'reports': [{**r, 'clientId': r['id']} for r in batch]})
            rejected = set(res.get('rejected') or [])
            if rejected:
                _quarantine([json.dumps(r) for r in batch if r['id'] in rejected], 'rejected by the server')
            pending = pending[100:]
            _write_outbox(pending)
        return len(pending)


def _read_mirror():
    try:
        with open(MIRROR) as f:
            return [r for r in json.load(f).get('reports', []) if isinstance(r, dict) and r.get('package')]
    except (OSError, ValueError, AttributeError):
        return []


def _save_mirror(reports):
    """Keep a copy for offline use. Failing to write it mustn't fail the read."""
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(MIRROR + '.tmp', 'w') as f:
            json.dump({'fetched': time.strftime('%Y-%m-%dT%H:%M:%S'), 'reports': reports}, f)
        os.replace(MIRROR + '.tmp', MIRROR)
    except OSError:
        pass


def load():
    """All reports: the database (cached for TTL s), else the offline mirror; plus unsent ones."""
    # The page asks for the catalogue and the reports at once: one refresh at a
    # time, so they share a fetch and never write the mirror's .tmp together.
    with _load_lock:
        now = time.time()
        if _mem['reports'] is None or now - _mem['at'] > TTL:
            try:
                if not shared():
                    raise DBError('no key')
                try:
                    flush()
                except Exception:
                    pass  # sending can fail for any reason; reading must still work
                reports, source = fetch_all(), 'lakebed'
                _save_mirror(reports)
            except DBError:
                reports, source = _read_mirror(), 'mirror'
            _mem.update(at=now, reports=reports, source=source)
    sent = {r.get('id') for r in _mem['reports']}
    return _mem['reports'] + [r for r in _outbox() if r['id'] not in sent]


def add(report):
    """Validate, queue, then try to send. Never raises once the report is queued."""
    r = {k: report.get(k) for k in FIELDS}
    r['id'] = report.get('id') or str(uuid.uuid4())
    why = problem(r)
    if why:
        raise ValueError(f'report not saved: {why}')
    os.makedirs(STATE, exist_ok=True)
    with _lock, open(OUTBOX, 'a') as f:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
    try:
        if shared():
            flush()
            _mem['at'] = 0  # refetch on next load
    except Exception:
        pass  # stays queued; load() shows it and a later call sends it
    return r


def main():
    cmd, *args = sys.argv[1:] or ['count']
    try:
        if cmd == 'count':
            print(len(fetch_all()))
        elif cmd == 'export':
            reports = fetch_all()
            with open(args[0], 'w') as f:
                json.dump({'exported': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'source': URL,
                           'count': len(reports), 'reports': reports}, f, indent=1)
            print(f'{len(reports)} reports -> {args[0]}')
        elif cmd == 'import':
            with open(args[0]) as f:
                backup = json.load(f)
            rows = [{**{k: r.get(k) for k in FIELDS}, 'id': r.get('id')} for r in backup['reports']]
            bad = [(r, problem(r)) for r in rows if problem(r)]
            ok = [r for r in rows if not problem(r)]
            for r, why in bad:
                print(f"skipped {r.get('package')!r}: {why}", file=sys.stderr)
            os.makedirs(STATE, exist_ok=True)
            with _lock, open(OUTBOX, 'a') as f:
                f.writelines(json.dumps(r) + '\n' for r in ok)
            left = flush()
            print(f'{len(ok)} reports sent, {len(bad)} invalid skipped, {left} still queued; '
                  'reports already in the database were not duplicated')
        elif cmd == 'flush':
            print(f'{flush()} still queued')
        else:
            sys.exit(__doc__)
    except DBError as e:
        sys.exit(f'error: {e}')


if __name__ == '__main__':
    main()
