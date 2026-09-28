"""Report a problem from inside Frame Control. Python stdlib only.

The page's Report a problem dialog shows the diagnostics below before anything
is sent, then this sends the report to the website's feedback API
(site/functions/api/feedback.js), which files it as a GitHub issue. The user
needs no GitHub account. Everything collected is scrubbed first
(frame_telemetry.scrub), since the issue is public.
"""
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request

import frame_host
import frame_telemetry

FEEDBACK_URL = os.environ.get('FRAME_CONTROL_FEEDBACK_URL', 'https://frame-control.pages.dev/api/feedback')
ISSUES_URL = 'https://github.com/saphid/frame-control/issues/new'
KINDS = ('bug', 'idea', 'question', 'other')
MESSAGE_MAX = 5000  # the feedback API's limit, in JavaScript (UTF-16) units
TEXT_MAX = 3500     # the person's own text
DIAG_MAX = 1300     # the diagnostics block, so text + diagnostics always fit MESSAGE_MAX
LOG_LINES = 40
ACTIVITY_LINES = 25
HEAD = '\n\n---\nDiagnostics from Frame Control (personal details removed):\n```\n'
TAIL = '\n```'

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
    """(title, message) for the feedback API: the person's text, then the diagnostics exactly as
    the dialog previewed them (passed back, scrubbed again and bounded here)."""
    title = ' '.join(str(body.get('title') or '').split())
    text = str(body.get('message') or '').strip()
    if len(title) < 5:
        raise ValueError('give it a short title (at least 5 characters)')
    if len(text) < 10:
        raise ValueError('say a little more about what happened (at least 10 characters)')
    title, text = cut(title, 120), cut(text, TEXT_MAX)
    diag = body.get('diagnostics')
    if not isinstance(diag, str) or not diag.strip():
        return title, text
    diag = cut(frame_telemetry.scrub(diag, 20000), DIAG_MAX)
    return title, f'{text}{HEAD}{diag}{TAIL}'


def send(body):
    """File the report. Returns {"number", "url"} of the new issue; raises ReportError."""
    kind = body.get('kind') if body.get('kind') in KINDS else 'bug'
    title, message = compose(body)
    payload = {'kind': kind, 'title': title, 'message': message, 'website': '',
               'github': str(body.get('github') or '').strip().lstrip('@')[:40],
               'version': frame_telemetry.app_version(), 'os': f'{frame_host.NAME} {platform.machine()}',
               'steamos': str(frame.get('build') or '')[:120],
               # How long the dialog was open; the API treats anything under 3 s as a script.
               'elapsed': max(0, int(body.get('elapsed') or 0))}
    req = urllib.request.Request(FEEDBACK_URL, data=json.dumps(payload).encode(), method='POST',
                                 headers={'content-type': 'application/json',
                                          'user-agent': f'FrameControl/{frame_telemetry.app_version()}'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b'{}').get('error')
        except ValueError:
            why = None
        e.close()
        raise ReportError(why or f'the feedback service said HTTP {e.code}')
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ReportError(f"couldn't reach the feedback service: {e}")
    if not res.get('url'):
        raise ReportError("the feedback service didn't file it; try again in a moment")
    frame_telemetry.capture('problem_reported', {'kind': kind, 'with_diagnostics': bool(body.get('diagnostics'))})
    return {'number': res.get('number'), 'url': res['url'], 'message': f"Sent. It's issue #{res.get('number')} on GitHub."}


class ReportError(RuntimeError):
    pass
