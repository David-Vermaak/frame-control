#!/usr/bin/env zsh
# Mac-side: start a Linux app on the Steam Frame as its OWN floating VR panel,
# separate from the Plasma desktop panel, so you can place it anywhere.
#
# How it works (verified 2026-09-25): gamescope runs with
# --virtual-connector-strategy PerAppId, so every distinct Steam app id gets
# its own SteamVR overlay (valve.steam.desktopgame.<id>). Steam normally sets
# that id on a game's X11 windows through the STEAM_GAME property. This script
# starts the app as an X11 client of gamescope (DISPLAY=:0), then tags each new
# top-level window with a per-panel id, which makes a new panel appear.
#
# Usage:
#   scripts/panel-on-frame.sh [--id N] [--name LABEL] konsole
#   scripts/panel-on-frame.sh --name notes -- kate '~/notes.md'
#   scripts/panel-on-frame.sh org.mozilla.firefox          # Flatpak app ID
#   scripts/panel-on-frame.sh mac-screen                   # Remmina into the Mac
#
# Apps sharing an id share a panel. The default id is derived from --name (or
# the command), so re-running the same app reuses its panel slot.
# A leading "~/" in any argument is expanded on the Frame (quote it on the Mac).
set -euo pipefail

FRAME_ALIAS=${FRAME_ALIAS:-frame}
# Frame Control passes the headset it has chosen: its address and pinned identity.
ssh_opts=(${(Q)${(z)FRAME_SSH_OPTS:-}})
REMMINA_PROFILE="~/.var/app/org.remmina.Remmina/data/remmina/mac-screen-sharing.remmina"
id="" name=""

while (( $# )); do
  case "$1" in
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    --id)      id=${2:?--id needs a number}; shift 2 ;;
    --name)    name=${2:?--name needs a label}; shift 2 ;;
    *)         break ;;
  esac
done

case "${1:-}" in
  "")         sed -n '2,20p' "$0"; exit 2 ;;
  remmina)    cmd=(flatpak run org.remmina.Remmina) ;;
  mac-screen) cmd=(flatpak run org.remmina.Remmina -c "$REMMINA_PROFILE") ;;
  --)         shift; (( $# )) || { print -u2 "panel-on-frame: missing command after --"; exit 2; }
              cmd=("$@") ;;
  *)
    if [[ "$1" =~ '^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}$' ]]; then
      cmd=(flatpak run "$@")
    else
      cmd=("$@")
    fi ;;
esac

if [[ -z "$id" ]]; then
  # Stable id per label, well above real Steam app ids (< 5,000,000 today).
  label=${name:-${cmd[*]}}
  id=$(( 2000000000 + $(print -rn -- "$label" | cksum | cut -d' ' -f1) % 1000000 ))
fi
[[ "$id" == <1-4294967295> ]] || { print -u2 "panel-on-frame: --id must be a positive 32-bit number"; exit 2; }

# Runs on the Frame with the app id as $1 and the command as the rest.
remote=$(cat <<'EOF'
set -u
appid=$1; shift
export DISPLAY=:0
unset WAYLAND_DISPLAY
# Make toolkits pick X11 so the window lands on gamescope's Xwayland.
export QT_QPA_PLATFORM=xcb GDK_BACKEND=x11 SDL_VIDEODRIVER=x11 MOZ_ENABLE_WAYLAND=0
if ! xprop -root GAMESCOPE_FOCUSABLE_WINDOWS >/dev/null 2>&1; then
  echo "gamescope's X display :0 isn't reachable; is the headset awake?" >&2
  exit 2
fi
toplevels() { xwininfo -root -children 2>/dev/null | awk '/^ +0x/ {print $1}' | sort; }
before=$(toplevels)
if [ -z "$before" ]; then
  echo "couldn't list windows on :0 (is xwininfo installed?)" >&2
  exit 2
fi
args=()
for a in "$@"; do
  case "$a" in "~/"*) a="$HOME/${a#\~/}" ;; esac
  args+=("$a")
done
log=$(mktemp /tmp/panel-on-frame.XXXXXX)
setsid nohup "${args[@]}" > "$log" 2>&1 < /dev/null &
child=$!
tagged=0 first=0
# Tag new mapped windows: keep watching ~3s after the first (splash screens,
# secondary windows), up to 20s in total for slow Flatpaks.
for i in $(seq 1 40); do
  sleep 0.5
  [ "$tagged" -eq 0 ] && ! kill -0 "$child" 2>/dev/null && break
  for w in $(comm -13 <(printf '%s\n' "$before") <(toplevels)); do
    xwininfo -id "$w" 2>/dev/null | grep -q 'Map State: IsViewable' || continue
    xprop -id "$w" STEAM_GAME 2>/dev/null | grep -q '= ' && continue
    xprop -id "$w" -f STEAM_GAME 32c -set STEAM_GAME "$appid" 2>/dev/null && tagged=$((tagged + 1))
  done
  [ "$tagged" -gt 0 ] && [ "$first" -eq 0 ] && first=$i
  [ "$first" -gt 0 ] && [ "$i" -ge $((first + 6)) ] && break
done
if [ "$tagged" -gt 0 ]; then
  echo "panel: ${args[*]} -> valve.steam.desktopgame.$appid ($tagged window(s), pid $child, log $log)"
elif kill -0 "$child" 2>/dev/null; then
  echo "started ${args[*]} (pid $child) but no new X11 window appeared." >&2
  echo "It may be Wayland-only or single-instance (already running elsewhere). Log: $log" >&2
  exit 1
else
  echo "failed: ${args[*]} exited. Log:" >&2
  tail -n 20 "$log" >&2
  exit 1
fi
EOF
)
b64=$(print -rn -- "$remote" | base64)

ssh "${ssh_opts[@]}" "$FRAME_ALIAS" "bash -c \"\$(echo $b64 | base64 -d)\" panel-on-frame $id ${(j: :)${(@q)cmd}}"
