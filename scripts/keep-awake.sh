#!/usr/bin/env zsh
# Mac-side: stop the Steam Frame from going to sleep while an agent works on it.
#
# The Frame sleeps when Steam's own idle timer runs out ("Sleep after
# inactivity": 60 min on AC, 15 min on battery by default). SSH activity
# doesn't count as input, and asleep the Frame is off the network. `on` sets
# both timers to Never through Steam's UI (DevTools on 127.0.0.1:8080, via
# ui/frame_steam.py) and holds a logind sleep inhibitor as a user unit.
# `off` drops the inhibitor and restores the timers `on` saved.
#
# Usage:
#   scripts/keep-awake.sh on
#   scripts/keep-awake.sh off
#   scripts/keep-awake.sh status
set -euo pipefail

FRAME_ALIAS=${FRAME_ALIAS:-frame}
HERE=${0:A:h}
cmd=${1:-status}
case $cmd in on|off|status) ;; *) echo "usage: keep-awake.sh on|off|status" >&2; exit 2 ;; esac

ssh -o ConnectTimeout=8 "$FRAME_ALIAS" \
  'mkdir -p ~/.cache/frame-control && cat > ~/.cache/frame-control/frame_steam.py' < "$HERE/../ui/frame_steam.py"

# Runs on the Frame. Verified 2026-09-28 (BUILD_ID 20260925.6191901): the
# timers are client settings system_idle_suspend_{ac,battery}_sec (0 = Never),
# written the way Steam's settings page does (steamui module exporting the
# SetSetting wrapper). logind refuses an inhibitor from an SSH session
# ("Interactive authentication required") but allows one from a user unit.
ssh "$FRAME_ALIAS" python3 - "$cmd" <<'EOF'
import json, os, subprocess, sys
sys.path.insert(0, os.path.expanduser("~/.cache/frame-control"))
from frame_steam import Page

cmd = sys.argv[1]
saved_path = os.path.expanduser("~/.cache/frame-control/keep-awake.json")
unit = "fc-keep-awake"
keys = ("system_idle_suspend_ac_sec", "system_idle_suspend_battery_sec")

if cmd == "off":  # release the lock first, even if Steam's UI is down
    subprocess.run(["systemctl", "--user", "stop", unit], stderr=subprocess.DEVNULL)
page = Page()
def read():
    return {k: page.eval(f"settingsStore.clientSettings.{k}") for k in keys}
def write(values):
    page.eval("""(async () => { let req;
      webpackChunksteamui.push([[Symbol()], {}, r => { req = r }]);
      const mod = Object.keys(req.m).map(id => req.m[id].toString().includes("Settings.SetSetting") ? req(id) : null).find(Boolean);
      const set = Object.values(mod).find(f => typeof f == "function" && f.toString().includes("SetSetting("));
      for (const [k, v] of Object.entries(%s)) await set(k, v);
      await new Promise(r => setTimeout(r, 1000)); })()""" % json.dumps(values))
def inhibitor():
    return subprocess.run(["systemctl", "--user", "is-active", "-q", unit]).returncode == 0

if cmd == "on":
    current = read()
    if not os.path.exists(saved_path):
        with open(saved_path, "w") as f:
            json.dump(current, f)
    write({k: 0 for k in keys})
    if not inhibitor():
        subprocess.run(["systemd-run", "--user", "-q", f"--unit={unit}",
                        "--description=Frame Control: keep the Frame awake",
                        "systemd-inhibit", "--what=sleep:idle:handle-suspend-key:handle-power-key",
                        "--who=Frame Control", "--why=Keep the Frame awake while an agent works on it",
                        "--mode=block", "sleep", "infinity"], check=True)
elif cmd == "off":
    if os.path.exists(saved_path):  # no backup: leave the timers as they are
        with open(saved_path) as f:
            write(json.load(f))
        os.remove(saved_path)

print(json.dumps({"timers": read(), "inhibitor": inhibitor()}))
EOF
