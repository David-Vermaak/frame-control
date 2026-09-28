# Mac in the headset

Frame Control can show any Mac window, or a whole Mac screen, as its own panel
in the Steam Frame. You place each panel anywhere in the room with the SteamVR
dashboard. The laser clicks and drags, the thumbstick scrolls, and you type on
the Mac's own keyboard. Find it under **Tools → Mac in the headset** (macOS
only).

The confidence labels are the same as in [ssh.md](ssh.md).

## Why this design

First-party options come first, as the repo's rule asks, with the reason
each one was or wasn't chosen. The full list for every device is in
[streaming.md](streaming.md#first-party-options-and-why-they-do-or-dont-fit).
Checked 2026-09-28.

| Goal | First-party option | Chosen? | Why |
|---|---|---|---|
| One Mac screen in the headset | **Apple Screen Sharing** (VNC) → Remmina (Remmina 1.4.43 is already installed on this Frame) | Kept as the fallback (`panel-on-frame.sh mac-screen`) | It's the closest to first-party and needs nothing new. But VNC sends compressed tiles rather than video, so moving content is slow. It shows only whole screens |
| One Mac screen | **Steam Remote Play**, Mac as host (Valve) | No | macOS isn't a SteamVR host, and Mac-hosted Remote Play is reported broken ([Steam forum](https://steamcommunity.com/groups/homestream/discussions/1/574921459914429988/)). It streams games, not the desktop. **Not tested here**; one real try is still worth doing |
| One Mac screen | **AirPlay** (Apple) | No | Apple licenses AirPlay receivers only to TV and speaker makers, and nothing official runs on Linux. UxPlay is an unofficial receiver, and it mirrors a whole screen, not single windows |
| One Mac screen | **Sidecar / Mac Virtual Display** (Apple) | No | These work only with an iPad or Apple Vision Pro |
| **Each Mac window as its own panel** | None | – | No first-party way does this: Apple's per-app streaming is only for Vision Pro, and Valve's desktop streaming needs a Windows SteamVR host. So Frame Control does it itself |
| Mac keyboard and trackpad driving the headset | **Bluetooth HID** | No | macOS can't act as a Bluetooth keyboard or mouse. A real Bluetooth keyboard paired with the Frame still works |
| Mac keyboard and trackpad | **KDE Connect** (KDE) | No | The Frame has no `kdeconnectd` and it isn't on Flathub. Its Mac app has no keyboard or mouse sharing (**inferred**), and on Wayland it can only reach the desktop panel |
| Mac keyboard and trackpad | **xrdp** (Valve, Developer Mode) | No | It runs a separate Linux session that you view on the Mac. It isn't the headset's view, and it doesn't carry input the other way |

What that leaves is our own stream: nothing to install on the Mac or the
Frame, and whole screens or single windows. Here the Mac's own keyboard and
trackpad need no forwarding, because the windows are still on the Mac. The
laser is the only input that has to be sent back.

Other routes that were compared:

| Option | One screen | Each window | Speed | Verdict |
|---|---|---|---|---|
| Sunshine → Moonlight | ✓ | – | Good | Sunshine's macOS support is still experimental ([discussion #777](https://github.com/orgs/LizardByte/discussions/777)), and it captures whole screens only |
| Virtual Desktop, Immersed | – | – | – | No Frame client as of September 2026 |
| **Frame Control's own stream** | ✓ | ✓ | Hardware H.264, sending only changed frames | **Built** |

To type into VR surfaces other than these panels (SteamVR's dashboard,
games), the Frame supports a uinput keyboard and mouse without sudo
(verified 2026-09-27: `steamos` is in `input`, and `/dev/uinput` is
`root:input 660`). That's a separate feature, not part of this one.

## How it works

```
Mac                                              Frame
ScreenCaptureKit (one window or display)
  → VideoToolbox H.264 (hardware, low-latency,
    no B-frames)
  → frame-mac-view, 127.0.0.1 ──ssh -R──→ 127.0.0.1:479xx
                                             → Chromium app window per stream
                                               (WebCodecs decode), on gamescope's
                                               X display, tagged STEAM_GAME
                                             → its own SteamVR panel
  ← CGEvent (clicks, drags, wheel, keys) ←──── pointer, wheel and key events
```

- **The agent** is `mac/bin/frame-mac-view`, built from `mac/frame-mac-view`
  (Swift, no dependencies; `build.sh`). Frame Control's server starts it on
  first use and stops it on quit.
  - It captures with ScreenCaptureKit, which sends frames only when something
    changes, so idle windows cost nothing.
  - It encodes in hardware with VideoToolbox's low-latency rate control (plain
    real-time mode where that's unavailable).
  - While anyone is watching, it keeps the Mac's display awake. A sleeping
    display isn't drawn, so there would be nothing to capture.
- **The link** is an `ssh -R` tunnel on its own connection. It's encrypted and
  works anywhere `ssh frame` works, Tailscale included, with no firewall
  changes on the Mac. If the headset sleeps or the network drops, Frame
  Control reopens the tunnel on the same port, and open viewers reconnect by
  themselves.
- **Access.**
  - Frame Control's own key never leaves the Mac.
  - Each viewer is opened with a **single-use ticket**. It's tied to one
    window or display and expires after a minute. It's spent as soon as the
    viewer confirms it has received its reconnect key. Until then, a retry
    gets the same key, so a connection lost at that moment doesn't strand
    the viewer. Stop revokes tickets that haven't been used yet.
  - After that, the viewer holds a reconnect key for that one source, in
    memory only. **Stop** revokes it.
  - Remaining risk: a program running as `steamos` on the Frame could read a
    ticket from Chromium's command line in the first second or so and use it
    first. That gets it the one source being opened, not the Mac, and the
    real viewer would then fail to connect. Android apps in Lepton run in
    their own podman container, so they shouldn't see the Frame's process
    list (inferred, not checked).
- **The viewer** is `ui/mac-view.html`, served by the agent. It opens on the
  Frame as a Chromium app window, preferring Chromium XR (`~/chromium-xr`,
  built with H.264) over Flathub Chromium.
  - The page puts `[fcNNNNN]` in its title. The launcher finds the window by
    that tag and sets `STEAM_GAME` to a stable id per source, which gives it
    its own panel (see [panels.md](panels.md)). The same Mac window gets the
    same panel id each time.
  - It decodes with WebCodecs. If it falls behind, it skips to the next
    keyframe instead of showing old frames late.
  - It falls back to JPEG stills (**Compatible** quality) where H.264 isn't
    available.
- **Flow control.** When the link backs up, the agent skips capture frames
  *before* encoding, so no reference frame goes missing. Once the link drains,
  it sends the newest picture.
- **Input.**
  - A click on a window's panel brings that Mac window to the front
    (Accessibility API), then clicks at the same point. Double clicks, right
    clicks, drags and the wheel work too.
  - Keys typed into the panel are sent as Mac key codes. Any keys or buttons
    still held down are released if the viewer loses focus or disconnects, or
    when the stream stops. Characters the key
    table doesn't know, such as those from other keyboard layouts, are typed
    as text.
  - The Mac's own keyboard and trackpad keep working as normal. Click a panel
    with the laser, then type on the Mac.

## Permissions (Mac)

- **Screen Recording**, to see windows. Without it, the card asks for it.
- **Accessibility**, so input from the headset reaches the Mac. Without it the
  stream still works, and the viewer says clicks won't go through.

Both are granted to Frame Control. After granting, press **Refresh**, which
restarts the helper so it picks them up. The app is ad-hoc signed, so macOS
may ask again after an update.

## Quality settings

| Setting | Long side | fps | Codec | Use |
|---|---|---|---|---|
| Sharp | 2560 | 60 | H.264, ~0.14 bits/pixel | Text-heavy windows on a strong link |
| Balanced (default) | 1920 | 60 | H.264, ~0.1 bits/pixel | Most things |
| Light | 1280 | 30 | H.264 | Weak Wi-Fi or Tailscale off the LAN |
| Compatible | 1280 | 20 | JPEG | A Frame browser without H.264 |

## Checked so far (2026-09-28)

- **Mac (checked by hand, macOS 26.5.2).**
  - The agent builds, and lists windows and displays.
  - In a browser, the test pattern decoded at about 60 fps (H.264) and 30 fps
    (JPEG, about 22 Mbps).
  - A click in the viewer arrived at the same point in the source.
  - `pmset -g assertions` showed the display-awake assertion only while a
    stream was being watched.
- **Automated** (`tests/test_macview.py`, on CI's macOS runner): the agent
  builds (a build failure fails the job).
  - `/ping` and the page are open; everything else needs the key.
  - Tickets work once and only for their own source, and Stop revokes
    reconnect keys.
  - A WebSocket frame claiming 2^63 bytes closes that socket, and the agent
    keeps running.
  - A stream sends an SPS-led H.264 keyframe, and sends another when asked.
  - Stop ends the stream on the Mac even if the viewer ignores it.
  - The test doesn't decode video, time it, or check where clicks land.
- **Linux aarch64 (verified in a stand-in, not on the Frame).** In an Arch
  Linux ARM container with sshd, Xvfb as `:0` and Chromium 153:
  - The real `show` path worked in 1–2.3 s: tunnel, ticket, launcher,
    window found and tagged `STEAM_GAME`.
  - Frame Control's key didn't appear anywhere in the stand-in's process
    list.
  - After the tunnel was killed, it came back on the same port within 4 s,
    and the viewer reconnected by itself.
  - Chromium decoded the stream in software with the GPU off.
  - Stop closed the window, and Chromium exited.
  - This test found and fixed a bug: in a C locale, `xwininfo` can't print a
    title with non-ASCII characters, so the launcher reads `_NET_WM_NAME`
    with `xprop`.
- **On the Frame (verified 2026-09-28, SteamOS build 20260925.6191901,
  from the Mac, nobody wearing the headset).** Frame Control's **Show** with
  the test pattern:
  - The panel was ready in 1.45 s. SteamVR logged `[Overlays] Created:
    valve.steam.desktopgame.2001639889` (in `vrwebhelper_systemui.txt`).
  - The window was tagged `STEAM_GAME`, and gamescope sized it to 1920×1080
    although 1280×720 was asked for.
  - Chromium XR decoded it live at about 60 fps: the frame counter advanced
    62 in 1.04 s.
  - Over home Wi-Fi, the frames in the viewer window had been drawn on the
    Mac about 11–17 ms earlier, plus `xwd`'s own time. That's measured
    against the two clocks, which were 37–39 ms apart (±4 ms, measured over
    one SSH session). SteamVR's compositor and the display come on top.
  - Clicks and keys injected with XTest into gamescope's Xwayland didn't
    reach the page. That's inconclusive, not a failure: XTest on gamescope
    isn't how real input arrives. The laser should arrive as a left mouse
    button and the thumbstick as a wheel, because the window has an app id
    (from gamescope's source; see [panels.md](panels.md)).
- **Not yet checked:**
  - Clicking, dragging and scrolling with the laser while wearing the
    headset.
  - Real window capture and input on a Mac with both permissions granted.
  - Keys from SteamVR's on-screen keyboard.
  - Whether Flathub Chromium has H.264. Chromium XR is used when it's
    installed, as it is on this Frame.
  - Latency with real, busy windows at Sharp.

## Limits

- Only windows on the Mac's current desktop (Space) are listed, and
  minimised windows can't be captured.
- A window's panel shows only that window. Its menus and sheets are separate
  windows on the Mac, so open them from the Mac or use **Whole screen**.
- Keys go to whichever Mac window is in front. Clicking a panel brings its
  window to the front first.
- Ctrl stays Ctrl. On the Mac, copy is ⌘C, so use Meta+C on a keyboard paired
  with the Frame.
