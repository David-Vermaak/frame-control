#!/usr/bin/env zsh
# Mac-side: find the Steam Frame, create keys, add a `Host frame` alias to
# ~/.ssh/config, get a key onto the headset, and optionally disable SSH password
# logins. It first pairs through Valve's SteamOS devkit service (port 32000:
# approve on the headset, no password), else copies the key with the password.
#
# Verified on a Frame 2026-09-25 (except --harden and devkit pairing). Idempotent.
#
# Usage:
#   scripts/connect.sh [HOST_OR_IP]          # set up key + alias
#   scripts/connect.sh [HOST_OR_IP] --harden # also disable password auth
#
# Env: FRAME_USER (default steamos), FRAME_ALIAS (default frame).
set -euo pipefail

user_from_env=${+FRAME_USER}
FRAME_USER=${FRAME_USER:-steamos}
FRAME_ALIAS=${FRAME_ALIAS:-frame}
KEY="$HOME/.ssh/id_ed25519_frame"
# The devkit service only accepts ssh-rsa keys, so pairing uses a second key.
DEVKIT_KEY="$HOME/.ssh/id_rsa_frame_devkit"
CONFIG="$HOME/.ssh/config"
BEGIN_MARK="# >>> steam-frame ($FRAME_ALIAS) >>>"
END_MARK="# <<< steam-frame ($FRAME_ALIAS) <<<"
DEVKIT_PORT=32000
DEVKIT_SERVICE=_steamos-devkit._tcp
MAGIC_PHRASE=900b919520e4cf601998a71eec318fec  # fixed token Valve's client appends
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'
HOST_RE='^[A-Za-z0-9][A-Za-z0-9.:%-]*$'

harden=0
host_arg=""
for arg in "$@"; do
  case "$arg" in
    --harden) harden=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) host_arg="$arg" ;;
  esac
done

port_open() {
  # nc resolves through the system resolver (including mDNS for .local).
  nc -z -G 3 "$1" "$2" >/dev/null 2>&1
}

# sshd, or the devkit service, which turns sshd on once a pairing is approved.
reachable() {
  port_open "$1" 22 || port_open "$1" $DEVKIT_PORT
}

# What a command printed within $1 seconds; dns-sd never exits by itself.
run_for() {
  local secs=$1; shift
  "$@" 2>/dev/null &
  local pid=$!
  sleep "$secs"
  kill $pid 2>/dev/null || true
  wait $pid 2>/dev/null || true
}

# Hosts advertising the devkit service over mDNS (dns-sd -B, then -L each).
discover_devkit() {
  local name target
  run_for 3 dns-sd -B $DEVKIT_SERVICE local. \
    | sed -n "s/.* Add .*${DEVKIT_SERVICE//./\\.}\\.[[:space:]]*//p" | awk '!seen[$0]++' | head -n 4 \
    | while IFS= read -r name; do
        target=$(run_for 2 dns-sd -L "$name" $DEVKIT_SERVICE local. \
          | sed -n 's/.* can be reached at \([^ :]*\):[0-9].*/\1/p' | head -n 1)
        [[ -n "$target" ]] && print -r -- "${target%.}"
      done | awk '!seen[$0]++'
}

pick_host() {
  local candidates=()
  [[ -n "$host_arg" ]] && candidates+=("$host_arg")
  [[ -z "$host_arg" ]] && candidates+=("$FRAME_ALIAS.local" "$FRAME_ALIAS")
  local h
  for h in "${candidates[@]}"; do
    if reachable "$h"; then
      print -r -- "$h"; return 0
    fi
    print -u2 "  - $h: not resolvable, or ports 22 and $DEVKIT_PORT closed"
  done
  [[ -n "$host_arg" ]] && return 1
  print -u2 "  - asking mDNS for $DEVKIT_SERVICE"
  for h in ${(f)"$(discover_devkit)"}; do
    if [[ "$h" =~ $HOST_RE ]] && reachable "$h"; then
      print -r -- "$h"; return 0
    fi
    print -u2 "  - $h: advertised, but not reachable"
  done
  return 1
}

make_key() {  # path type comment [extra ssh-keygen args]
  if [[ ! -f "$1" ]]; then
    ssh-keygen -q -t "$2" "${@:4}" -N '' -C "$3" -f "$1"
    print "    created $1"
  else
    print "    exists: $1"
  fi
}

# Takes the lock Frame Control uses to edit ~/.ssh/config (ui/frame_devices.py), so a
# running app and this script never write over each other's change.
write_config() {
  local lockfd="" rc
  zmodload zsh/system 2>/dev/null
  touch "$CONFIG.frame-control.lock" 2>/dev/null
  zsystem flock -t 30 -f lockfd "$CONFIG.frame-control.lock" 2>/dev/null || lockfd=""
  write_config_locked; rc=$?
  [[ -n "$lockfd" ]] && zsystem flock -u "$lockfd"
  return $rc
}

# Checks each step itself: pair_with_devkit calls this from an `elif`, where set -e is off.
write_config_locked() {
  touch "$CONFIG" && chmod 600 "$CONFIG" || return 1
  local tmp new="$CONFIG.frame-control.$$"
  tmp=$(mktemp) || return 1
  # Drop any previous managed block, then PREPEND a fresh one: ssh uses the first
  # value it sees per option, so this block must precede any other "Host frame"
  # or "Host *". The trailing "Host *" returns the rest of the file to global scope.
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0==b {skip=1; next}
    $0==e {skip=0; next}
    !skip {print}
  ' "$CONFIG" > "$tmp" || { rm -f "$tmp"; return 1; }
  {
    print -r -- "$BEGIN_MARK"
    print -r -- "Host $FRAME_ALIAS"
    print -r -- "  HostName $HOST"
    print -r -- "  User $FRAME_USER"
    print -r -- "  IdentityFile $KEY"
    print -r -- "  IdentityFile $DEVKIT_KEY"
    print -r -- "  IdentitiesOnly yes"
    print -r -- "  ServerAliveInterval 30"
    print -r -- "Host *"
    print -r -- "$END_MARK"
    cat "$tmp"
  } > "$new" && chmod 600 "$new" && mv -f "$new" "$CONFIG" \
    || { rm -f "$new"; print -u2 "!! Writing $CONFIG failed; its previous contents are in $tmp"; return 1; }
  rm -f "$tmp"
}

# accept-new: after pairing, this is the first contact, so trust a first-seen host
# key (as ssh-copy-id's prompt would); a changed one still fails.
key_login_works() {
  ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$FRAME_ALIAS" true 2>/dev/null
}

# The User in our managed block, so a re-run keeps one the headset named earlier.
configured_user() {
  [[ -f "$CONFIG" ]] || return 0
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0==b {inside=1; next}
    $0==e {exit}
    inside && $1=="User" {print $2; exit}
  ' "$CONFIG"
}

devkit_url() {
  if [[ "$HOST" == *:* ]]; then print -r -- "http://[$HOST]:$DEVKIT_PORT$1"
  else print -r -- "http://$HOST:$DEVKIT_PORT$1"; fi
}

# Valve's steamos-devkit-service: GET /properties.json names the login user; POST
# /register with "ssh-rsa <key> <comment> <magic>" shows an approve prompt in the
# headset (the comment is what it displays, 30 s to answer), then installs the key
# and turns sshd on. Returns non-zero with the reason in $devkit_why to fall back.
devkit_why=""
pair_with_devkit() {
  local props login comment body resp code text err
  print "==> Pairing through the headset's SteamOS devkit service (no password)"
  if [[ ! -r "$DEVKIT_KEY.pub" ]]; then
    devkit_why="can't read the pairing key $DEVKIT_KEY.pub"; return 1
  fi
  if ! props=$(curl -fsS --noproxy '*' -m 5 "$(devkit_url /properties.json)" 2>&1); then
    devkit_why="devkit service not reachable on port $DEVKIT_PORT: ${${props##*curl: }%%$'\n'*}"; return 1
  fi
  # properties.json is Valve's json.dumps(indent=2): "login" sits on its own line.
  login=$(print -r -- "$props" | sed -n 's/.*"login"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  [[ "$login" =~ $NAME_RE && "$login" != root ]] || login=""
  # Before the prompt, so the password fallback uses this user too.
  if [[ -n "$login" && "$login" != "$FRAME_USER" ]]; then
    if (( user_from_env )); then
      print "    the headset logs in as '$login'; keeping FRAME_USER=$FRAME_USER"
    else
      FRAME_USER=$login
      print "    the headset logs in as '$FRAME_USER'"
      write_config || { print -u2 "Could not rewrite $CONFIG."; exit 1; }
    fi
  fi
  # One word: the headset splits the body on spaces and shows the third field.
  comment="frame-control@$(hostname -s | tr -cs 'A-Za-z0-9._-' '-' | sed 's/^[-.]*//; s/[-.]*$//')"
  [[ "$comment" == "frame-control@" ]] && comment="frame-control@computer"
  body="ssh-rsa $(awk '{print $2}' "$DEVKIT_KEY.pub") $comment $MAGIC_PHRASE"
  print "    In the headset: Steam Settings > Developer > Pair new host, then approve the request"
  # The headset refuses at once unless Steam is on its "Pair new host" screen
  # (verified on a Frame, 2026-09-26), so keep asking for 2 minutes while it's opened.
  local deadline=$(( SECONDS + 120 ))
  while true; do
    if ! resp=$(print -r -- "$body" | curl -sS --noproxy '*' -m 60 -H 'Content-Type: text/plain' \
        --data-binary @- -w '\n%{http_code}' "$(devkit_url /register)" 2>&1); then
      devkit_why="devkit pairing failed: no answer (${${resp##*curl: }%%$'\n'*})"; return 1
    fi
    code=${resp##*$'\n'}
    text=${resp%$'\n'*}
    [[ "$code" == 2* ]] && break
    err=$(print -r -- "$text" | sed -n 's/.*"error"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    devkit_why="devkit pairing failed: ${err:-${text:-HTTP $code}}"
    [[ "$devkit_why" == *"pairing mode"* ]] && (( SECONDS < deadline )) || return 1
    sleep 3
  done
  # The approval is what turns sshd on, so it may take a moment to answer.
  local i
  for i in {1..10}; do
    key_login_works && return 0
    sleep 1
  done
  devkit_why="paired, but key login still fails"; return 1
}

print "==> Looking for the Steam Frame"
if ! HOST=$(pick_host); then
  print -u2 "Could not reach the Frame on port 22 or $DEVKIT_PORT."
  print -u2 "Check: Developer Mode on + user password set; same Wi-Fi; no client isolation."
  print -u2 "Then re-run with the IP from Quick Settings: scripts/connect.sh 192.168.x.y"
  exit 1
fi
print "    found: $HOST"

print "==> SSH keys"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
make_key "$KEY" ed25519 "mac->steam-frame"
make_key "$DEVKIT_KEY" rsa "frame-control@$(hostname -s | tr -cs 'A-Za-z0-9._-' '-' | sed 's/^[-.]*//; s/[-.]*$//')" -b 3072

if (( ! user_from_env )); then
  prev_user=$(configured_user)
  if [[ "$prev_user" =~ $NAME_RE ]]; then FRAME_USER=$prev_user; fi
fi
print "==> ~/.ssh/config alias '$FRAME_ALIAS' -> $HOST"
write_config

print "==> Checking key login"
if key_login_works; then
  print "    key login already works"
elif pair_with_devkit; then
  print "    paired; key login OK"
else
  print "    $devkit_why; falling back to the password"
  print "    copying key (enter the Developer Mode password once)"
  ssh-copy-id -i "$KEY.pub" -o IdentitiesOnly=yes "$FRAME_USER@$HOST"
  key_login_works || { print -u2 "Key login still failing after ssh-copy-id."; exit 1; }
  print "    key login OK"
fi

if (( harden )); then
  print "==> Disabling SSH password auth (sudo password asked on the Frame)"
  # shellcheck disable=SC2016
  if ! ssh -t "$FRAME_ALIAS" '
    set -e
    grep -Eiq "^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf" /etc/ssh/sshd_config \
      || { echo "sshd_config has no sshd_config.d include; not hardening."; exit 1; }
    printf "PasswordAuthentication no\nKbdInteractiveAuthentication no\n" \
      | { sudo mkdir -p /etc/ssh/sshd_config.d; sudo tee /etc/ssh/sshd_config.d/01-frame-keys-only.conf >/dev/null; }
    sudo sshd -t
    sudo systemctl reload sshd
    echo "password auth disabled"
  '; then
    print -u2 "!! Hardening failed. If the drop-in was written, it will disable password SSH"
    print -u2 "!! on the next sshd restart. To undo it:"
    print -u2 "!!   ssh $FRAME_ALIAS 'sudo rm -f /etc/ssh/sshd_config.d/01-frame-keys-only.conf'"
    exit 1
  fi
  if ssh -o BatchMode=yes -o ConnectTimeout=5 "$FRAME_ALIAS" true; then
    print "    key login still OK after hardening"
  else
    print -u2 "!! Key login FAILED after hardening. Password SSH is now off."
    print -u2 "!! Recover via RDP or 'adb shell' (USB-C), then run:"
    print -u2 "!!   sudo rm /etc/ssh/sshd_config.d/01-frame-keys-only.conf && sudo systemctl reload sshd"
    exit 1
  fi
fi

print "\nDone. Try: ssh $FRAME_ALIAS"
