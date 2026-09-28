#!/bin/bash
# The computer side of the harness: tester's ~/.ssh as Frame Control's setup
# leaves it (ui/frame_connect.py's own config block), pointing `frame` at the
# fake Frame, with the harness key the fake trusts.
set -euo pipefail
host=${FAKEFRAME_HOST:-fakeframe}
for _ in $(seq 1 240); do
  [ -s /keys/id_ed25519_frame.pub ] && break
  sleep 0.5
done
mkdir -p -m 700 ~/.ssh
install -m 600 /keys/id_ed25519_frame ~/.ssh/id_ed25519_frame
install -m 644 /keys/id_ed25519_frame.pub ~/.ssh/id_ed25519_frame.pub
python3 - "$host" > ~/.ssh/config <<'PY'
import sys
sys.path.insert(0, '/repo/ui')
import frame_connect
print('\n'.join(frame_connect.config_block(sys.argv[1])))
PY
chmod 600 ~/.ssh/config
until ssh-keyscan -T 2 "$host" > ~/.ssh/known_hosts 2>/dev/null && [ -s ~/.ssh/known_hosts ]; do
  sleep 1
done
touch ~/.ready
exec sleep infinity
