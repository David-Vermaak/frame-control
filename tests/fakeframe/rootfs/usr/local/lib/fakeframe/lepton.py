#!/usr/bin/env python3
"""Stand-in for Lepton's launcher (~/.local/share/Steam/steamapps/common/Lepton/lepton).

frame/android/lepton-app.sh runs it as `lepton waitforexitandrun -- APK` with
the Steam compat variables set. Like the real one (docs/apks.md, verified
2026-09-25, BUILD_ID 20260922.6101926) it:
- dies with "unbound variable" when STEAM_COMPAT_SHADER_PATH isn't set;
- needs STEAM_COMPAT_DATA_PATH under ~/.local/share/Steam (only that tree is
  mounted in the container; elsewhere the app crashes with ENOENT);
- runs a "steamlaunch" container named lepton-steamlaunch-<SteamAppId> when
  SteamAppId is set, else Lepton Development (lepton-dev);
- opens ADB on 0.0.0.0: 5555 for Lepton Development, the next free port for
  each own instance (README security notes, 2026-09-25);
- shows a flat window only when lepton-show-flatscreen sits next to the APK.
There's no Android inside: it records the launch and holds the port until
`podman stop` (the podman stub) ends it.
"""
import json
import os
import signal
import socket
import sys
import time

sys.path.insert(0, '/usr/local/lib/fakeframe')
import fakeframe_state as fs  # noqa: E402

ENV = ('SteamAppId', 'STEAM_COMPAT_INSTALL_PATH', 'STEAM_COMPAT_DATA_PATH', 'STEAM_COMPAT_SHADER_PATH',
       'STEAM_FOSSILIZE_DUMP_PATH', 'IS_PARENT')


def main():
    args = sys.argv[1:]
    env = {k: os.environ.get(k) for k in ENV}
    fs.log('lepton', args=args, env=env)
    for k in ('STEAM_COMPAT_SHADER_PATH', 'STEAM_COMPAT_DATA_PATH', 'STEAM_COMPAT_INSTALL_PATH'):
        if not env[k]:
            sys.exit(f'{sys.argv[0]}: line 1: {k}: unbound variable')
    if not os.path.realpath(env['STEAM_COMPAT_DATA_PATH']).startswith(fs.STEAM_ROOT + '/'):
        sys.exit('ENOENT: STEAM_COMPAT_DATA_PATH is outside ~/.local/share/Steam, which is all the container sees')
    apk = args[-1] if args else ''
    if not apk.endswith('.apk') or not os.path.isfile(apk):
        sys.exit(f'lepton: no APK at {apk!r}')
    steamlaunch = bool(env['SteamAppId'])
    name = f"lepton-steamlaunch-{env['SteamAppId']}" if steamlaunch else 'lepton-dev'

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with fs.update() as state:
        if name in state['lepton']:
            sys.exit(f'lepton: {name} is already running')
        used = {c['port'] for c in state['lepton'].values()}
        for port in ([5555] if not steamlaunch else range(5556, 5600)):
            if port in used:
                continue
            try:
                sock.bind(('0.0.0.0', port))
                break
            except OSError:
                continue
        else:
            sys.exit('lepton: no free ADB port')
        sock.listen(8)
        os.makedirs(os.path.join(env['STEAM_COMPAT_DATA_PATH'], 'internal'), exist_ok=True)
        state['lepton'][name] = {'port': port, 'pid': os.getpid(), 'apk': apk, 'started': time.time(),
                                 'flatscreen': os.path.exists(os.path.join(os.path.dirname(apk),
                                                                           'lepton-show-flatscreen'))}

    def stop(*_):
        with fs.update() as state:
            state['lepton'].pop(name, None)
        fs.log('lepton', stopped=name)
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({'container': name, 'adb_port': port}), flush=True)
    while True:
        conn, _ = sock.accept()     # nothing speaks ADB here; just close
        conn.close()


if __name__ == '__main__':
    main()
