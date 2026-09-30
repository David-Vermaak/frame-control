#!/usr/bin/env zsh
# Mac or Linux: drive a Windows 11 test VM for Frame Control's Windows build.
# The VM is a dockur/windows container on another machine; this reaches it over
# SSH through that machine. See docs/testing.md#windows-test-vm.
#
# Usage: scripts/windows-vm.sh <command> [args]
#   up | down | status      start it (waits for SSH), shut Windows down cleanly, show state
#   ps '<PowerShell>'       run PowerShell as the VM's user
#   put <file> [<dir>]      copy a file in (default: the user's Downloads)
#   shot <out.png>          save the VM's screen
#   click <x> <y>           click screen pixel x,y in the signed-in session
#   scroll <x> <y> <n>      turn the wheel n notches at x,y (negative scrolls down)
#   keys <key>...           type QEMU key names: a, shift-a, ret, tab, esc, spc ...
#   frame-key add|remove    let the VM's Frame Control key into the headset (`frame`
#                           alias) for a test run, then take it out again
#
# Set WINVM_HOST to the ssh alias of the machine running the container. Optional:
# WINVM_CONTAINER (frame-winvm), WINVM_USER (frame), WINVM_PORT (2222: the VM's
# sshd, published on that machine's loopback), WINVM_KEY (~/.ssh/id_ed25519_winvm).
set -euo pipefail
setopt extendedglob

host=${WINVM_HOST:?set WINVM_HOST to the ssh alias of the machine running the VM}
ctr=${WINVM_CONTAINER:-frame-winvm}
user=${WINVM_USER:-frame}
port=${WINVM_PORT:-2222}
key=${WINVM_KEY:-$HOME/.ssh/id_ed25519_winvm}
opts=(-o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new
      -o UserKnownHostsFile=${TMPDIR:-/tmp}/windows-vm-known_hosts -i $key -J $host)
TAG=windows-vm-test  # comment on the VM's key in the headset's authorized_keys

die() { print -u2 "windows-vm: $*"; exit 1 }
int() { [[ $1 == (-|)<-99999> ]] || die "not a whole number (up to 99999): $1" }
# These go into commands run by a shell on the other machine, so keep them plain.
for v in $host $ctr $user; do [[ $v == [A-Za-z0-9_.]##[A-Za-z0-9_.-]# ]] || die "not a plain name: $v"; done
[[ $port == <1-65535> ]] || die "WINVM_PORT isn't a port: $port"

# Windows' OpenSSH waits for stdin to close, so it always gets /dev/null. The
# script travels UTF-16 base64-encoded, so no quoting survives two shells.
vm_ps() {
  local b64=$(print -rn -- "\$ProgressPreference = 'SilentlyContinue'"$'\n'"$1" |
              iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n')
  ssh $opts -p $port $user@127.0.0.1 "powershell -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand $b64" </dev/null
}

# QEMU's monitor inside the container: one command per line on stdin.
monitor() { ssh $host "docker exec -i $ctr nc -q 1 -U /run/shm/monitor.sock" >/dev/null }

# Input has to come from the signed-in desktop session, not SSH's session 0, so
# a scheduled task running as the user replays one click or wheel turn written
# to input.txt, then deletes the file to say it's done. Any error stops it
# before that, so the caller times out instead of reporting a click.
INPUT_PS1='$ErrorActionPreference = "Stop"
$a = (Get-Content "$PSScriptRoot\input.txt").Trim() -split "\s+"
Add-Type -Namespace WinVm -Name Input -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint flags, int dx, int dy, int data, System.IntPtr extra);
"@
[WinVm.Input]::SetProcessDPIAware() | Out-Null
if (-not [WinVm.Input]::SetCursorPos([int]$a[0], [int]$a[1])) { throw "SetCursorPos failed" }
Start-Sleep -Milliseconds 150
if ($a[2] -eq "click") {
  [WinVm.Input]::mouse_event(0x2, 0, 0, 0, [IntPtr]::Zero); Start-Sleep -Milliseconds 60
  [WinVm.Input]::mouse_event(0x4, 0, 0, 0, [IntPtr]::Zero)
} else { [WinVm.Input]::mouse_event(0x800, 0, 0, 120 * [int]$a[3], [IntPtr]::Zero) }
Remove-Item "$PSScriptRoot\input.txt"'

pointer() {  # x y click|wheel [notches]
  local b64=$(print -rn -- $INPUT_PS1 | base64 | tr -d '\n')
  vm_ps '$ErrorActionPreference = "Stop"
$d = Join-Path $env:LOCALAPPDATA "windows-vm"; $req = "$d\input.txt"; $held = $false
trap { [Console]::Error.WriteLine("windows-vm: $_"); if ($held) { Remove-Item $req -ErrorAction SilentlyContinue }; exit 1 }
New-Item -ItemType Directory -Force $d | Out-Null
$old = Get-Item $req -ErrorAction SilentlyContinue   # left behind by a run that died
if ($old -and $old.LastWriteTime -lt (Get-Date).AddSeconds(-15)) { Remove-Item $req }
# Creating input.txt is the lock: CreateNew fails if another request already has it.
try { $f = [IO.File]::Open($req, "CreateNew", "Write") }
catch { [Console]::Error.WriteLine("windows-vm: another click or scroll is in progress"); exit 1 }
$held = $true
$text = [Text.Encoding]::ASCII.GetBytes("'"$*"'"); $f.Write($text, 0, $text.Length); $f.Close()
[IO.File]::WriteAllText("$d\input.ps1", [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("'$b64'")))
$act = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$d\input.ps1`""
$who = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName WindowsVmInput -Action $act -Principal $who -Force | Out-Null
Start-ScheduledTask -TaskName WindowsVmInput
foreach ($i in 1..50) { if (-not (Test-Path $req)) { exit 0 }; Start-Sleep -Milliseconds 200 }
Remove-Item $req -ErrorAction SilentlyContinue
[Console]::Error.WriteLine("windows-vm: no input after 10 s: is $env:USERNAME signed in on the VM screen, and is x,y on it?"); exit 1'
}

(( $# )) || die "usage: see the top of $0"
cmd=$1; shift
case $cmd in
  up)
    ssh $host "docker start $ctr" >/dev/null
    for i in {1..60}; do
      vm_ps 'exit 0' 2>/dev/null && { print "up"; exit 0 }
      sleep 5
    done
    die "Windows didn't answer on SSH within 5 minutes" ;;
  down)   # the container turns SIGTERM into an ACPI shutdown and waits for Windows
    ssh $host "docker stop -t 150 $ctr" >/dev/null && print "down" ;;
  status) ssh $host "docker ps -a --filter 'name=^$ctr\$' --format '{{.Names}}: {{.Status}}'" ;;
  ps)     (( $# == 1 )) || die "usage: ps '<PowerShell>'"; vm_ps "$1" ;;
  put)
    [[ -f ${1:-} ]] || die "usage: put <file> [<dir>]"
    scp -q $opts -P $port $1 "$user@127.0.0.1:${2:-C:/Users/$user/Downloads}/${1:t}" </dev/null ;;
  shot)
    (( $# == 1 )) || die "usage: shot <out.png>"
    print "screendump /tmp/windows-vm-shot.ppm" | monitor
    sleep 1
    ssh $host "docker exec $ctr sh -c 'cat /tmp/windows-vm-shot.ppm && rm /tmp/windows-vm-shot.ppm'" | python3 -c '
import re, struct, sys, zlib
d = sys.stdin.buffer.read()
m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", d) or sys.exit("windows-vm: no screen dump")
w, h = int(m[1]), int(m[2]); px = d[m.end():]
raw = b"".join(b"\0" + px[y * w * 3:(y + 1) * w * 3] for y in range(h))
chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
open(sys.argv[1], "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                              + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
' $1
    print $1 ;;
  click)  (( $# == 2 )) || die "usage: click <x> <y>"; int $1; int $2; pointer $1 $2 click ;;
  scroll) (( $# == 3 )) || die "usage: scroll <x> <y> <n>"; int $1; int $2; int $3; pointer $1 $2 wheel $3 ;;
  keys)
    (( $# )) || die "usage: keys <key>..."
    for k; do [[ $k == [a-z0-9_.,/=-]## ]] || die "not a QEMU key name: $k"; done
    for k; do print "sendkey $k"; done | monitor ;;
  frame-key)
    [[ ${1:-} == (add|remove) ]] || die "usage: frame-key add|remove"
    pub=(${=$(vm_ps 'Get-Content (Join-Path $env:USERPROFILE ".ssh\id_ed25519_frame.pub")' | tr -d '\r')})
    [[ ${pub[1]:-} == ssh-ed25519 && ${pub[2]:-} == [A-Za-z0-9+/=]## ]] ||
      die "the VM has no Frame Control key yet: run Set Up Connection in the app first"
    # $1: add|remove, $2: the key's base64, $3: the exact line this script owns.
    ssh frame "sh -s -- $1 '$pub[2]' '$pub[1] $pub[2] $TAG'" <<'EOF'
f=~/.ssh/authorized_keys
if [ "$1" = add ]; then
  if grep -qxF "$3" "$f"; then exit 0; fi
  if grep -qF "$2" "$f"; then echo "the headset already trusts this key through another entry; left as it is"; exit 0; fi
  [ -z "$(tail -c 1 "$f")" ] || echo >> "$f"   # a last line without a newline would swallow ours
  echo "$3" >> "$f"
else
  t=$(mktemp "$f.XXXXXX") || exit 1
  grep -vxF "$3" "$f" > "$t"   # 0: lines left, 1: none left, more: couldn't read or write
  if [ $? -gt 1 ] || ! chmod 600 "$t" || ! mv "$t" "$f"; then
    rm -f "$t"; echo "couldn't rewrite $f; it's unchanged" >&2; exit 1
  fi
fi
EOF
    print "frame-key $1: done" ;;
  *) die "unknown command: $cmd (see the top of $0)" ;;
esac
