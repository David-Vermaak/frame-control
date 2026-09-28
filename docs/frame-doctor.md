# Frame doctor runbook

Checks and fixes for a Frame that's unreachable, crashing, or whose Steam,
SteamVR, Lepton or panels misbehave. A future `scripts/frame-doctor.sh` should
run the checks in section order, print OK, WARN or BROKEN for each, and apply
only the fixes marked **safe**. Anything marked **ask** needs the user's OK,
and anything marked **user** needs a hand on the headset.

Sources are the Frame's own journal and `coredumpctl` history (boots from
2026-09-25 to 2026-09-28) and this repo's docs. Each entry cites where it came
from. Dates are when a fact was seen. BUILD_IDs were 20260922.6101926 until
2026-09-26 and 20260925.6191901 after.

## Never do these

- `modprobe -r ath12k` on a wedged Wi-Fi chip. It oopsed the kernel on
  2026-09-28 and caused the displays-broken, Steam-damaged boot in section 3.
- Leave WoWLAN armed. The next sleep breaks Wi-Fi until reboot (section 2).
- Suspend the Frame from a script. Nothing can wake it remotely (section 6).
- Let the SteamOS health checks count up to their repair. The SteamVR one
  re-extracts Steam at 3 failures and tries to switch OS slots at 4 (section 4).
- Kill `gamescope` to stop a gamescope crash loop. Kill the orphaned SteamVR
  processes instead (section 3).
- Write the sudo password to disk or logs.
- Leave `power.pauseCompositorOnStandby` or `power.turnOffScreensTimeout`
  changed after testing (section 3).
- Force a power-off (holding Power) or reset while Steam is extracting or
  repairing. That's how files got truncated on 2026-09-28. Use an orderly
  `systemctl reboot`/`poweroff` or the power menu. Holding Power is for an
  unresponsive Frame, with the user's involvement.
- Run long diagnostics while a Steam or SteamVR restart loop is live without
  freezing the health-check trackers first (section 4). The repair threshold is
  3 SteamVR failures, and a loop reaches it in under a minute.

## 0. Reaching the Frame

| Path | How | Works when |
|---|---|---|
| Tailscale | `ssh frame` (`frame.<tailnet>.ts.net`) | Wi-Fi up, and Tailscale on the Mac and the Frame |
| LAN | `ssh -o HostName=192.168.1.237 -o HostKeyAlias=frame.<tailnet>.ts.net frame`, or `frame.local` | Wi-Fi up. The alias avoids "Host key verification failed" |
| USB-C | Same, with `HostName=10.86.200.233` | Cable to the Mac, even with Wi-Fi dead. The Mac gets `en9` "Steam Frame" 10.86.200.234/29 (`networksetup -listallhardwareports`) |
| ADB over USB-C | `adb -s frame shell` | SSH refused, for example after Developer Mode was lost ([how-the-frame-works.md](how-the-frame-works.md), boot-loop row) |

- **Asleep means off the network (verified 2026-09-27, unreachable for about
  2.5 h).** Every path times out and nothing remote wakes it
  (section 6). **user**: press power. `tailscale status | grep frame` on the
  Mac shows "offline, last seen N ago".
- **`frame` alias doesn't resolve.** The Mac's Tailscale is off. Use
  `frame.local` ([tailscale.md](tailscale.md)). Bare `frame` doesn't resolve on
  macOS. Check with `dns-sd -G v4 frame.local` ([ssh.md](ssh.md)).
- **Pairing answers `403 "please put the Steam client in pairing mode"`.**
  **user**: Steam, then Settings → Developer → Pair new host. `connect.sh`
  retries for 2 min ([ssh.md](ssh.md)).
- **iPhone app can't use devkit pairing.** It only installs an RSA key, and
  Citadel signs RSA with SHA-1, which OpenSSH 9.7 rejects. Use ed25519 and
  password pairing instead ([ssh.md](ssh.md), 2026-09-27).
- **Locked out after `connect.sh --harden`.** Undo with `sudo rm
  /etc/ssh/sshd_config.d/01-frame-keys-only.conf && sudo systemctl reload sshd`
  (**ask**, over USB-C or ADB) ([ssh.md](ssh.md)).
- **No SSH at all (Developer Mode off).** Run `scripts/serve-bootstrap.sh`, and
  the **user** types `curl -fsS mac.local:8765|bash` in Konsole. Stop the
  server afterwards, because it's plain HTTP ([ssh.md](ssh.md)).
- **Tailscale exposes every loopback port** (8080 Steam DevTools, 5555
  unauthenticated ADB, 27062, 3389) to the tailnet. Check read-only with
  `~/.local/bin/tailscale debug prefs | grep ShieldsUp` (the CLI isn't on `PATH`; verified 2026-09-28) and the tailnet ACLs. Report it
  as a WARN. `~/.local/bin/tailscale set --shields-up` is a mitigation, not a check, and it
  also blocks inbound SSH over Tailscale, so it's **ask**, and only with
  another way in available ([tailscale.md](tailscale.md)).
- **sudo:** `printf '%s\n' "$PW" | ssh frame 'sudo -S -p "" …'`. It's the
  password the user set on the Frame.

## 1. Boot and crash history

```sh
ssh frame 'uptime; journalctl --list-boots --no-pager | tail -n 6'
ssh frame 'journalctl -b -1 -k --no-pager -q | grep -aE "Unable to handle kernel|Internal error|Kernel panic" | tail -n 3'
ssh frame 'coredumpctl list --no-pager --since -1d'
```

- **Kernel oops in the previous boot.** Report "oops observed". An oops alone
  doesn't prove a reset, because Linux can keep running after one. Classify the
  reset as unclean only if the oops is among the last lines of that boot and no
  shutdown lines follow
  (`journalctl -b -1 -q -n 30 | grep -aE "systemd-shutdown|Reached target.*(Reboot|Power)"`
  is empty). On 2026-09-28 the oops was the last thing logged at 20:57:53. After
  an unclean reset, check sections 3 and 4 closely.
- **A boot ending with no shutdown lines and no errors.** On 2026-09-26 there
  were five boots of 0–12 min like this (−12, −9, −8, −7, −5), with nothing
  failing beforehand. They were probably hard power-offs during setup. Treat
  them as unexplained, not as crashes.
- **Crash signatures seen so far** (all `coredumpctl`, UID 1000):

| When | What crashed | Cause | Section |
|---|---|---|---|
| 09-25 21:02–21:03 | vrcompositor SEGV, steamwebhelper SEGV, then Android composer, surfaceflinger and gamescope ABRT | Lepton crash cascade. Two `pasta` processes were both failing to listen on port 16385 just before | 7 |
| 09-25 22:30–22:42 | `app_process64` ×3 | Android apps during APK testing | 7 |
| 09-25 23:20 | `ffmpeg` | hardware H.264 encoder | 10 |
| 09-26 13:56–22:45, 09-27 09:51 | `chromium-xr/chrome` ×16 | Chromium XR (panels, Mac view) | 8 |
| 09-26 21:11–21:49 | XRService ABRT ×9, vrcompositor SEGV ×5, gamescope ABRT ×4 | leftover SteamVR processes from a failed start (29 Steam restarts, 31 SteamVR failures that boot) | 3 |
| 09-28 16:27–17:49 | `app_process64` ×4 | Android runtime amid `binder_user_error` floods | 7 |
| 09-28 17:01 | `kdeconnectd` | SMS plugin during device teardown | 9 |
| 09-28 20:58–21:39 | vrcompositor SEGV ×10, XRService ×15, steamwebhelper ×2 | broken displays after a kernel oops | 3 |

Per-boot counters a doctor should print:

```sh
ssh frame 'for b in 0 -1; do
  k=$(journalctl -b $b -k -q) || { echo "boot $b: journal unreadable"; continue; }
  u=$(journalctl -b $b --user -u steam.service -q) || { echo "boot $b: user journal unreadable"; continue; }
  s=$(journalctl -b $b -q) || { echo "boot $b: journal unreadable"; continue; }
  echo "boot $b dsi=$(grep -ac "wait for video done" <<<"$k") steam_restarts=$(grep -ac "Scheduled restart" <<<"$u") steamvr_fail=$(grep -ac "steamvr.service: Failed" <<<"$s")"
done'
```

Report an unreadable journal as unknown, not as zero. What matters is
whether the counts are **still rising**, so run it twice a minute apart. One
or two SteamVR start failures around boot are normal
([how-the-frame-works.md](how-the-frame-works.md), boot-loop row). Healthy
boots on 2026-09-28 were 0 / 0 / 0. The 2026-09-26 21:22 boot reached
0 / 29 / 31 (leftover processes), and the 2026-09-28 20:58 boot reached
424 / 61 / 62 (broken displays), both rising every ~15 s.

## 2. Wi-Fi

| Check | Healthy | Broken |
|---|---|---|
| `nmcli -t d \| grep ^wlan0` | `wlan0:wifi:connected:…` | `wlan0:wifi:unavailable:` |
| `journalctl -b -k \| grep -a ath12k` | none, or a few at boot | `failed to wakeup from wow: -110`, `Resuming from non M3 state (RESET)`, `WMI_PDEV_SET_PARAM_CMDID timeout`, `fail to start mac operations` |
| `iw phy phy0 wowlan show` | `WoWLAN is disabled.` | `wake up on magic packet` |

- **WoWLAN armed (safe).** Disarm it by UUID, because the user may have made
  same-name duplicates. `default` means "use NetworkManager's global
  `wifi.wake-on-wlan`". The Frame sets none (checked 2026-09-28), so it falls
  back to `ignore`, which leaves the chip untouched and doesn't clear an armed
  chip. `0` disarms it:
  ```sh
  set -e
  U=$(nmcli -t -f UUID,DEVICE c show --active | awk -F: '$2=="wlan0"{print $1}')
  [ -n "$U" ] || { echo "no active connection on wlan0"; exit 1; }
  nmcli -g 802-11-wireless.wake-on-wlan c show "$U"   # record the old value
  systemd-run --user --wait --pipe -q nmcli c modify "$U" 802-11-wireless.wake-on-wlan 0
  systemd-run --user --wait --pipe -q nmcli device modify wlan0 802-11-wireless.wake-on-wlan 0
  iw phy phy0 wowlan show | grep -q "WoWLAN is disabled" || { echo "still armed"; exit 1; }
  ```
  Use the **active** connection on wlan0, because there can be same-name
  duplicates. `c modify` saves the setting. `device modify` changes only
  WoWLAN on the live device, unlike `device reapply`, which would also apply
  any other saved changes such as IP or DNS. Tested 2026-09-28: Wi-Fi stayed
  connected. NetworkManager only allows the
  modify under `systemd-run --user`. From SSH it's `auth`. Leave the profile
  at `0`, since that stays safe even if a global `wifi.wake-on-wlan` is added
  later. Setting the recorded old value back is **ask**. On 2026-09-28 the
  profile was set back to `default` by hand. If the Wi-Fi is `unavailable`,
  there's no active connection, so this has to wait for the reboot, and then
  arming comes from the profile.
- **`unavailable` after resume (user/ask).** Do a **clean** reboot: power
  menu, or `sudo systemctl reboot` over USB-C. Never reload the module.
- **Duplicate "ThisIsTheWifi" profiles.** Ones with `TIMESTAMP-REAL` `never`
  are unused. Deleting them is **ask**.

## 3. Displays, SteamVR, gamescope

| Check | Healthy | Broken |
|---|---|---|
| `journalctl -b -k \| grep -ac "wait for video done"` | `0` | hundreds (`msm_dsi ae94000.dsi / ae96000.dsi`) |
| `coredumpctl list vrcompositor --since -10min` | none | SEGV every ~15 s |
| `grep -a "failed to wait for present" ~/.local/share/Steam/logs/vrcompositor.txt` | none recent | `WaitForPendingPresent: failed to wait for present` |
| `journalctl -b --user -u steamvr.service \| grep -a "left-over process"` | none | `Found left-over process … (vrserver) … (vrcompositor) in control group` |
| journal `gamescope` | quiet | `rendervulkan.cpp:2181 … Assertion '!modifiers.empty()'` about once a second |

- **Broken displays (DSI timeouts).** The chain is: the GPU can't present,
  vrcompositor SEGVs on its first frame, `steamvr.service` fails and stops the
  gamescope VR session, and gamescope and Steam get SIGKILLed. The user sees
  "There was an issue launching Steam". Fix (**ask/user**): a **clean**
  reboot. An unclean reset after a kernel oops caused it, and the clean reboot
  had 0 DSI errors (2026-09-28). Don't touch Steam while this is happening.
- **Leftover SteamVR processes (2026-09-26 21:22 boot).** A first
  `steamvr.service` start failed on `dependency`, its vrserver, XRService and
  vrcompositor kept running, and each restart crashed against them. The same
  fix as the next item applies, with the same guard.
- **gamescope crash loop on `!modifiers.empty()`** (verified 2026-09-25,
  [apks.md](apks.md)). gamescope keeps attaching to SteamVR processes orphaned
  from a dead session. The broad fix is
  `for p in vrdashboard vrcompositor vrserver; do pkill -TERM -x $p; done`,
  and it recovers within about a minute. That kills **every** matching
  process, including a working session, so it's always **ask**. A doctor may
  signal automatically only **individually verified stale PIDs**, and only
  when **all** of these hold:
  - The loop is live: new vrcompositor/gamescope crashes in the last 2
    minutes, and `NRestarts` rising between two reads.
  - The process started before the current `steamvr.service` main process:
    compare `ps -o pid,lstart,args -C vrserver,vrcompositor,vrdashboard`
    with `systemctl --user show steamvr.service -p ExecMainStartTimestamp`.
  - The journal ties it to the failed run:
    `Found left-over process <pid> (…) in control group`.

  Re-read `/proc/<pid>/stat` start time and `comm` just before signalling, and
  signal by number, never by name. A process that's merely outside
  `steamvr.service`'s cgroup could be a legitimate launch, so that's **ask**.
- **Standby test settings left on.** Check that `vrcmd --get-settings`
  (or `~/.config/openvr/config/steamvr.vrsettings`) shows
  `power.pauseCompositorOnStandby` = 1 and `power.turnOffScreensTimeout` = 5.
  If they differ, report a WARN and leave them alone (**ask**), since the user
  may want them. If a doctor run changes them for a test, it must snapshot both
  values first, including whether they were set in `steamvr.vrsettings` at all.
  Afterwards it restores exactly those values, removing the keys if they were
  absent, rather than the defaults 1 and 5.
  The bool setter needs `1`/`0`, not `true`
  ([how-the-frame-works.md](how-the-frame-works.md)).
- **Dashboard open over an app** (`visible-blurred` just means it's open).
  Run `SteamClient.OpenVR.VROverlay.HideDashboard()` over CDP on port 8080
  only when the doctor itself is driving an app test. Otherwise it's **ask**,
  because the user may have opened it.
- **Steam launch stuck in standby** at `ShowInterstitials`/`CreatingProcess`
  (`console_log.txt`). Run `SteamClient.Apps.ContinueGameAction(<action id>,
  "<appid>", "<task>")` over CDP.

## 4. Steam client and the SteamOS health checks

| Check | Healthy | Broken |
|---|---|---|
| `systemctl --user show steam.service -p NRestarts` | `0` or stable | climbing every ~15 s |
| `tail -n 40 ~/.local/share/Steam/logs/connection_log.txt \| grep -a "Logged On"` | `[Logged On, …] [U:1:<id>]` | only `[Logged Off, 0, 0] [U:1:0]` |
| `grep -a BVerifyInstalledFiles ~/.local/share/Steam/logs/steam_output.log` | none | `<file> is N bytes, expected M`, `bad symlink …` |
| last line of `steam_output.log` | client running | `Installing update...` or `Extracting package...` for minutes |
| `pgrep -af child-update-ui` + `/proc/<pid>/wchan` | none | `drm_syncobj_array_wait_timeout` |
| `cat /run/user/1000/steam{,vr}-short-session-tracker; ls -l` those files | empty | `frog…` / `frog:glasses:…` building up |

Check section 3 first. If the displays are broken, Steam can't get past its
first frame, whatever the files look like.

**The two health checks** (read from `/usr/share/deckard/`, 2026-09-28):

- `steam-health-check` (run by `steam.service`) appends `frog` to
  `steam-short-session-tracker` for each run that fails in under 120 s or lasts
  under 5 s. At 5 it runs `do_repair`. On BUILD_ID 20260925.6191901 the
  script deletes `~/.steam` (keeping `registry.vdf`) and then either extracts
  `/usr/lib/steam/steam.tar.zst` (705 MB) over `~/.local/share/Steam` (the
  "unpacked" install this Frame has) or, on an overlay install, deletes the
  upper-dir files that shadow `/usr/local/steam`. Then it touches
  `.install-complete`. It also repairs at **every Steam start** if
  `.install-complete` is missing, whatever the counter says.
- `steamvr-health-check` (run by `steamvr.service`) appends `frog:glasses:`
  for each failed or under-10-s SteamVR run. At 3 it runs `steam-health-check
  --repair-now`. At 4 it also runs `steamos-bootconf set-mode reboot-other`,
  which fails as non-root.
- **What a repair erases isn't consistent across notes.** On 2026-09-26
  (BUILD_ID 20260922.6101926) the boot-loop row in
  [how-the-frame-works.md](how-the-frame-works.md) records that all of
  `~/.local/share/Steam` was deleted, including games, login and Developer
  Mode. On 2026-09-28 the scripts above only extract over it, a repair ran at
  21:13 (`.install-complete` mtime), and the login survived. Treat any repair
  as possibly destructive. Before a restart that could trigger one, check that
  `.install-complete` exists.
- **Stop them counting while you fix the cause (safe, resets at boot).** Do
  this **first**, right after connecting, if `NRestarts` or either tracker is
  rising, before any long checks:
  ```sh
  rc=0
  for f in /run/user/1000/steam-short-session-tracker /run/user/1000/steamvr-short-session-tracker; do
    { [ -e "$f" ] || : > "$f"; } && chmod u+w "$f" && : > "$f" && chmod 444 "$f" || rc=1
    # verify: empty and not writable
    [ -e "$f" ] && [ ! -s "$f" ] && [ ! -w "$f" ] && echo "frozen $f" || { echo "NOT frozen $f"; rc=1; }
  done
  exit $rc
  ```
  A doctor must stop and not restart Steam or SteamVR unless this exits 0.
  It's idempotent, so run it on files that are already 444. Both were
  already 444 on 2026-09-28, applied by an earlier session. This only stops
  the **counting**. The start-time repair when `.install-complete` is missing
  still runs. Re-apply after every reboot while the loop's cause is unfixed.

Fixes:

- **Verify files yourself (safe, read-only).** The record is
  `~/.local/share/Steam/package/steam_client_<branch>_linuxarm64.installed`,
  with lines of `path,size;mtime;crc32` (size `-1` is a directory). Compare
  sizes and `zlib.crc32` (13,518 files on 2026-09-28). `steam_output.log` is
  rewritten on every launch, so copy it before the next restart. `bad symlink`
  reports taken mid-extraction are transient.
- **Truncated files (ask).** Restart Steam (`systemctl --user restart
  steam.service`), and it re-verifies and re-extracts from `package/`.
  Preconditions:
  - The displays are healthy.
  - The trackers are frozen.
  - `.install-complete` exists.
  - The updater is idle: the `steam_output.log` tail hasn't changed for 60 s
    and the steam process isn't writing (`/proc/<pid>/io` `write_bytes` is
    steady).
  - No game or app is running.

  Re-verify afterwards.
- **Updater deadlocked on the update UI.** Kill only the `-child-update-ui`
  process, and the install continues (worked 2026-09-26). On 2026-09-28 it
  was followed by a truncated `steamui.so`, so re-verify afterwards. If the
  deadlock came from broken displays, fix those first.
- **Stale pending install (ask, with backup).** `package/steam_client_<branch>_linuxarm64`
  with no extension means an install is pending. Only act when all of these hold:
  - `cmp` shows it's identical to `.manifest`.
  - The verify is clean.
  - The updater is idle (as above).

  Then stop Steam, move it to `~/.cache/frame-control/…pending-backup`, and
  start Steam. The log should
  show `Nothing to do`, then `Verification complete`, then webhelpers.
- **Heavy repair (ask).** `steam-health-check --repair-now`, or the boot
  menu's `Repair Steam Installation` (section 12).
- **Dead ends (don't repeat).**
  - `STEAM_EXTRA_ARGS=-no-child-update-ui` still draws GLX in-process and
    blocks.
  - With `DISPLAY` unset, Steam exits ("XOpenDisplay failed"), with no text
    fallback.
  - Xvfb has no GLX visual here.
- Steam's launch path is `steam.service` → `/usr/share/deckard/select_steam.sh
  RUNSTEAM.sh`. Runtime drop-ins in `/run/user/1000/systemd/user/steam.service.d/`
  are cleared at reboot. Remove any you add.
- **`create-shortcut` refuses ids with hyphens** (`missing/invalid arguments`).
  Ids must match `^[A-Za-z_][A-Za-z0-9_.]+$`. It reports "Steam client is not
  running" when Steam is down, and re-running finishes the install without a
  re-upload ([sideloading.md](sideloading.md)).

## 5. Idle sleep and keep-awake

- **Frame slept mid-task.** SSH doesn't count as activity, and Steam's idle
  timer (`system_idle_suspend_ac_sec` 3600, `…_battery_sec` 900) suspends it.
  The journal shows `Switching to power state: [ k_ESystemPowerState_Sleep ]`.
  Fix (**safe**): `scripts/keep-awake.sh on` before long work and `off` after.
  It uses one shared unit and one saved-settings file. So a doctor records
  whether `fc-keep-awake` was already active, and runs `off` only if it was
  the one that turned it on. Otherwise it releases another task's lock and
  restores that task's saved timers.
  It sets both timers to 0 and holds the `fc-keep-awake` user-unit inhibitor.
  A plain SSH-session inhibitor is refused.
- **Check:** `systemctl --user is-active fc-keep-awake`,
  `systemd-inhibit --list | grep "Frame Control"`. WARN if it's held with no
  agent working, since that drains the battery on battery power.
- **Charging shows "Discharging" at ~0 W while full on a charger.** This is a
  reporting quirk. Treat under 0.5 W on a charger as "not charging"
  ([how-the-frame-works.md](how-the-frame-works.md)).

## 6. Remote wake

It doesn't work on this build. WoWLAN arms, but the WCN7850 is reset in both
`deep` and `s2idle`, packets don't wake it, and Wi-Fi is dead after resume.
Tested 2026-09-28. Details are in [how-the-frame-works.md](how-the-frame-works.md)
and [open-questions.md](open-questions.md). Doctor checks: `cat
/sys/power/mem_sleep` should read `s2idle [deep]` (resets at boot), and WoWLAN
should be disabled.

## 7. Lepton (Android)

| Check | Broken sign |
|---|---|
| `podman ps --format '{{.Names}} {{.Ports}}'` | two containers with the same name or instance, or both bound to the same host port. Several instances with different ports are normal ([apks.md](apks.md)) |
| journal `pasta` | `Listen failed for HOST TCP port 0.0.0.0/16385: Address already in use` repeating |
| journal | `android.hardware.graphics.composer@2.1-service` or `surfaceflinger` aborts right after an app crash |
| `dmesg` / journal | floods of `binder_user_error: N callbacks suppressed` near `app_process64` crashes |
| journal | `Clearing baked app data due to non steamlaunch container` |

- **Duplicate Lepton containers or port clash (2026-09-25 21:01).** Two
  `pasta` instances fought over 16385, and a minute later the compositor,
  webhelper, Android composer, surfaceflinger and gamescope crashed together.
  Fix (**ask**): find the two instances that share the port (`podman ps`,
  `ss -ltnp | grep 16385`) and stop only the duplicate. Leave other instances
  running.
- **Lepton's graphics HAL aborts after an app crash, taking the container down**
  (3 times on 2026-09-25). It's intermittent, so retry ([apks.md](apks.md)).
  Then check section 3 for a gamescope loop.
- **All ADB-installed apps gone.** Lepton Development wipes its data whenever
  it exits outside a Steam launch. Use `install-apk.sh` (a per-app Steam
  launch, whose data survives), or the launch option `LEPTON_NO_CLEANUP=1
  %command%` (inferred) ([apks.md](apks.md)).
- **App dies on first file write with `ENOENT`.** Its `STEAM_COMPAT_DATA_PATH`
  is outside `~/.local/share/Steam`. Use `steamapps/compatdata/<id>`.
- **Lepton won't start outside Steam.** Set `IS_PARENT=true` and use `setsid
  --wait`. "unbound variable" means `STEAM_COMPAT_SHADER_PATH` is unset
  ([apks.md](apks.md)).
- **App compatibility, not a fault** ([apks.md](apks.md)):
  - Compose older than 1.11, SDL2/Kivy and Godot 4.3 crash on the missing
    clipboard service.
  - `INSTALL_FAILED_OLDER_SDK` means minSdk is over 30.
  - `INSTALL_FAILED_NO_MATCHING_ABIS` means there's no arm64 build.
  - `monkey` returning `-5` means you should launch the activity directly.
    `--brief` prints a metadata line first, so take the last line:
    `adb -s $S shell am start -W -n "$(adb -s $S shell cmd package resolve-activity --brief -c android.intent.category.LAUNCHER <pkg> | tail -n 1)"`.

## 8. Chromium XR (panels, Mac view, WebXR)

- **16 crashes on 2026-09-26/27.** The logs showed `Failed to create a
  temporary file for memory-mapping: No such process (3)`, then `Received
  signal 11 SEGV_MAPERR`. The cause isn't known yet. Check with
  `coredumpctl list chrome --since -1d`.
- **Zygote crash about 30 s after a Steam-launched start.** Steam's
  `gameoverlayrenderer.so` is in `LD_PRELOAD`, and the launcher strips it
  (verified 2026-09-27, [webxr-chromium.md](webxr-chromium.md)). Check that
  the running chrome's `/proc/<pid>/environ` has no `gameoverlayrenderer`.
- **XR process seccomp crash (syscall 209) or `VRInitError_Init_Internal`.**
  The launcher runs with `--disable-seccomp-filter-sandbox`. Use that profile
  only for VR sites ([webxr-chromium.md](webxr-chromium.md)).
- **Mac view kept working when Steam was down** (2026-09-28). It's a separate
  Chromium talking to the Mac over the LAN. It's a useful way in when Steam is
  broken, but it's killed by any reboot and needs relaunching.

## 9. KDE Connect

- **`kdeconnectd` crashed on 2026-09-28 17:01** in `kdeconnect_sms.so` under
  `Device::~Device` (device teardown). Check whether it's still running with
  `pgrep -f frame-control/kdeconnect/root/usr/lib/kdeconnectd`. Fix
  (**safe**): restart it the way this repo launches it. A doctor should
  report a missing daemon rather than guess.

## 10. Streaming and capture

- **`ffmpeg` crash with `h264_v4l2m2m`** (hardware encoder, 2026-09-25/26).
  Use `libx264 -preset ultrafast -tune zerolatency`
  ([how-the-frame-works.md](how-the-frame-works.md)).
- **`vrcmd --screenshot` writes nothing.** Use `ui/frame_vrshot.py`
  (`IVRScreenshots`).
- **No Mac cursor in the VNC mirror.** Load `scripts/mac-cursor-ring.lua` in
  Hammerspoon on the Mac (`dofile(".../scripts/mac-cursor-ring.lua")` in
  `~/.hammerspoon/init.lua`). It isn't a standalone script. Toggle it with
  ctrl+alt+cmd+M.
- **Remmina asks for the Mac login password.** macOS offers RFB type 30
  first. Seed the password with `--update-profile … --set-option password`
  ([streaming.md](streaming.md)).

## 11. Panels

- **Window stays on the default panel.** Run one `panel-on-frame.sh` at a
  time. Tag windows by hand with `DISPLAY=:0 xprop -id <win> -f STEAM_GAME 32c
  -set STEAM_GAME <id>` ([panels.md](panels.md)).
- **Single-instance apps** (Remmina, KDE). Close them in Plasma first.
- **Wayland-only apps** can't be floated this way.
- **Dragging selects instead of scrolling.** That's by design for tagged
  windows (laser mode).
- **`Failed to get app info`** for a made-up id is benign.

## 12. Boot loop, recovery, re-image

Work down this list, least destructive first ([recovery-and-images.md](recovery-and-images.md)).
The boot menu is inferred from Valve's docs and hasn't been tried on this Frame.

1. **If the Frame is reachable, freeze the health-check trackers first and
   verify them** (section 4), then diagnose. A reboot clears the freeze and
   restarts the failing services, and 3 SteamVR failures trigger a repair.
2. **Clean reboot** only when the diagnosed fault needs one (broken displays,
   dead Wi-Fi). Straight after reconnecting, freeze and verify the trackers
   again before anything else.
3. **Boot menu (user):** shut down cleanly if the Frame responds. Hold Power
   ~10 s until the LED is off only if it doesn't, and never while Steam is
   extracting or repairing. Then power on holding **AUX** (top button). Choose `Previous` (the other A/B slot, keeps data),
   then try `Repair Steam Installation`.
4. **`Erase User Data`** wipes `~`: SSH keys, Tailscale, Flatpaks and setup
   (**ask**).
5. **Re-image** with `steamframe-oobe-repair-<build>` over USB or cable/EDL
   (`qdl`). Copies are in `~/Downloads/steam-frame-recovery/` (**ask**).

## What a doctor script should do

1. Find a path (Tailscale, then LAN, then USB), and report which one worked.
2. Print uptime, the last boots, and whether the previous boot ended in an
   oops.
3. Run the read-only checks in sections 2–11 and print one line each: OK,
   WARN or BROKEN.
4. **Order matters.** If a restart loop is live (`NRestarts` or a tracker
   rising between two reads a few seconds apart), freeze the trackers
   **before** any other check. Then fix displays before Steam. After any
   reboot, check the trackers again, because they reset.
5. Apply only **safe** fixes, printing each command. For reboots, deleting
   profiles, heavy repairs and anything touching a user profile: print the
   fix and ask.
6. Never do anything under "Never do these".
7. Re-run the checks after any fix and report the before and after.
8. The fake-Frame harness (`fakeframe-ctl sleep|disk-full|sshd|devkit-service|keys`,
   [testing.md](testing.md)) can exercise the unreachable, disk-full and
   no-SSH branches without a headset.

## Incident 2026-09-28

| Time | What happened |
|---|---|
| 19:36 | WoWLAN armed, `deep` suspend, magic packets sent. No wake. Power-button resume: chip in MHI RESET, Wi-Fi dead. User restarted. |
| 20:10 | WoWLAN disarmed. Clean boot, no DSI errors. |
| 20:29 | `s2idle` via sudo, WoWLAN re-armed, suspend. No wake, same chip reset, Wi-Fi `unavailable`. |
| 20:57:53 | Over USB-C: `modprobe -r ath12k`, and the **kernel oopsed** and the Frame reset itself. |
| 20:58 | Boot with `steamclient.so` truncated (18.6 of 50.3 MB) and DSI timeouts from +28 s. Steam "couldn't connect", then "There was an issue launching Steam". Mac view still worked. |
| 21:00–21:13 | Steam re-extracts and hangs on "Installing update..." (update UI stuck on the GPU). Killing the UI child let it continue, and `steamui.so` was later found truncated. The health-check repair ran at 21:13. |
| 21:28 | Own CRC check: all 13,518 files OK. |
| 21:30 | Moved aside the identical pending manifest. Steam reached login, then died every ~15 s: vrcompositor SEGV on DSI timeouts. |
| 21:39 | User did a clean reboot. 0 DSI errors, Steam logged on 21:40:30, NRestarts 0. |
