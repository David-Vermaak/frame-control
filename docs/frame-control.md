# Frame Control in detail

What each part of the app does, how it works, and what has been checked on a
real Frame. For installing it, see the [README](../README.md#install).

As of 2026-09-25 no other desktop app manages the Frame end to end.
[Stream Frame](https://streamframe.app/) (macOS 14+, free) records and screenshots
the headset over SSH. [FrameDrop](https://framedropvr.com) sideloads but is
Windows-only. Steam Link views the headset.

You can also run the same UI in a browser without the app, from a checkout:

```sh
./scripts/frame-ui.sh        # macOS: opens http://127.0.0.1:47810 in its own window
python3 ui/server.py         # anywhere: then open http://127.0.0.1:47810
```

## Features

The window has four tabs: **Home** (headset view, status, screenshots),
**Games** (installed games, sideloaded titles, getting games), **Android** (apps,
the catalogue, display settings, reports) and **Tools** (sending files and text,
Flatpaks, remote and power). Keys 1–4 switch between them. Files can be dropped
anywhere in the window. When the Frame can't be reached, one banner says why in
plain words and the app retries every few seconds, filling everything in once it
answers. Flatpak and Android installs run in the background; the bottom bar
counts them while they run.

- **Headset view**: what the lenses show, as SteamVR composites it (the room,
  floating panels, dashboard and controllers). Shows the left eye, like pointing
  a camera into one lens, or both eyes, as a single shot; saves as PNG. **Live**
  is 720p video at about 30 fps: `ffmpeg` on the Frame encodes SteamVR's
  headset-view device (`/dev/video99`) to H.264 over SSH, and the page decodes
  it with WebCodecs. Live video is one eye; Capture still gets both. The viewer
  fits the whole frame; zoom with − / + (or scroll, or double-click), drag to
  pan, `0` to fit, `F` for full screen. Capture uses OpenVR's `IVRScreenshots`
  API through Python `ctypes` (`ui/frame_vrshot.py`). Nothing extra is
  installed on the Frame (SteamOS ships `ffmpeg`). **Desktop panel** captures
  gamescope's flat layer instead.
- **Screenshots** you take in the headset with Steam's shortcut: browse them and
  save them to `~/Pictures/SteamFrame`.
- **Battery** with charging state: charge rate in watts, time to full or empty,
  charger type and wattage (for example USB-C PD 20 W), and battery temperature.
- **Status**: storage, memory, temperature, Wi-Fi, uptime, and whether SteamVR,
  the desktop, Lepton and xrdp are running.
- **Library** shelf with Steam cover art and a Play button (`steam://rungameid`).
- **Get games**: every game you own with its Steam Frame rating (Verified,
  Playable, Unsupported, Unknown). Install on Frame downloads it to the headset
  with live progress. Search the Steam store with prices and Frame ratings; Buy
  opens the store page in your browser, or Store on Frame opens it in the
  headset. It drives the Frame's own Steam client through its DevTools port;
  see [steam-games.md](steam-games.md).
- **Volume** and mute (`wpctl`).
- **Android apps**: search about 4,500 F-Droid apps rated for the Frame, install
  one with a click as its own Lepton instance (it keeps its data and shows in the
  Steam library), then launch, stop, test or remove it. **Report an APK** records
  whether any APK worked (F-Droid or not: pick a file, type a package, or use an
  installed app). Your reports are saved on your computer and change the verdicts
  you see. With **Share compatibility results** on (Privacy & updates), they also
  go to the shared database ([privacy.md](privacy.md),
  [compat-db/README.md](../compat-db/README.md)). A failed install records
  itself when the APK was the problem, and after an install the app offers a
  20-second test. Uses the app's bundled `adb`, or yours if you have one.
- **Android display**: pick a running Lepton instance (by the app in it) and set
  its resolution (Native 1920×1080, or Sharp 2560×1440 with density scaled to
  match), UI scale (Smaller / Default / Larger, or an exact dpi) and text size
  (0.85–1.3×) over ADB (`wm size`, `wm density`, `font_scale`). Reset puts all
  three back. Whether the settings survive the app relaunching is untested.
- **Transfer**: drag and drop files to `~/Downloads`; `.apk` files install as
  their own Android app. A game's `.zip`, folder or `.exe` becomes a title in
  the Steam library (Valve's Devkit Game path, with Proton or the Steam Linux
  Runtime picked from the program's header), listed under **Sideloaded titles**
  with Launch and Remove; see [sideloading.md](sideloading.md). Send typed text, or your computer's clipboard, to the
  Frame clipboard.
- **Flatpaks**: install and remove them (quick picks: Moonlight, Firefox, VLC,
  Remmina).
- **One-click tools**: SSH or SFTP in a terminal window, Steam Link, and remote
  desktop (Windows App on macOS, Remote Desktop on Windows, Remmina or FreeRDP on
  Linux). Sleep, restart and shut down open a terminal window because SteamOS
  asks for the sudo password over SSH.

## How it works

`app/` is an Electron shell. It starts `ui/server.py` on a free loopback port
and shows it in its own window; the server stops when you quit the app. The
app bundles `ui/`, `scripts/`, `frame/android/`, Valve's `frame/devkit-utils/` and the rated catalogue from
`apk-catalog/`, plus a standalone Python
([python-build-standalone](https://github.com/astral-sh/python-build-standalone))
and `adb` from Google's platform-tools, so there's nothing else to install. It
also bundles curl's copy of Mozilla's CA list, because Python on Windows only
trusts root certificates already in the Windows store.
`app/build/fetch-deps.js` downloads both, pinned by SHA-256.

The server is Python stdlib only and listens on 127.0.0.1. It rejects requests
with a non-local `Host` header, and any `/api/` request without a custom
header, so other websites can't drive it or read captures. Everything reaches
the headset through the `frame` SSH alias. On macOS and Linux it keeps one
multiplexed SSH connection open, so status and each capture take about 0.3 s.
Windows' OpenSSH can't share a connection, so there each request connects on
its own and the app is a little slower. What differs between the three
systems lives in `ui/frame_host.py`.

Headset captures are deleted from the Frame as soon as they're copied, because
they show everything on screen, including anything private. The look follows
the Steam client: its palette, Motiva Sans (loaded from Valve's CDN), portrait
library capsules and green Play buttons.

**Verified on the Frame 2026-09-25 (macOS app):** status and charging details,
both capture modes (headset view while in use, and a blank frame in standby,
which the UI labels), clipboard, volume, file push, and input validation.
**Not yet exercised from the UI:** Launch, Flatpak install/remove, APK drop,
title sideloading (not yet run on a headset at all), and the power buttons. Each of these calls a command that was verified
separately.

## Per-platform notes

**macOS.** The app reads `PATH` from your login shell, so Homebrew's `rsync`
and `adb` are used when you launch it from Finder. Set Up Connection runs
`scripts/connect.sh` in Terminal. The log is at
`~/Library/Logs/Frame Control/server.log`. The build is ad-hoc signed and not
notarized: a downloaded copy is quarantined until you run
`xattr -dr com.apple.quarantine "/Applications/Frame Control.app"`. The first
time you use them, macOS asks to allow local network access (for SSH) and
control of Terminal (for SSH and power actions).

**Windows.** `ssh` is Windows' built-in OpenSSH client
(Settings → System → Optional features, if it's been removed). Set Up
Connection runs `ui/frame_connect.py` in a console window. Copies use `scp`
because Windows has no `rsync`. The installer isn't code-signed, so SmartScreen
warns on first run: choose **More info → Run anyway**. The log is at
`%APPDATA%\Frame Control\logs\server.log`.

**Linux.** Needs `ssh`, which most desktops have; the `.deb` pulls it in.
The arm64 build also needs your distribution's `adb` for Android apps, because
Google publishes no arm64 Linux platform-tools. Set Up Connection runs
`ui/frame_connect.py` in your terminal emulator (GNOME Terminal, Konsole, xterm
and others). The log is at
`~/.config/Frame Control/logs/server.log`. Running `ui/server.py` in a browser
instead of the app, sending the clipboard needs `wl-clipboard` (Wayland) or
`xclip` (X11).

## Building

```sh
cd app
npm install
npm start              # run from the checkout without packaging
npm run dist           # macOS: dist/*.dmg and .zip (Apple Silicon)
npm run dist:win       # Windows: installer and .zip
npm run dist:linux     # Linux: AppImage and .deb, x64 and arm64
```

Pushing a `v*` tag builds all three in GitHub Actions and attaches them to a
draft release (`.github/workflows/release.yml`). Running copies are offered it
once you publish it: see [releasing.md](releasing.md).
