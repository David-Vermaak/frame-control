"""Shared state of the fake Frame: one JSON file plus a log of stub calls.

Every fake part (the supervisor, fakesteam, the command stubs) reads and
writes /var/lib/fakeframe/state.json through update(), which holds an
exclusive flock, so separate processes never lose each other's changes.
Tests read the file over SSH (`fakeframe-ctl state`) or the control port.
"""
import contextlib
import fcntl
import json
import os
import time

DIR = '/var/lib/fakeframe'
STATE = os.path.join(DIR, 'state.json')
CALLS = os.path.join(DIR, 'calls.jsonl')
LOCK = os.path.join(DIR, 'state.lock')

HOME = '/home/steamos'
STEAM_ROOT = HOME + '/.local/share/Steam'
DEVKIT_GAMES = HOME + '/devkit-game'
LEPTON = STEAM_ROOT + '/steamapps/common/Lepton/lepton'

# Compat tools and the Steam app ids of their depots. 4183110 is the id Steam
# printed on the Frame for "Steam Linux Runtime 4.0" (docs/sideloading.md,
# BUILD_ID 20260922.6101926); Lepton Development is 3056000 (docs/apks.md).
# The others are placeholders: nothing in Frame Control reads them.
RUNTIME_APPIDS = {'proton-experimental': 1493710, 'proton-stable': 3658110,
                  'SteamLinuxRuntime_4-arm64': 4183120, 'SteamLinuxRuntime_4': 4183110,
                  'lepton': 3056000}
RUNTIME_NAMES = {'proton-experimental': 'Proton Experimental', 'proton-stable': 'Proton 11.0',
                 'SteamLinuxRuntime_4-arm64': 'Steam Linux Runtime 4.0 (arm64)',
                 'SteamLinuxRuntime_4': 'Steam Linux Runtime 4.0', 'lepton': 'Lepton Development'}


def default_state():
    return {
        'switches': {
            'pairing_mode': False,      # Steam Settings > Developer > Pair new host open or not
            'pairing_answer': 'approve',  # approve | deny | timeout
            'steam': True,              # Steam client running
            'asleep': False,            # headset asleep: ports 22 and 32000 accept but never answer
            'sshd': True,
            'devkit_service': True,
            'disk_full': False,
        },
        # Which compat tools are installed. On the Frame the x86-64 Steam Linux
        # Runtime 4.0 wasn't, and a devkit title didn't install it (docs/sideloading.md,
        # 2026-09-26, BUILD_ID 20260922.6101926).
        'runtimes': {'proton-experimental': True, 'proton-stable': True,
                     'SteamLinuxRuntime_4-arm64': True, 'SteamLinuxRuntime_4': False, 'lepton': True},
        # /sys/class/power_supply values, in the units the kernel uses (µV, µA, tenths
        # of °C), as frame_status.py reads them (paths verified on build 20260922; its
        # docstring gives the charger type 'C PD [PD_PPS]' at 20 W as seen on the Frame).
        'battery': {'capacity': 76, 'status': 'Charging', 'voltage_now': 7700000, 'current_now': 1250000,
                    'time_to_full_now': 2520, 'temp': 312, 'health': 'Good',
                    'charger': {'online': 1, 'usb_type': 'C PD [PD_PPS]', 'voltage_now': 9000000,
                                'current_now': 2220000}},
        'thermal_mc': [41500, 38250],   # thermal_zone*/temp, millidegrees C
        'volume': {'level': 0.4, 'muted': False},
        'flatpaks': [],
        'clipboard': [],
        'steam': {
            'country': 'AU',            # GetIPCountry() answered "AU" (docs/steam-games.md)
            'next_shortcut': 0,
            # A few owned games. Balatro went straight to install state 14 and
            # Broforce stopped at 7 on the Frame (docs/steam-games.md, 2026-09-25,
            # BUILD_ID 20260922.6101926); `wizard` makes the fake do the same.
            'apps': [
                {'appid': 2379780, 'display_name': 'Balatro', 'installed': False, 'size': 67000000,
                 'packed': 3 << 8 | 3, 'vr': False, 'wizard': 14},
                {'appid': 274190, 'display_name': 'Broforce', 'installed': False, 'size': 600000000,
                 'packed': 0 << 8 | 2, 'vr': False, 'wizard': 7},
                {'appid': 620980, 'display_name': 'Beat Saber', 'installed': True, 'size': 4200000000,
                 'packed': 3 << 8 | 1, 'vr': True, 'vr_only': True, 'wizard': 14},
            ],
            'shortcuts': [],            # {appid, name, exe, start_dir, icon, devkit_gameid}
            'compat_tools': {},         # shortcut appid (str) -> compat tool alias (CompatToolMapping)
            'install_manager': {'eInstallState': 0, 'currentAppID': 0, 'nDiskSpaceRequired': 0,
                                'nDiskSpaceAvailable': 0},
            'download': None,
            'pages': [],                # extra DevTools targets, e.g. a store page
        },
        'devkit_games': {},             # gameid -> what create-shortcut registered
        'launches': [],                 # every launch Steam was asked for, and what it did
        'pairing_requests': [],
        'lepton': {},                   # container name -> {port, pid, package dir}
    }


def _share(fd):
    # Files are made by root or steamos, whichever comes first; both write them (umask aside).
    try:
        os.fchmod(fd, 0o666)
    except OSError:
        pass                            # not ours: whoever made it already did this


def _ensure_dir():
    os.makedirs(DIR, exist_ok=True)


@contextlib.contextmanager
def _locked(kind):
    _ensure_dir()
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o666)
    _share(fd)
    try:
        fcntl.flock(fd, kind)
        yield
    finally:
        os.close(fd)


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default_state()


def _save(state):
    tmp = f'{STATE}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o666)                # the supervisor (root) and steamos both write it
    os.replace(tmp, STATE)


def read():
    with _locked(fcntl.LOCK_SH):
        return _load()


@contextlib.contextmanager
def update():
    """with update() as s: change s; it's written back when the block ends without an error."""
    with _locked(fcntl.LOCK_EX):
        state = _load()
        yield state
        _save(state)


def reset():
    with _locked(fcntl.LOCK_EX):
        _save(default_state())
        with open(CALLS, 'w'):
            pass
        os.chmod(CALLS, 0o666)


def log(tool, **fields):
    """Append one call record to calls.jsonl."""
    _ensure_dir()
    line = json.dumps({'time': round(time.time(), 3), 'tool': tool, **fields}) + '\n'
    fd = os.open(CALLS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
    _share(fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def calls(tool=None):
    try:
        with open(CALLS) as f:
            out = [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []
    return [c for c in out if tool is None or c['tool'] == tool]


def steam_pid():
    """The fake Steam client's pid if it's running, as devkit_utils.validate_steam_client checks."""
    try:
        with open(HOME + '/.steam/steam.pid') as f:
            pid = int(f.read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None
