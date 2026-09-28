# Screen and desktop streaming

This covers three directions, plus input:

- **A. Frame → Mac**: see and control the headset from the Mac.
- **B. Mac → Frame**: use the Mac's desktop inside the headset.
- **C. iPhone → Frame**: mirror the phone inside the headset.
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

The Frame's streaming features are built around a **Windows PC running
SteamVR** plus the USB Wi-Fi 6E dongle. Even Linux hosts had VR-streaming
problems at launch
([Steam discussion](https://steamcommunity.com/app/4165890/discussions/0/528765047224280796/),
[gbl08ma](https://gbl08ma.com/posts/steam-frame-a-linux-machine-doesnt-support-linux/)).
**macOS isn't a supported SteamVR host**, so for the Mac we're only looking at
flat 2D desktop streaming into a window on the Frame's Linux desktop.

| Option | Setup | Confidence | Verdict |
|---|---|---|---|
| **macOS Screen Sharing (VNC) → Remmina on the Frame** | **Mac:** System Settings → General → Sharing → Screen Sharing on → (i) → enable "VNC viewers may control screen with password". **Frame:** `./scripts/install-apps.sh remmina` from the Mac, then open Remmina in the headset and connect to `vnc://<mac>.local` | **Inferred.** Remmina is on Flathub for **aarch64** with VNC and RDP ([Flathub](https://flathub.org/apps/org.remmina.Remmina)). The Frame desktop runs Flatpaks ([UploadVR](https://www.uploadvr.com/flatpaks-open-source-steam-frame/)). macOS VNC is built in. | **Recommended.** Nothing to install on the Mac, and it's easy to set up. Latency is fine for productivity but not for games. You'll type the Mac's hostname once in Remmina on the headset, then save the profile. To avoid even that, the script can pre-seed a Remmina profile over SSH (see below). |
| Sunshine (Mac) → Moonlight (Frame Flatpak) | `brew install` Sunshine on the Mac, then `./scripts/install-apps.sh moonlight` | Moonlight Flatpak supports **aarch64** ([Flathub](https://flathub.org/apps/com.moonlight_stream.Moonlight)). **Sunshine on macOS is poorly supported**: install problems on Apple Silicon/Sequoia, and no virtual gamepads ([LizardByte discussion #777](https://github.com/orgs/LizardByte/discussions/777)). | Try it if VNC is too laggy. Expect some friction. |
| Steam Remote Play with the Mac as host | Steam on the Mac, Steam Link/Remote Play on the Frame | macOS-hosted Remote Play is reported broken or flaky in 2024–2026 ([Steam discussion](https://steamcommunity.com/groups/homestream/discussions/1/574921459914429988/)) | Not recommended. It's only for games, if it works at all. |
| Immersed / Virtual Desktop | Vendor apps | Immersed has a Mac agent but no known Frame client. Virtual Desktop's developer said he'd "try" to port it ([NewsBreak](https://www.newsbreak.com/news/4892834783961-virtual-desktop-dev-says-he-ll-try-to-bring-the-app-to-steam-frame)). | Not available as of 2026-09-25. Check again later. |
| WiVRn / ALVR | VR streaming from a Linux or Windows PC | Irrelevant for a Mac host (no SteamVR/OpenXR runtime on macOS) | N/A |

For **VR video files** (180°/360° stereo), don't stream the Mac's screen. Play
them on the Frame in DeoVR instead: see [vr-video.md](vr-video.md).

### Pre-seeding the Remmina profile (no typing in the headset)

`scripts/install-apps.sh remmina --vnc-host <your-mac>.local` writes
`~/.var/app/org.remmina.Remmina/data/remmina/mac-screen-sharing.remmina` on the Frame over
SSH. The profile then appears in Remmina's list, and you just click it. You'll
still be asked for the VNC password in the headset the first time, unless you
choose to save it. Remmina stores passwords encrypted with a per-install key,
so the script doesn't try to write the password. (The Remmina file format is
standard; the Flatpak data path is inferred.)

## C. Show the iPhone's screen inside the Frame

iOS only shares its screen two ways: **AirPlay** (Screen Mirroring in Control
Centre) or a **ReplayKit broadcast extension** in an app. Nothing else can
capture it.

| Option | What it takes | Confidence | Verdict |
|---|---|---|---|
| **UxPlay** (an open-source AirPlay receiver) on the Frame | Build it for aarch64 (no Flathub package; there's a Snap and distro packages), run it in `~` or a podman container, and advertise it over mDNS. The iPhone *and* the Mac then see "Frame" in Screen Mirroring, with nothing to install on either | **Inferred.** It runs on ARM64 Linux such as the Raspberry Pi ([UxPlay](https://github.com/FDH2/UxPlay)). Not tried on the Frame: needs mDNS registration and its ports (7000, 7001, 7100 and a UDP range) reachable | **Recommended to try first.** It's the only receiver-side option, and it covers the Mac too. The window shows in the Frame's Linux desktop panel |
| A broadcast extension in Frame Control | ReplayKit sends the screen to a small extension (50 MB memory limit), which encodes H.264 and sends it through the app's SSH tunnel to the page, shown the same way as the Frame's live view in reverse | **Inferred** from Apple's ReplayKit docs | Full control and no network setup, but several days' work, and the picture only shows where Frame Control's page is open in the headset |

## Input: type and point in the Frame from the Mac or iPhone

**Built: Home → Keyboard and trackpad**, in every version of Frame Control
(Mac, Windows, Linux, iPhone and iPad), with nothing to install on the device
you're holding. On a phone the panel is a trackpad (drag to move, tap to click,
two fingers to scroll, two-finger tap to right-click) plus a text field that
types on the Frame. On a computer, clicking the pad passes your mouse and
keyboard through to the Frame until you press Esc (⌘ is sent as Ctrl on a Mac).

It goes through **KDE Connect**, the first-party route (KDE makes the Frame's
desktop): Frame Control's server runs [`ui/frame_input_agent.py`](../ui/frame_input_agent.py)
on the Frame, which talks KDE Connect's own LAN protocol to the Frame's
`kdeconnectd` as if it were a phone. KDE Connect does the typing and clicking.

**Verified 2026-09-28** (SteamOS 0.4.1, build 20260925.6191901):

- KDE Connect isn't installed, but **Valve's package repository for the Frame
  has it** (`kdeconnect` 24.02.2 in `extra`). The Frame lacks only `kpeople`,
  `libfakekey`, `modemmanager-qt` and `pulseaudio-qt`. The agent fetches them
  with `pacman -Sp` + `curl` into `~/.local/share/frame-control/kdeconnect`
  (8 MB download, 82 MB unpacked, about 11 s), with no root and nothing on the
  read-only system, so SteamOS updates leave it alone.
- It pairs by itself: the agent asks to pair and accepts on KDE Connect's side
  over D-Bus (`qdbus6 … acceptPairing`). It keeps its identity in
  `…/kdeconnect/bridge`, so later connections are already paired. (A pair
  request to a device that's already paired makes KDE Connect unpair it, so
  the agent only asks when it isn't paired.)
- Protocol version 7: whoever opens the TCP connection sends its identity line
  in plain text, then acts as the **TLS server** (KDE Connect's
  `lanlinkprovider.cpp`). Remote input is `kdeconnect.mousepad.request` with
  `dx`/`dy`, `singleclick`, `rightclick`, `singlehold`/`singlerelease`,
  `scroll`, `key` (any text) or `specialKey` (1 Backspace … 14 Escape,
  21–32 F1–F12) and modifier flags.
- **KDE Connect runs only while something uses the keyboard and trackpad.**
  Each device gets its own KDE Connect identity (KDE Connect keeps one
  connection per device, so a shared one would make a phone and a computer
  knock each other off). The last one to disconnect stops KDE Connect, so it
  isn't left running, or discoverable on your network, afterwards.
- KDE Connect 24.02 **hangs or crashes when asked to unpair a device that's
  offline** (seen twice: once spinning at 100% CPU with D-Bus unresponsive,
  once exiting). Frame Control never unpairs. If its copy stops answering, the
  agent restarts it once (tested by freezing it with `kill -STOP`).
- Moves from the iPhone app (Simulator) and the Mac's server moved the Frame's
  X pointer by exactly the amount sent.
- gamescope runs **two Xwayland displays**. `:0` holds Steam's VR bar and menus
  and ignores injected pointer motion; `:1` holds apps such as Chromium and
  takes it. KDE Connect runs on `:1`, so it reaches apps, not Steam's own menus.
  There's also a `gamescope-0-ei` (libei) socket.
- **Not yet tested:** typing and clicking as seen in the headset, and whether
  it reaches the KDE desktop panel (Plasma is its own session).
- **Known limit:** keys and clicks typed while the link is reconnecting wait
  and are sent once it's back, but anything sent in the moment the Wi-Fi
  drops, before SSH notices, can be lost. Confirming every event would add a
  round trip to each pointer move.

Our own `uinput` keyboard and mouse would also work (`steamos` is in the
`input` group and `/dev/uinput` is group-writable, verified 2026-09-27), and
remains the fallback if KDE Connect ever can't be fetched.

| Other option | Mac | iPhone | Why not |
|---|---|---|---|
| **Bluetooth keyboard and mouse** | – | – | Needs real hardware, paired in SteamOS settings. The iPhone can't pretend to be a Bluetooth keyboard: iOS won't advertise the HID service ([Apple forums](https://developer.apple.com/forums/thread/733916)) |
| **Deskflow** (formerly Input Leap / Barrier) | ✓ | – | Moves the Mac's own mouse and keyboard onto the Frame's screen edge. Flathub has an aarch64 build ([Flathub](https://flathub.org/apps/org.deskflow.deskflow)); on Wayland it needs the InputCapture/libei portal, and only works while Plasma is running. No iPhone client |
| **Remmina / Steam Link / RDP** | ✓ | – | Input only reaches the streamed session, not the headset's own apps |

Other ways to get text in:

- **Clipboard from the Mac**: `scripts/paste-to-frame.sh`, or Frame Control's
  clipboard box (see [file-transfer.md](file-transfer.md#clipboard)). Needs
  the desktop panel open.
- **RDP session**: Windows App syncs the clipboard with xrdp, but only inside
  that RDP session.
