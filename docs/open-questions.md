# Open questions and on-device checks

Research as of 2026-09-25, eight days after the Frame's retail release
(2026-09-18). Most first-party detail comes from Valve's Steamworks developer
pages. Searches of Reddit and the Steam forums turned up **almost no
end-user reports** about SSH, desktop streaming, or macOS. Treat that as
"not documented yet", not "doesn't work".

## Verified on device (2026-09-25)

Checked over SSH from the Mac, read-only, on SteamOS 0.3.0 (`VARIANT_ID=vr`,
build 20260922.6101926, kernel 6.18, aarch64):

- **1–2.** Developer Mode + Set User Password gave working SSH with no terminal
  steps. `sshd` is enabled and active. The user is `steamos` (in `wheel`) and
  the hostname is `frame`.
- **3.** `frame.local` resolves from the Mac; `avahi-daemon` is active.
- **5.** `/etc/ssh/sshd_config` has `Include /etc/ssh/sshd_config.d/*.conf`.
  The existing drop-ins are `20-systemd-userdb.conf` and `99-archlinux.conf`, so
  `01-frame-keys-only.conf` would sort first as intended. (`--harden` itself
  hasn't been run.)
- **8.** The in-headset desktop is `kwin_wayland` + `plasmashell` nested
  inside gamescope (1280×800), with `XDG_RUNTIME_DIR=/run/user/1000/nested_plasma`,
  `WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:2` and a private D-Bus bus. SteamVR
  (`vrserver`, `vrcompositor`) and `xrdp` are running.
- **9.** `rsync`, `flatpak`, `python3`, `git`, `qdbus6` and `xrdp` are present.
  `wl-copy`, `xclip`, `xsel`, `kdeconnect-cli`, `tailscale`, `krfb` and `wayvnc`
  are **not** (Tailscale can be added in `~`; see [tailscale.md](tailscale.md)). `paste-to-frame.sh` now uses Klipper over D-Bus and round-trips
  text correctly.
- Flathub is already configured as a **system** remote; Chromium is the only
  installed Flatpak. `/` is 10 GB (42% used); `/home` is 929 GB.
- `push.sh` copied a test file with rsync.
- **10.** `install-apps.sh remmina --vnc-host <mac>.local` installed Remmina as
  a `--user` Flatpak over SSH and wrote the profile. The desktop's
  `XDG_DATA_DIRS` includes the user Flatpak exports, so it shows up in the menu.
  The Frame can reach the Mac's Screen Sharing port (5900).
- **11.** Answered 2026-09-27 (BUILD_ID 20260925.6191901, macOS 27.0): the
  pre-seeded profile connects and shows the Mac in its own panel. It asks for
  the Mac account login rather than the VNC password, needs scale-to-fit at
  Retina resolutions, and doesn't show the Mac cursor without
  `scripts/mac-cursor-ring.lua`. It's usable but noticeably laggy. See
  [streaming.md](streaming.md).

- **Panels.** An X11 window on gamescope's `:0` with its own `STEAM_GAME` id
  gets its own SteamVR overlay (`valve.steam.desktopgame.<id>`). Three were
  created side by side with `panel-on-frame.sh`. See [panels.md](panels.md).

Still open: 4, 6, 7, 12–15, 16 (off-LAN and after a reboot), 17–21.

## Check on the headset (in order)

1. **Is Developer Mode available on a retail unit?** Valve's pages are aimed at
   developers. Confirm that **Steam Settings → System → Enable Developer Mode**
   and **Developer → Set User Password** both exist on your OS channel (Stable
   vs Beta).
2. **Does SSH work straight after that, with no terminal steps?** From the Mac,
   run `nc -z frame.local 22`, then `./scripts/connect.sh`.
3. **Does `frame.local` resolve from the Mac (mDNS/Avahi)?** If not, use the IP
   and set up a DHCP reservation.
4. **Does SSH stay enabled after a reboot and after an OS update?** Also check
   that `~/.ssh/authorized_keys` survives an update.
5. **Is the `sshd_config.d` include present?** Check before `--harden`:
   `ssh frame 'grep -n Include /etc/ssh/sshd_config'`.
6. **What does Steam Link on macOS show when connected to `frame`?** Is it the
   VR view, a flat mirror, or the desktop? Does keyboard/mouse input reach the
   headset?
7. **Does the xrdp session work from Microsoft Windows App on macOS?** Valve
   only documents Windows Remote Desktop Connection. Is clipboard sync
   supported?
8. **What kind of session is the in-headset Linux desktop?** It could be a
   normal Plasma Wayland session (with a `wayland-*` socket in
   `/run/user/$(id -u)`), X11, or something nested in SteamVR. This decides
   whether `paste-to-frame.sh` works. `ssh frame 'ls /run/user/$(id -u); loginctl list-sessions'`.
9. **Are `wl-copy`, `xclip`, and `rsync` present on the image?**
   `ssh frame 'command -v wl-copy xclip rsync flatpak'`.
10. **Can Flatpaks be installed `--user` over SSH, and do they appear in the
    headset's desktop?** Test with `./scripts/install-apps.sh remmina`.
11. ~~**Remmina → macOS Screen Sharing**~~: answered 2026-09-27; see above
    and [streaming.md](streaming.md).
12. **Moonlight Flatpak (aarch64) + Sunshine on macOS:** VNC works but is
    noticeably laggy, so this is worth trying.
13. **KDE Connect**: is it preinstalled or installable on the Frame, and does
    it pair with KDE Connect for macOS?
14. **Bluetooth keyboard pairing** on the Frame, for the rare times you do need
    to type locally.
15. **ADB**: does `adb shell` over USB-C from a Mac (not just a Windows PC)
    reach the Linux side? Does USB power from the Mac cope?
16. ~~**Tailscale**~~: answered 2026-09-25. A userspace `tailscaled` in `~`
    runs as a lingering user service with no sudo; see [tailscale.md](tailscale.md).
    Still open: reaching the Frame from outside the home network, and the service
    starting after a reboot.
17. **Floating panels in the headset** (see [panels.md](panels.md)): panels
    from `panel-on-frame.sh` show up and take controller input (verified
    2026-09-27 with `mac-screen`). Still open: do they offer **Float in
    World** / **Move** / **Size**? Do floating positions survive closing and
    reopening the app, or a reboot?
18. **`LEPTON_NO_CLEANUP=1 %command%`** as Lepton Development's launch
    option: do ADB-installed apps survive closing and reopening it?
19. **Typing in Android apps:** Lepton has no IME installed. Does the SteamVR
    keyboard or a Bluetooth keyboard reach Android text fields, or does an
    F-Droid keyboard (installed and enabled with `ime enable`/`ime set`) work?
20. **F-Droid 2.0** (Compose 1.12): does it run? If so, the catalogue can
    install it instead of 1.17.2.

21. **Owned media player:** worn-headset comfort, audio quality/lip sync, long
    movies and 4K/8K decoding remain to check. Native spatial-photo container
    extraction, large/immersive splats and existing-panel theatre docking need
    further implementation. Remote eye isolation, short hardware decode and
    owned-overlay cleanup are verified; see [vr-video.md](vr-video.md).

## Verified 2026-09-27

- **Recovery images exist** for the Frame at
  `https://steamdeck-images.steamos.cloud/recovery/`; the root filesystem inside
  is btrfs and runs, as a userland, on ARM64 Linux. See
  [recovery-and-images.md](recovery-and-images.md).
- **Frame Control's server runs on the Frame itself** (the iPhone app does
  this), including headset capture, 31 fps live video and file uploads. See
  [iphone.md](iphone.md).
- **Password pairing and `sudo -S`** work against the recovery image's own
  sshd and sudo (not yet against the headset, whose password we don't hold).

## Still open (2026-09-27)

- Does `podman exec <lepton container> /system/bin/sh -c 'wm size'` change an
  instance's display the way `adb shell wm size` does?
- Can the recovery image, or its kernel, boot in a VM at all?
- Does a real sleep, restart or shut down from the iPhone app work (via
  `sudo -S systemctl`)?
- The Mac EDL flashing script in `~/Downloads/steam-frame-recovery/` hasn't
  been run against a Frame.

## Mac in the headset (2026-09-28)

The test pattern streams to the Frame as its own panel at about 60 fps
(verified, build 20260925.6191901; see
[mac-in-headset.md](mac-in-headset.md#checked-so-far-2026-09-28)). Still to
check in the headset:

- Laser clicks, drags and thumbstick scrolling in a viewer panel.
- Real window capture and input once Screen Recording and Accessibility are
  granted to Frame Control.
- Keys from SteamVR's on-screen keyboard.
- Whether Flathub Chromium decodes H.264 (otherwise use Compatible).

## Unconfirmed claims made in these docs

- `/home` and `/etc` persist across Frame OS updates. This is inferred from
  Steam Deck behaviour.
- Steam Remote Play with a Mac as host is broken. That's based on community
  reports, not tested with the Frame.
- `connect.sh --harden`, `serve-bootstrap.sh` and
  `bootstrap-on-frame.sh` haven't run against real hardware.
