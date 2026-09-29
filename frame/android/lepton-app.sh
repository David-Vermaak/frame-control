#!/bin/bash
# Frame-side: run one Android app in its own persistent Lepton instance.
# Copied into ~/Applications/Android/<package>/launch.sh by the Mac-side
# installer, next to app.apk, instance.id and (for 2D apps) the empty
# lepton-show-flatscreen marker. A non-Steam shortcut points at this file.
#
# Why not Lepton Development: it wipes every app it installed when it exits.
# A "steamlaunch" context (SteamAppId set) keeps app data in
# compatdata/<id>/internal across restarts and APK updates. Pattern from
# frame/t3code/launch.sh; see docs/apks.md.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LEPTON="$HOME/.local/share/Steam/steamapps/common/Lepton/lepton"
[[ -x "$LEPTON" ]] || { echo "Lepton isn't installed (Steam app 3056000)" >&2; exit 1; }
for need in "$DIR/app.apk" "$DIR/instance.id"; do
  [[ -f "$need" ]] || { echo "launch.sh: missing $need" >&2; exit 1; }
done

# A number that isn't a real Steam app; it names this app's Lepton context.
export SteamAppId="$(cat "$DIR/instance.id")"
[[ "$SteamAppId" =~ ^[0-9]+$ ]] || { echo "invalid instance.id" >&2; exit 1; }
# Keep the stable Lepton context, but identify the Android VR client as its
# actual Steam shortcut. Lepton applies LEPTON_ENV_* after its own passthrough.
if [[ -f "$DIR/shortcut.id" ]]; then
  shortcut="$(cat "$DIR/shortcut.id")"
  [[ "$shortcut" =~ ^[0-9]+$ ]] || { echo "invalid shortcut.id" >&2; exit 1; }
  export LEPTON_ENV_SteamAppId="$shortcut"
fi
exec 9>"$DIR/launch.lock"
flock -n 9 || { echo "Android app is already running" >&2; exit 1; }
CONTAINER="lepton-steamlaunch-$SteamAppId"
# Holding the lock means no launcher owns a running container: it was orphaned
# (this script SIGKILLed), so stop it rather than refuse every later Play. The
# name is this app's alone. A Lepton host process whose launcher was killed
# before it made the container may linger briefly; nothing else is killed.
if [[ "$(podman inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == true ]]; then
  echo "Stopping orphaned $CONTAINER" >&2
  podman stop -t 5 "$CONTAINER" >/dev/null 2>&1 || true
fi
export STEAM_COMPAT_INSTALL_PATH="$DIR"
# Must be under ~/.local/share/Steam: only that tree is mounted in the container.
export STEAM_COMPAT_DATA_PATH="$HOME/.local/share/Steam/steamapps/compatdata/$SteamAppId"
export STEAM_COMPAT_SHADER_PATH="$HOME/.local/share/Steam/steamapps/shadercache/$SteamAppId"
export STEAM_FOSSILIZE_DUMP_PATH="$STEAM_COMPAT_SHADER_PATH/fozpipelinesv6/steamapp_pipeline_cache"
mkdir -p "$STEAM_COMPAT_DATA_PATH" "$STEAM_FOSSILIZE_DUMP_PATH"

# Lepton's setpgid --foreground re-exec needs a terminal that Steam shortcuts
# and SSH don't have; give it its own session instead.
export IS_PARENT=true
# Keep this shell in Steam's process tree; setsid alone has no container cleanup.
child=""
cleanup() {
  trap '' TERM INT HUP
  if [[ -n "$child" ]]; then
    kill -TERM -- "-$child" 2>/dev/null || true
    kill -TERM "$child" 2>/dev/null || true
  fi
  podman stop -t 5 "$CONTAINER" >/dev/null 2>&1 || true
  if [[ -n "$child" ]]; then
    kill -KILL -- "-$child" 2>/dev/null || true
    kill -KILL "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP
# 9>&-: the lock is this launcher's alone; Lepton's tree mustn't keep it held.
setsid --wait "$LEPTON" waitforexitandrun -- "$DIR/app.apk" 9>&- &
child=$!
rc=0
wait "$child" || rc=$?
child=""
exit "$rc"
