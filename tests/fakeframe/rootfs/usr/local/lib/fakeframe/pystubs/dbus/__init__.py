"""Stand-in for dbus-python, just enough for Valve's steamos-devkit-service.

The service advertises _steamos-devkit._tcp through systemd-resolved's
RegisterService over the system bus (on the Frame, `frame` answered mDNS,
docs/ssh.md, 2026-09-26). A container has no system bus or resolved, so
this records the call in the fake Frame's log instead.
"""
import sys

from . import exceptions  # noqa: F401

sys.path.insert(0, '/usr/local/lib/fakeframe')
import fakeframe_state as fs  # noqa: E402


class Array(list):
    def __init__(self, items=(), signature=None):
        super().__init__(items)


class Dictionary(dict):
    def __init__(self, items=(), signature=None):
        super().__init__(items)


def UInt16(v):
    return int(v)


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        if all(isinstance(x, int) for x in v):
            return bytes(v).decode('utf-8', 'replace')
        return [_plain(x) for x in v]
    return v


class _Object:
    def get_dbus_method(self, name, interface=None):
        def call(*args):
            fs.log('resolve1', method=name, args=_plain(list(args)))
            return f'/org/freedesktop/resolve1/dnssd/{args[0]}' if name == 'RegisterService' else None
        return call


class SystemBus:
    def get_object(self, bus_name, path):
        return _Object()
