---
name: steam-frame
description: Operate the user's Valve Steam Frame headset from the Mac through this repo's helpers and field notes. Use for Steam Frame SSH, screen streaming, clipboard, file push, APK or Flatpak installs, launching apps on the headset, arranging floating windows or panels in VR space, or debugging SteamOS/gamescope/SteamVR on the Frame.
---

# Steam Frame

SSH works through the `frame` alias
(user `steamos`). The headset has to be awake for anything that touches its
desktop or panels.

## Start here

1. Read `docs/how-the-frame-works.md`. It's the map: the layer cake (SteamVR →
   gamescope → nested Plasma), the verified facts, and debug recipes.
2. Open the topic doc for the task:

| Task | Doc | Script |
|---|---|---|
| Floating windows in the room, one panel per app | `docs/panels.md` | `scripts/panel-on-frame.sh` |
| First-time access, SSH keys | `docs/ssh.md` | `scripts/connect.sh` |
| See the Frame from the Mac, or the Mac inside the Frame | `docs/streaming.md` | `scripts/run-on-frame.sh mac-screen` |
| Files and clipboard | `docs/file-transfer.md` | `scripts/push.sh`, `scripts/paste-to-frame.sh` |
| Android apps (Lepton) | `docs/apks.md` | `scripts/install-apk.sh` |
| Reach the Frame off the home LAN (Tailscale) | `docs/tailscale.md` | `scripts/tailscale-on-frame.sh` |
| Install or buy Steam games, Frame ratings | `docs/steam-games.md` | `ui/frame_steam.py` |
| Flatpaks | `docs/streaming.md` | `scripts/install-apps.sh` |
| Launch an app inside the desktop panel | the script's header comment | `scripts/run-on-frame.sh` |
| Mac GUI over all of this | `README.md` → Frame Control | `scripts/frame-ui.sh` |
| iPhone/iPad app (server runs on the Frame, `FRAME_LOCAL=1`) | `docs/iphone.md` | `ios/`, `ui/local-bin/ssh` |
| Recovery images, factory reset, boot loops | `docs/recovery-and-images.md`, `docs/how-the-frame-works.md` | `~/Downloads/steam-frame-recovery/` |
| Test without the headset (the Frame OS image's own sshd) | `tests/frame-container/README.md` | `tests/frame-container/frame-image.sh` |
| What's still unverified | `docs/open-questions.md` | — |

Each script's usage is in its header comment. Read the header rather than
running `--help`: `paste-to-frame.sh`, `serve-bootstrap.sh` and
`bootstrap-on-frame.sh` act on any argument.

## Ground rules

- Label every claim **verified** (seen on the device, with the date and
  SteamOS build) or **inferred**. The docs use this convention. Keep it, and
  move items out of `docs/open-questions.md` once they're checked.
- When you learn something new about the Frame, record it in
  `docs/how-the-frame-works.md` (or the topic doc) in the same change.
- The Frame's rootfs is read-only and SteamOS updates replace it. Put changes in
  `~` (`--user` Flatpaks, `~/.config`) rather than `steamos-readonly disable`.
- `sudo` on the Frame asks for the user's Developer Mode password. Hand those
  steps to the user (open Terminal) and keep automation to non-sudo commands.
- The Mac uses BSD userland and zsh (no `timeout`, use `head -n`).
- **First-party first.** For any new capability, investigate the first-party
  way before anything else: Valve (SteamOS, Steam, Steam Link), Apple (the Mac
  and iPhone), and KDE (the Frame's desktop is Plasma). It's usually the best
  answer. If it isn't, write down why not. If it is, find what Frame Control
  can do to make it easier to set up (install over SSH, pre-seed settings,
  pair automatically, tell the user the one setting to turn on).
