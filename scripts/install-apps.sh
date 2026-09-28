#!/usr/bin/env zsh
# Mac-side: install Flatpaks on the Steam Frame over SSH (per-user, so they
# survive SteamOS updates and need no sudo / steamos-readonly changes).
#
# Verified on a Frame 2026-09-25 (remmina + --vnc-host). Idempotent.
# The "exports/share is not in the search path" warning only applies to the
# SSH shell; the headset desktop's XDG_DATA_DIRS already includes it.
#
# Usage:
#   scripts/install-apps.sh remmina [--vnc-host my-mac.local]
#   scripts/install-apps.sh moonlight
#   scripts/install-apps.sh org.example.SomeApp   # any Flathub app ID
#
# --vnc-host pre-seeds a Remmina profile pointing at the Mac's built-in
# Screen Sharing (VNC, port 5900) so nothing needs typing in the headset.
set -euo pipefail

FRAME_ALIAS=${FRAME_ALIAS:-frame}
vnc_host=""
apps=()

while (( $# )); do
  case "$1" in
    --vnc-host) vnc_host=${2:?--vnc-host needs a hostname}; shift 2
      [[ "$vnc_host" =~ '^[A-Za-z0-9.-]+$' ]] || { print -u2 "Bad hostname: $vnc_host"; exit 2; } ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    remmina) apps+=(org.remmina.Remmina); shift ;;
    moonlight) apps+=(com.moonlight_stream.Moonlight); shift ;;
    [A-Za-z]*.*[A-Za-z0-9_]) [[ "$1" =~ '^[A-Za-z0-9_.-]+$' ]] || { print -u2 "Bad app ID: $1"; exit 2; }; apps+=("$1"); shift ;;
    *) print -u2 "Unknown app '$1' (use remmina, moonlight, or a Flathub app ID)"; exit 2 ;;
  esac
done

if (( ${#apps} == 0 )) && [[ -z "$vnc_host" ]]; then
  sed -n '2,13p' "$0"; exit 2
fi

if (( ${#apps} )); then
  print "==> Installing on $FRAME_ALIAS: ${apps[*]}"
  ssh "$FRAME_ALIAS" "
    set -e
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user -y flathub ${(j: :)${(@q)apps}}
  "
fi

if [[ -n "$vnc_host" ]]; then
  print "==> Writing Remmina profile for vnc://$vnc_host"
  ssh "$FRAME_ALIAS" "
    set -e
    d=\$HOME/.var/app/org.remmina.Remmina/data/remmina
    mkdir -p \"\$d\"
    cat > \"\$d/mac-screen-sharing.remmina\" <<'EOF'
[remmina]
name=Mac Screen Sharing
protocol=VNC
server=$vnc_host:5900
colordepth=32
quality=9
viewonly=0
showcursor=1
scale=1
viewmode=1
window_maximize=1
EOF
    echo \"wrote \$d/mac-screen-sharing.remmina\"
  "
  print "On the Mac: System Settings > General > Sharing > Screen Sharing (i) >"
  print "  enable 'VNC viewers may control screen with password' and set one."
  print "Remmina may ask for your Mac account name + login password instead (Apple auth)."
fi
