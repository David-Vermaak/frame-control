"""An email address the person chooses to leave, and what it may be used for. Python stdlib only.

Two separate opt-in choices, both off until ticked:

- updates: occasional notices about Frame Control releases and updates
- followup: the maintainer may ask follow-up questions, mainly about problem reports

The address and the choices are kept on this computer (frame_host.data_dir('contact')) and
sent privately to Frame Control's PostHog project as a `contact_consent` event, the same way
as problem reports (frame_report.py), so only the maintainer can read them. Every change
sends a new event under this copy's own random contact id (not the analytics id), numbered
by `rev`, and the highest rev for an id is the one that counts, whatever the clocks say:
removing the address sends a withdrawal with no address in it, and wipes the address from
the local log of what was sent. The maintainer lists who agreed to what with
`python3 ui/frame_report.py contacts`. Nothing here sends email.

A change that can't be sent (offline) waits in the state file and is retried in the
background, so a withdrawal is never lost. The page's one-time prompt is remembered here
too: once it has been shown or dismissed it never comes back.
"""
import json
import os
import re
import threading
import time
import uuid

import frame_host
import frame_telemetry

STATE = frame_host.data_dir('contact')
FILE = STATE / 'contact.json'
EMAIL_MAX = 254
EMAIL_RE = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s.]+')
PROMPTS = ('new', 'shown', 'dismissed', 'answered')
RETRY_EVERY = 600

_lock = threading.RLock()
_send_lock = threading.Lock()  # one send at a time, so events reach PostHog in rev order
_removed = {}  # address (lower case) -> when it was removed, for reports still being sent then
_wake = threading.Event()
_retrier = None


def _defaults():
    return {'id': str(uuid.uuid4()), 'email': '', 'updates': False, 'followup': False,
            'prompt': 'new', 'pending': None, 'rev': 0}


def load():
    with _lock:
        s = _defaults()
        try:
            with open(FILE) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                s.update({k: v for k, v in saved.items() if k in s})
        except (OSError, ValueError):
            pass
        return s


def _save(s):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s, indent=1))
    os.replace(tmp, FILE)


def valid_email(email):
    return len(email) <= EMAIL_MAX and bool(EMAIL_RE.fullmatch(email))


def flag(body, key):
    """A consent choice: true only when it really is true (not "false" or 1), left out is no."""
    v = body.get(key)
    if v is not None and not isinstance(v, bool):
        raise ValueError(f'{key} must be true or false')
    return v is True


def contact_id():
    """This copy's contact id, kept from now on. A report with follow-up consent carries it, so
    removing the address later takes back the follow-up permission given with the report too."""
    with _lock:
        s = load()
        if not FILE.exists():
            _save(s)
        return s['id']


def state():
    """What the page shows. showPrompt: the one-time prompt hasn't been shown or answered yet,
    and the Frame has connected at least once (setup worked), so it never greets a new install."""
    s = load()
    set_up = bool(frame_telemetry.settings().get('frames_seen'))
    return {'email': s['email'], 'updates': s['updates'], 'followup': s['followup'],
            'waiting': s['pending'] is not None, 'showPrompt': s['prompt'] == 'new' and set_up}


def _event(s):
    email = s['email'] if s['updates'] or s['followup'] else ''
    return {'event': 'contact_consent', 'distinct_id': s['id'], 'uuid': str(uuid.uuid4()),
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'properties': {**frame_telemetry.common(), 'email': email, 'updates': bool(email and s['updates']),
                           'followup': bool(email and s['followup']),
                           'action': 'set' if email else 'withdraw', 'rev': s['rev'], 'level': 'contact'}}


def _send_pending(block=True):
    """Send what's waiting, including changes made while sending. True if nothing is left
    waiting. Without block, a send already under way is left to pick up the newest change."""
    if not _send_lock.acquire(blocking=block):
        return False
    try:
        while True:
            with _lock:
                event = load()['pending']
            if event is None:
                break
            try:
                frame_telemetry.post([event], timeout=30)
            except frame_telemetry.SendError:
                return False
            _sent(event)
    finally:
        _send_lock.release()
    # A change saved just as this finished found the lock still held and left it to us.
    with _lock:
        left = load()['pending'] is not None
    return _send_pending(block=False) if left else True


def _sent(event):
    with _lock:
        s = load()
        if s['pending'] and s['pending'].get('uuid') == event['uuid']:  # not replaced meanwhile
            s['pending'] = None
            _save(s)
        # A withdrawal, or the address still in use: not an old one removed while this was on its way.
        if event['properties']['email'] in ('', s['email']):
            try:
                frame_telemetry.record_sent([event])
            except OSError:
                pass


def _forget_locally(email):
    """Take a removed address out of the log of what was sent (contact events and reports)."""
    with frame_telemetry._lock:
        _removed[email.lower()] = time.time()
        rows = frame_telemetry._read_lines(frame_telemetry.SENT)
        hit = False
        for e in rows:
            p = e.get('properties') or {}
            for k in ('email', 'contact'):
                if p.get(k) and str(p[k]).strip().lower() == email.lower():
                    p[k], hit = '<removed>', True
        if hit:
            frame_telemetry._write_lines(frame_telemetry.SENT, rows)


def redact_removed(event, started):
    """Before logging a report (started at time.time() `started`) whose address was removed
    while it was being sent: take the address out. Call with frame_telemetry._lock held, so a
    removal can't slip between this and the log."""
    p = event.get('properties') or {}
    removed_at = _removed.get(str(p.get('contact') or '').strip().lower())
    if removed_at is not None and started <= removed_at:
        p['contact'] = '<removed>'


def save(body):
    """Set, change or remove the address and the two choices. An address needs at least one
    choice ticked; an empty address (or neither ticked) removes it and withdraws both."""
    email = str(body.get('email') or '').strip()
    updates, followup = flag(body, 'updates'), flag(body, 'followup')
    if email and not valid_email(email):
        raise ValueError("that doesn't look like an email address")
    if email and not (updates or followup):
        raise ValueError('tick what the address may be used for, or remove it')
    if not email:
        updates = followup = False
    with _lock:
        s = load()
        old = s['email']
        changed = (email, updates, followup) != (s['email'], s['updates'], s['followup'])
        s.update(email=email, updates=updates, followup=followup)
        if body.get('fromPrompt') or email:
            s['prompt'] = 'answered'
        if changed:
            # Only the newest choice matters, so it replaces anything still waiting. A withdrawal
            # is sent even for an address still waiting here: its send may already be under way.
            s['rev'] += 1
            s['pending'] = _event(s)
        _save(s)
        if old and old.lower() != email.lower():
            try:
                _forget_locally(old)
            except OSError:
                pass
    if changed and not _send_pending(block=False):
        _wake.set()  # offline, or a send under way that will take this change with it
    return state()


def prompt(body):
    """The one-time prompt was shown, or dismissed with No thanks. Either way it stays gone."""
    action = body.get('prompt')
    if action not in ('shown', 'dismissed'):
        raise ValueError('unknown prompt action')
    with _lock:
        s = load()
        if s['prompt'] in ('new', 'shown'):
            s['prompt'] = action
            _save(s)
    return state()


def start():
    """Retry a change that couldn't be sent, from now on in the background."""
    global _retrier
    if _retrier:
        return

    def loop():
        while True:
            try:
                _send_pending()
            except Exception:
                pass
            _wake.wait(RETRY_EVERY)
            _wake.clear()

    _retrier = threading.Thread(target=loop, name='contact', daemon=True)
    _retrier.start()
