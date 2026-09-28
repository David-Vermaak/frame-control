#!/usr/bin/env python3
"""Install or run our local-only tracking tools on the Frame.

  python3 scripts/tracking-on-frame.py install
  python3 scripts/tracking-on-frame.py gaze --seconds 10
  python3 scripts/tracking-on-frame.py gaze --seconds 3600 --osc 127.0.0.1 9000
  python3 scripts/tracking-on-frame.py heart --device AA:BB:CC:DD:EE:FF --panel

FRAME_ALIAS overrides the SSH alias (default: frame). Runs in the foreground;
Ctrl-C stops the reader. No service, autostart, sudo or SteamVR settings changes.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

REMOTE = '"$HOME/.local/share/frame-control/tracking"'
INSTALL = '''set -eu
base="$HOME/.local/share/frame-control/tracking"
mkdir -p "$base"
stage=$(mktemp -d "$base/.install.XXXXXX")
trap 'rm -rf "$stage"' EXIT
 tar -xf - -C "$stage"
cc -O2 -Wall -Wextra -Werror "$stage/gaze.c" \\
  -L/opt/steamvr/bin/linuxarm64 -Wl,-rpath,/opt/steamvr/bin/linuxarm64 \\
  -lopenxr_loader -o "$stage/gaze"
chmod 700 "$stage/gaze" "$stage/tracking.py"
mv "$stage/gaze" "$stage/tracking.py" "$base/"
echo 'Installed Frame Control tracking tools (no service started).'
'''


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "gaze", "heart"):
        print(__doc__)
        return 2
    host = os.environ.get("FRAME_ALIAS", "frame")
    if not host or host.startswith("-"):
        raise ValueError("invalid SSH alias")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host]
    if sys.argv[1] == "install":
        import tempfile
        source = Path(__file__).resolve().parents[1] / "frame" / "tracking"
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for name in ("gaze.c", "tracking.py"):
                    tar.add(source / name, arcname=name)
            archive.seek(0)
            return subprocess.call(ssh + ["bash -c " + shlex.quote(INSTALL)], stdin=archive)
    # Allocate a tty so SSH forwards Ctrl-C and hangup to the foreground process.
    command = 'exec python3 ' + REMOTE + '/tracking.py ' + shlex.join(sys.argv[1:])
    return subprocess.call(ssh[:-1] + ["-tt", host, command])


if __name__ == "__main__":
    raise SystemExit(main())
