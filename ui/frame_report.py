"""Report a problem from inside Frame Control. Python stdlib only.

The page's Report a problem dialog shows the diagnostics below before anything
is sent, then this sends the report privately to Frame Control's PostHog
project as a `problem_report` event: only the maintainer can read it, and
nothing is published. It is sent whatever the analytics settings are, because
the person sends it deliberately. Diagnostics are scrubbed first
(frame_telemetry.scrub); the person's own words are sent as written.

An email address goes with a report only when the person ticks "may contact me with
follow-up questions" (contact_followup). Standing choices made in Settings are
frame_contact.py's `contact_consent` events; `contacts` lists them.
"""
import os
import platform
import sys
import time
import uuid

import frame_contact
import frame_host
import frame_telemetry

KINDS = ('bug', 'idea', 'question', 'other')
TEXT_MAX = 5000     # the person's own text, in JavaScript (UTF-16) units like the page's maxlength
DIAG_MAX = 8000     # the diagnostics block
LOG_LINES = 60
ACTIVITY_LINES = 25

frame = {}  # the Frame's last known SteamOS build, set by server.status()


def u16(s):
    """Length as the website's validator counts it (JavaScript strings are UTF-16)."""
    return len(s.encode('utf-16-le')) // 2


def cut(s, n):
    """s shortened to at most n UTF-16 units, never splitting a character."""
    while u16(s) > n:
        s = s[:max(0, len(s) - max(1, (u16(s) - n) // 2))]
    return s


def _log_tail():
    """The last lines of the server log the app writes (FRAME_CONTROL_LOG), newest first."""
    path = os.environ.get('FRAME_CONTROL_LOG')
    if not path:
        return []
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    # Request lines ("GET /api/status ...") are noise; keep what went wrong.
    keep = [ln for ln in lines if ln.strip() and not ln.startswith(('GET ', 'POST '))]
    return list(reversed(keep[-LOG_LINES:]))


def diagnostics(activity=(), include_logs=False, limit=DIAG_MAX):
    """What a report includes, scrubbed and at most `limit` UTF-16 units. Always the versions
    and builds; recent activity and the server log only when asked for, since they can name
    files. Sections are filled in order of use, newest lines first, so trimming drops the oldest."""
    t = frame_telemetry.state()
    levels = ', '.join(f"{name} {'on' if on else 'off'}" for name, on in
                       (('usage', t['usage']), ('compat', t['compat']), ('error details', t['diagnostics'])))
    env = [
        f"Frame Control {frame_telemetry.app_version()}"
        f"{' (built app)' if os.environ.get('FRAME_CONTROL_PACKAGED') else ' (source checkout)'}",
        f"Computer: {frame_host.NAME} {platform.release()} {platform.machine()}, Python {'%d.%d.%d' % sys.version_info[:3]}",
        f"SteamOS: {frame.get('build') or 'unknown'} ({frame.get('version') or 'not connected since start'})",
        f"Analytics: {levels}",
        f"Report time: {time.strftime('%Y-%m-%d %H:%M %Z')}",
    ]
    out = frame_telemetry.scrub('\n'.join(env), limit=limit)
    if not include_logs:
        return cut(out, limit)
    sections = [('Recent activity (newest first):', [str(a)[:300] for a in list(activity)[:ACTIVITY_LINES] if isinstance(a, str)]),
                ('Server log (newest first):', _log_tail())]
    for title, lines in sections:
        if not lines:
            continue
        block = '\n\n' + title
        if u16(out + block) > limit:
            break
        out += block
        for line in lines:
            line = '\n' + frame_telemetry.scrub(line, 300)
            if u16(out + line) > limit:
                break
            out += line
    return out


def compose(body):
    """(title, text, diagnostics): the diagnostics exactly as the dialog previewed them (passed
    back, scrubbed again and bounded here)."""
    title = ' '.join(str(body.get('title') or '').split())
    text = str(body.get('message') or '').strip()
    if len(title) < 5:
        raise ValueError('give it a short title (at least 5 characters)')
    if len(text) < 10:
        raise ValueError('say a little more about what happened (at least 10 characters)')
    diag = body.get('diagnostics')
    diag = cut(frame_telemetry.scrub(diag, 40000), DIAG_MAX) if isinstance(diag, str) and diag.strip() else ''
    return cut(title, 120), cut(text, TEXT_MAX), diag


def send(body):
    """Send the report to PostHog. Returns {"id", "message"}; raises ReportError."""
    kind = body.get('kind') if body.get('kind') in KINDS else 'bug'
    title, text, diag = compose(body)
    followup = bool(body.get('contactFollowup'))
    contact = str(body.get('contact') or '').strip() if followup else ''
    if followup and not frame_contact.valid_email(contact):
        raise ValueError('add your email address for follow-up questions, or untick that box')
    ref = uuid.uuid4().hex[:8].upper()
    props = {**frame_telemetry.common(), 'kind': kind, 'title': title, 'message': text,
             'contact': contact, 'contact_followup': followup, 'diagnostics': diag,
             'report_id': ref, 'steamos': str(frame.get('build') or '')[:120], 'level': 'report'}
    # Its own random id: a report can carry contact details, so it isn't linked to this copy's analytics.
    event = {'event': 'problem_report', 'distinct_id': str(uuid.uuid4()), 'uuid': str(uuid.uuid4()),
             'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'properties': props}
    try:
        frame_telemetry.post([event], timeout=30)
    except frame_telemetry.SendError as e:
        raise ReportError(str(e))
    try:
        with frame_telemetry._lock:  # the lock a removal holds while wiping its address
            frame_contact.redact_removed(event)
            frame_telemetry.record_sent([event])
    except OSError:
        pass  # it was sent; failing to log it here mustn't make the person send it again
    return {'id': ref, 'message': f'Sent privately to the Frame Control developer (report {ref}).'}


class ReportError(RuntimeError):
    pass


def inbox(days=30):
    """The maintainer's recent reports from PostHog, newest first (needs the personal API key
    frame_compat_db.sync uses)."""
    import frame_compat_db
    res = frame_compat_db._posthog_query(
        "SELECT timestamp, properties.report_id, properties.kind, properties.title, properties.message, "
        "properties.contact, properties.app_version, properties.os, properties.steamos, properties.diagnostics, "
        "properties.contact_followup "
        f"FROM events WHERE event = 'problem_report' AND timestamp > now() - INTERVAL {int(days)} DAY "
        "ORDER BY timestamp DESC LIMIT 200")
    return res.get('results') or []


def _yes(v):
    return v is True or str(v).lower() in ('true', '1')


def contacts():
    """{'updates': [(email, since)], 'followup': [...]}: the addresses whose newest
    contact_consent event agrees to each, oldest first. A withdrawal, or a change to another
    address, replaces what came before, so withdrawn addresses are never listed. "Newest" is
    the highest rev from that copy (then time), so every field comes from the same event
    whatever order they arrived in or what the clocks said."""
    import frame_compat_db
    newest = "tuple(ifNull(toInt(properties.rev), 0), timestamp)"
    res = frame_compat_db._posthog_query(
        f"SELECT distinct_id, argMax(properties.email, {newest}), argMax(properties.updates, {newest}), "
        f"argMax(properties.followup, {newest}), argMax(timestamp, {newest}) FROM events "
        "WHERE event = 'contact_consent' GROUP BY distinct_id ORDER BY max(timestamp) LIMIT 100000")
    out = {'updates': [], 'followup': []}
    for row in res.get('results') or []:
        if not isinstance(row, list) or len(row) != 5:
            continue
        _, email, updates, followup, ts = row
        email = str(email or '').strip()
        if not frame_contact.valid_email(email):
            continue
        for kind, agreed in (('updates', updates), ('followup', followup)):
            if _yes(agreed):
                out[kind].append((email, str(ts or '')[:10]))
    return out


USAGE = 'usage: frame_report.py inbox [days] | contacts [updates|followup]'


def main():
    cmd, *args = sys.argv[1:] or ['inbox']
    if cmd == 'contacts':
        kinds = args[:1] or ['updates', 'followup']
        if not set(kinds) <= {'updates', 'followup'}:
            sys.exit(USAGE)
        found = contacts()
        for kind in kinds:
            print(f"== {'Release and update notices' if kind == 'updates' else 'Follow-up questions'}"
                  f" ({len(found[kind])})")
            for email, since in found[kind]:
                print(f"   {email}  (since {since})")
            print()
        return
    if cmd != 'inbox':
        sys.exit(USAGE)
    for row in inbox(*(args[:1] or [30])):
        if not isinstance(row, list) or len(row) != 11:
            continue
        ts, ref, kind, title, text, contact, version, osname, steamos, diag = (str(v or '') for v in row[:10])
        # Reports from before contact_followup existed only carried an address given for a reply.
        reply = contact and (row[10] is None or _yes(row[10]))
        print(f"== {ts[:16].replace('T', ' ')}  {ref}  [{kind}] {title}")
        print(f"   {version} on {osname}, SteamOS {steamos or 'unknown'}"
              f"{', may follow up at ' + contact if reply else ''}")
        print('   ' + text.replace('\n', '\n   '))
        if diag:
            print('   --- diagnostics\n   ' + diag.replace('\n', '\n   '))
        print()


if __name__ == '__main__':
    main()
