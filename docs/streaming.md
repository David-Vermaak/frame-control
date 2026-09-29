# Screen and desktop streaming

This covers three directions, plus input:

- **A. Frame → Mac**: see and control the headset from the Mac.
- **B. Mac → Frame**: use the Mac's desktop inside the headset.
- **C. iPhone → Frame**: mirror the phone inside the headset.
- **PC VR from Linux**: [feasibility and options](linux-vr-streaming.md),
  including Valve's streaming and USB support. No Linux host tested yet.
- **Input**: type and point in the Frame from the Mac or iPhone.

The confidence labels are the same as in [ssh.md](ssh.md).

## A. See and control the Frame from the Mac

| Option | What you get | Confidence | Notes |
|---|---|---|---|
| **Steam Link (macOS app) → `frame`** | A remote view of the headset | **Confirmed (Frame)**: Valve says to "use Steam Link on iOS, Android, or desktop to view the headset remotely by connecting to 'frame'" ([debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging)) | Steam Link for macOS exists ([Tom's Guide](https://www.tomsguide.com/news/macbook-gaming-just-got-a-killer-upgrade-with-steam-link-heres-how-it-looks)). It's the lowest-effort option. Whether you get the VR view or a flat mirror, and whether input works, is unverified. |
| **RDP to xrdp** | A separate Linux (Xorg) desktop session as `steamos` | **Confirmed (Frame)** for the server ([debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging)); **Inferred** for the Mac client | On the Mac, use Microsoft **Windows App** (the old "Microsoft Remote Desktop") from the App Store. Add PC `frame.local` (or the IP), user `steamos`, and the Developer Mode password. Valve says Xorg is the default session. This is a *separate* X session, not a mirror of what's in the headset. It's good for running GUI apps and supports clipboard sync. |
| **ADB + scrcpy (Lepton only)** | A mirror of the Android container | **Guess** | `brew install scrcpy android-platform-tools`, then `adb connect frame.local:5555` while Lepton Development is running ([adb_lepton](https://partner.steamgames.com/doc/steamhardware/steamframe/adb_lepton)), then `scrcpy`. This only shows Android apps, not SteamOS. |
| VNC server on the Frame (krfb / wayvnc) | A mirror of the Plasma desktop | **Inferred (SteamOS)** | Deck users run krfb in Desktop Mode ([one.vg](https://one.vg/blog/remote-control-your-steam-deck)). On the Frame, the in-headset desktop is a virtual screen, and krfb isn't known to be preinstalled. RDP and Steam Link cover this case, so it's not recommended. |

**Recommendation for A:** start with Steam Link for macOS, because Valve
documents it. Use Windows App (RDP) when you want a proper Linux desktop on the
Mac with keyboard, mouse, and clipboard.

## B. Show the Mac's desktop inside the Frame

The Frame's VR streaming uses **SteamVR** on the host. Linux hosts had
VR-streaming problems at launch
([Steam discussion](https://steamcommunity.com/app/4165890/discussions/0/528765047224280796/),
[gbl08ma](https://gbl08ma.com/posts/steam-frame-a-linux-machine-doesnt-support-linux/)).
Valve's later 2.17.8 notes explicitly describe Steam Link fixes on Linux and
initial USB streaming support (**documented**, not tested from a Linux host
here). See [the current comparison](linux-vr-streaming.md#a-valves-own-path--recommended-first).
**macOS isn't a supported SteamVR host**, so for the Mac we're only looking at
flat 2D desktop streaming into a window on the Frame's Linux desktop.

| Option | Setup | Confidence | Verdict |
|---|---|---|---|
| **macOS Screen Sharing (VNC) → Remmina on the Frame** | **Mac:** System Settings → General → Sharing → Screen Sharing on → (i) → enable "VNC viewers may control screen with password". **Frame:** `./scripts/install-apps.sh remmina` from the Mac, then open Remmina in the headset and connect to `vnc://<mac>.local` | **Verified 2026-09-27** (Frame BUILD_ID 20260925.6191901, macOS 27.0), in its own panel via `panel-on-frame.sh mac-screen`. Remmina is on Flathub for **aarch64** with VNC and RDP ([Flathub](https://flathub.org/apps/org.remmina.Remmina)). The Frame desktop runs Flatpaks ([UploadVR](https://www.uploadvr.com/flatpaks-open-source-steam-frame/)). macOS VNC is built in. | **Recommended.** Nothing to install on the Mac, and it's easy to set up. Noticeable lag, even at lower Remmina quality settings on a good 5 GHz link, where neither Wi-Fi nor the Frame's CPU was the bottleneck. Usable for reading and coding, but not for games. You'll type the Mac's hostname once in Remmina on the headset, then save the profile. To avoid even that, the script can pre-seed a Remmina profile over SSH (see below). |
| Sunshine (Mac) → Moonlight (Frame Flatpak) | `brew install` Sunshine on the Mac, then `./scripts/install-apps.sh moonlight` | Moonlight Flatpak supports **aarch64** ([Flathub](https://flathub.org/apps/com.moonlight_stream.Moonlight)). **Sunshine on macOS is poorly supported**: install problems on Apple Silicon/Sequoia, and no virtual gamepads ([LizardByte discussion #777](https://github.com/orgs/LizardByte/discussions/777)). | Try it if VNC is too laggy. Expect some friction. |
| Steam Remote Play with the Mac as host | Steam on the Mac, Steam Link/Remote Play on the Frame | macOS-hosted Remote Play is reported broken or flaky in 2024–2026 ([Steam discussion](https://steamcommunity.com/groups/homestream/discussions/1/574921459914429988/)) | Not recommended. It's only for games, if it works at all. |
| Immersed / Virtual Desktop | Vendor apps | Immersed has a Mac agent but no known Frame client. Virtual Desktop's developer said he'd "try" to port it ([NewsBreak](https://www.newsbreak.com/news/4892834783961-virtual-desktop-dev-says-he-ll-try-to-bring-the-app-to-steam-frame)). | Not available as of 2026-09-25. Check again later. |
| WiVRn / ALVR | VR streaming from a Linux or Windows PC | Irrelevant for a Mac host (no SteamVR/OpenXR runtime on macOS) | N/A |

For **local movies and stereo photos**, Frame Control's own OpenVR player
runs on the Frame; see [vr-video.md](vr-video.md). It currently renders a flat
stereo screen. VR180/360 projection is not implemented; the same page records
DeoVR only as an optional, independently installed alternative.

### Pre-seeding the Remmina profile (no typing in the headset)

`scripts/install-apps.sh remmina --vnc-host <your-mac>.local` writes
`~/.var/app/org.remmina.Remmina/data/remmina/mac-screen-sharing.remmina` on the Frame over
SSH. The profile then appears in Remmina's list, and you just click it. It
scales the Mac's desktop to fit the window (`scale=1`, `viewmode=1`). Without
that, Remmina shows a Retina Mac's native pixels 1:1, so you see a zoomed-in
corner. (Verified 2026-09-27.)

**Expect a Mac login prompt, not the VNC password.** macOS offers Apple's own
authentication (RFB security type 30) ahead of plain VNC auth (type 2), and
Remmina picks it. So Remmina asks for your **Mac account name and login
password**; the "VNC viewers may control screen" password isn't used. To store
the password without typing it in the headset, run on the Frame:

```sh
printf '%s' "$PASSWORD" | flatpak run org.remmina.Remmina \
  --update-profile ~/.var/app/org.remmina.Remmina/data/remmina/mac-screen-sharing.remmina \
  --set-option password
```

Remmina encrypts it into the profile with its own key, because there's no
secret service in the SSH session. (Verified 2026-09-27.)

### The Mac's cursor

The mirror doesn't show the Mac's pointer, with either `showcursor` value.
macOS keeps the pointer out of the picture it sends, and Remmina's cursor mode
draws the cursor shape only at the Frame's own pointer, which doesn't follow
the Mac trackpad. `scripts/mac-cursor-ring.lua` works around this: a
[Hammerspoon](https://www.hammerspoon.org/) script that draws a ring around the
Mac pointer as a real window, so it's part of the mirrored picture. Setup is in
its header. (Verified 2026-09-27.)

Going the other way, pointing a controller at the panel moves the Mac's mouse,
because Remmina forwards input (`viewonly=0`).

## C. Show the iPhone's screen inside the Frame

iOS only shares its screen two ways: **AirPlay** (Screen Mirroring in Control
Centre) or a **ReplayKit broadcast extension** in an app. Nothing else can
capture it.

| Option | What it takes | Confidence | Verdict |
|---|---|---|---|
| **UxPlay** (an open-source AirPlay receiver) on the Frame | Build it for aarch64 (no Flathub package; there's a Snap and distro packages), run it in `~` or a podman container, and advertise it over mDNS. The iPhone *and* the Mac then see "Frame" in Screen Mirroring, with nothing to install on either | **Inferred.** It runs on ARM64 Linux such as the Raspberry Pi ([UxPlay](https://github.com/FDH2/UxPlay)). Not tried on the Frame: needs mDNS registration and its ports (7000, 7001, 7100 and a UDP range) reachable | **Recommended to try first.** It's the only receiver-side option, and it covers the Mac too. The window shows in the Frame's Linux desktop panel |
| A broadcast extension in Frame Control | ReplayKit sends the screen to a small extension (50 MB memory limit), which encodes H.264 and sends it through the app's SSH tunnel to the page, shown the same way as the Frame's live view in reverse | **Inferred** from Apple's ReplayKit docs | Full control and no network setup, but several days' work, and the picture only shows where Frame Control's page is open in the headset |

## Input: type and point in the Frame from the Mac or iPhone

**Verified 2026-09-27** on the headset: `steamos` is in the `input` group and
`/dev/uinput` is `crw-rw-r-- root input`, so **our own code can create a
virtual keyboard and mouse without sudo**. The Frame has no `python-evdev`,
`ydotool`, `wtype` or KDE Connect; `kwin_wayland` and `plasmashell` run only
while the desktop panel is open in the headset.

| Option | Mac | iPhone | Notes |
|---|---|---|---|
| **A uinput keyboard and mouse in Frame Control's server** | ✓ | ✓ | **Recommended.** The server opens `/dev/uinput` with `ctypes` (standard library only) and the page sends key and pointer events through the tunnel it already has. On the phone: a trackpad area (drag to move, tap to click, two fingers to scroll) and the iOS keyboard for typing. On the Mac: a "control the Frame" mode that captures the keyboard and pointer (Esc to release). Uinput devices look like real hardware to the kernel, so libinput, KWin and gamescope should take them; [frame-voice](https://github.com/DeeJanuz/frame-voice) already types into a Frame through a uinput keyboard. **Untested**: which surfaces in VR (desktop panel, SteamVR dashboard, games, Android apps in Lepton) accept the pointer. About a day or two of work |
| **Bluetooth keyboard and mouse** | – | – | Real hardware paired in SteamOS settings. The iPhone can't pretend to be a Bluetooth keyboard: iOS won't advertise the HID service ([Apple forums](https://developer.apple.com/forums/thread/733916)) |
| **Deskflow** (formerly Input Leap / Barrier) | ✓ | – | Moves the Mac's own mouse and keyboard onto the Frame's screen edge. Flathub has an aarch64 build ([Flathub](https://flathub.org/apps/org.deskflow.deskflow)); on Wayland it needs the InputCapture/libei portal, and only works while Plasma is running. No iPhone client |
| **KDE Connect** | ~ | ✓ | Its iOS app has a remote touchpad and keyboard, but the Frame would need KDE Connect installed (not on Flathub; `pacman` on a read-only root). More moving parts than the uinput route |
| **Remmina / Steam Link / RDP** | ✓ | – | Input only reaches the streamed session, not the headset's own apps |

Other ways to get text in:

- **Clipboard from the Mac**: `scripts/paste-to-frame.sh`, or Frame Control's
  clipboard box (see [file-transfer.md](file-transfer.md#clipboard)). Needs
  the desktop panel open.
- **RDP session**: Windows App syncs the clipboard with xrdp, but only inside
  that RDP session.
