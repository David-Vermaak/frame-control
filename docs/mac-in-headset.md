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
| One Mac screen in the headset | **Apple Screen Sharing** (VNC) → Remmina (Remmina 1.4.43 is already installed on this Frame) | Kept as the fallback (`panel-on-frame.sh mac-screen`) | It's the closest to first-party and needs nothing new. But VNC sends compressed tiles rather than video, so moving content is slow: noticeable lag even on a good 5 GHz link (**verified** 2026-09-27, see [streaming.md](streaming.md)), and the Mac's pointer isn't in the picture without a helper. It shows only whole screens |
| One Mac screen | **Steam Remote Play**, Mac as host (Valve) | For Mac games only; see [Steam's own streaming](#steams-own-streaming) | The Mac's and the Frame's Steam clients already find each other (**verified**). But Remote Play streams a game (the whole desktop only while the game is out of focus, untested from a Mac), never single windows, and a Mac can't host the Frame's VR streaming |
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

## Steam's own streaming

The Frame is built around Steam streaming, so this was checked first
(2026-09-28). It fits Mac games, not Mac windows.

- **VR streaming from the Mac: no.** The Frame streams VR from a PC running
  SteamVR ("Steam Link" with foveated streaming). SteamVR dropped macOS in
  2020, and Valve lists PCs, laptops, Steam Deck and Steam Machine as
  hosts, never a Mac (**documented**:
  [UploadVR](https://www.uploadvr.com/steamvr-drops-mac-support/),
  [Road to VR](https://roadtovr.com/steam-frame-game-certification-specs/)).
- **Flat Remote Play from the Mac: probably, for Steam games.**
  - Steam on this Mac has streaming on, and the two Steam clients already
    see each other. The Frame's `remote_connections.txt` shows it
    connecting directly to "Alexs-MacBook-Pro-7" at 192.168.1.211:27036,
    and the Mac's shows the Frame connecting over Wi-Fi and over the USB-C
    link (**verified** in both clients' logs).
  - Whether a stream then starts, and how a flat game looks in the headset
    (reviews describe a theater screen), is **not tested yet**. The Frame's
    Steam was crash-looping during this session (below).
  - Mac-hosted Remote Play has a long-standing report of the stream
    closing as the game loads
    ([Steam forum](https://steamcommunity.com/groups/homestream/discussions/1/574921459914429988/),
    **reported**).
- **The Mac desktop through Steam: untested; single windows: no.** Valve
  says Remote Play shows the host's desktop when the game loses focus
  ([Steam Remote Play FAQ](https://help.steampowered.com/en/faqs/view/0689-74B8-92AC-10F2),
  **documented**), so a whole Mac screen may be reachable by starting a
  game, then switching away from it. Nobody has tried that from a Mac
  host. It would still be one screen in one panel: Remote Play has
  nothing like one panel per Mac window, so Frame Control's own stream
  stays the way to see separate windows.
- **What Steam's work did give us: the USB-C link.** Plugged into the Mac,
  the Frame appears as a network port called "Steam Frame". Steam's Remote
  Play discovery uses it, and so does Frame Control's stream now (see
  "USB-C, when it's plugged in" below).

Next steps, once Steam on the Frame is healthy:

1. Stream the one Mac game installed here (Fortune Mill) from the Frame's
   library, over Wi-Fi and over USB-C.
2. Record whether it starts, how it's shown, and its latency. Steam's
   streaming overlay shows this; our benchmark can't measure it.
3. While streaming, switch away from the game on the Mac, and see whether
   the Mac's desktop appears in the headset, and whether its keyboard and
   pointer work.
4. If it works, Frame Control's Games page could offer "Stream from the
   Mac" for Mac-installed games.

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
- **USB-C, when it's plugged in.** Connected to the Mac by cable, the Frame
  is also a USB network device: macOS lists a network port called "Steam
  Frame", and the Frame's `usb0` answers in under 1 ms. Frame Control
  checks for it each time it opens the tunnel and uses it when it's there,
  with the Frame's usual SSH host key. Otherwise it uses the normal path.
  `FRAME_MACVIEW_USB=0` turns this off. **Verified** 2026-09-28, in two
  interleaved pairs of runs:

  | | USB-C | Wi-Fi (Tailscale) |
  |---|---|---|
  | test: content p50 / p95 | 6.9–7.3 / 8.4–8.8 ms | 9.8–10.1 / 12.1–12.3 ms |
  | test: click to drawn p50 | 16.6–16.9 ms | 26.4–27.8 ms |
  | scroll: content p95 | 23.0–23.5 ms | 31.4–36.9 ms |
  | scroll: late frames | 3.2–3.3% | 4.7–7.0% |

  (`bench/results/2026-09-28-*-usb1.json`, `-usb2`, `-wifi1`, `-wifi2`.)
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
- **Flow control.** The agent never lets frames queue up anywhere on the
  way. It skips capture frames *before* encoding, so no reference frame goes
  missing, and it lowers the bitrate, then the frame rate, then the size, to
  fit the link (see [Adapting to the network](#adapting-to-the-network)).
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

## Measuring

Every frame carries a sequence number, and the agent records its journey on
the Mac's clock (`Sources/Stats.swift`):

| Stage | From → to |
|---|---|
| capture | the Mac composited it (ScreenCaptureKit's display time) → the agent got it |
| queue, encode | → encoding started → the encoder finished |
| network | → the viewer received it |
| decode, draw | → WebCodecs decoded it → it was drawn on the page's canvas |
| present | → the page's next animation frame |

- **Clock sync.** The viewer syncs its clock to the Mac's the way NTP does:
  it pings over the stream's own WebSocket and keeps the sample with the
  shortest round trip. It then reports, in Mac time, when each frame arrived
  (right away, so the agent can pace itself) and when it was decoded and
  drawn (in batches every 250 ms).
- **Input.** The first frame captured after a click or key carries that
  event's id. So input latency is the viewer's event → injected on the Mac →
  the first frame after it → drawn in the headset.
- **Where to see it.**
  - `GET /stats` (key required) returns every frame and input record.
  - `/status` includes a two-second summary, which Frame Control's card
    shows next to each live stream.
  - In the headset, add `?stats=1` to the viewer or press
    Ctrl+Alt+Shift+S for an overlay.
- **The benchmark.** `scripts/macview-bench.py` runs fixed scenarios on the
  real Frame, from the Mac, with nobody wearing the headset:
  - **test** is the moving test pattern.
  - **scroll** is a Chrome page on its own display scrolling at 240 pt/s,
    which gives about 9 Mbit/s of real 1920×1290 video.
  - **type** types into a Chrome text box, first fast and then with pauses.

  It writes `bench/results/<date>-<commit>-<label>.json`, compares two
  results, and runs interleaved A/B tests between agent settings (`ab`).
  Wi-Fi changes from minute to minute, so single runs at different times
  aren't comparable. Throttled links come from a shaping relay on the Mac,
  which needs no sudo (`--net 50@0,3@8,50@16` means 50 Mbit/s, then 3 from
  8 s, then 50 from 16 s).
- **What's graded.** "Content" runs from when the Mac composited a frame
  (or when ScreenCaptureKit delivered it, if that was earlier) to when it was
  drawn in the viewer. The Frame's compositor adds its own delay after that.
  That part is reported, but not graded: an unworn Frame throttles panels to
  about 36 fps after a few seconds, and to 15 fps in standby, whatever they
  draw. This was **verified** with a local canvas page that ran on the Frame
  with no network involved (`bench/pages/present.html`). The compositor's
  share needs a run with the headset worn.

Targets: content p50 ≤ 25 ms (p95 ≤ 40), click to photon p50 ≤ 50 ms
(p95 ≤ 70), 60 fps with ≤ 1% late frames, no stall over 100 ms, and adapting
to a new link rate within 1 s.

Baseline on 2026-09-28 (**verified**, home Wi-Fi, Tailscale, Balanced,
`bench/results/2026-09-28-a3c6e5c-dirty-baseline-fixed.json`; ms p50/p95):

| Scenario | Content | Input to drawn | fps drawn | Notes |
|---|---|---|---|---|
| test (1280×720) | 9.9 | 28.8 / 39.0 | 59 | encode 4.0, network 4.0, decode 1.3 |
| scroll (1920×1290) | 14.7 | – | 50.4 | encode 6.7, decode 6.4 (software), 9.4 Mbit/s, 16% late |
| type (1920×1290) | 16.7 | 51.2 / 73.2 | – | most of the input time is the Mac app reacting |

After this work, with the controller on (**verified**, same setup,
`bench/results/2026-09-28-9e4dcdd-final.json`; ms p50/p95). Content is
now measured from the earlier of display time and delivery, which adds
about 5 ms to scroll compared with the baseline's way of measuring:

| Scenario | Content | Input to drawn | fps drawn | Grades |
|---|---|---|---|---|
| test | 10.5 / 16.7 | 29.6 / 36.3 | 60 | all within target |
| scroll | 19.8 / 27.4 | – | 55.9 | fps, late frames (4.8%) and worst gap (222 ms) only "acceptable": Wi-Fi stalls (the Mac captured 57 fps in this run; earlier runs got 46–53 from virtual displays) |
| type | 15.0 / 21.4 | 43.0 / 60.5 | – | all within target |

Of the targets, click to photon is met without the Frame's compositor (the
headset has to be worn to measure its share), and so is content latency.
The frame-rate and no-stall targets aren't yet met while scrolling.

What was learned (all **verified**, unless marked):

- The biggest costs are encoding (4–7 ms), network (4–6 ms), and decoding
  on the Frame. Chromium XR on the Frame decodes H.264 in **software**.
- Wi-Fi alone stalls for 240–580 ms now and then, over Tailscale and over
  the LAN alike. Over the LAN (`--host 192.168.1.237`) latency was no
  better, but the Frame used about 8% less CPU, because tailscaled runs in
  userspace there.
- With no controller, a link that slows down queues without limit. In one
  run, frames arrived 1.9 s late, and at worst 9.4 s late.
- Tried, and no help, so not kept: VideoToolbox options (require hardware,
  no frame delay, prioritise speed, a hard data-rate cap), Chromium flags
  (`--disable-gpu-vsync`, `--disable-frame-rate-limit`,
  `--use-angle=vulkan`), and a 120 Hz virtual display.
- Inconclusive, so off by default: keeping the Frame's Wi-Fi awake during
  typing (`FRAME_MAC_VIEW_WARM=40`, a tiny message every 40 ms for 5 s
  after input). Over three interleaved runs each, input p50 went 44 → 47 ms
  and p95 92 → 71 ms, and the ranges overlapped widely
  (`…-ab-keepwarm.json`). In that run, and in the baseline's typing, the
  harness typed spaces as "+" (a URL-encoding bug, since fixed), so they
  went through the text path rather than as space keys.
- Pointer moves now go out on an 8 ms timer, not on the page's next
  animation frame, which an unworn Frame slows to 15–36 Hz. This is
  **inferred** to help dragging; the benchmark has no drag scenario yet.
- Kept: the encoder's timestamps never jump more than two frame intervals.
  Before this, the first frame after a pause got a quarter of a second's
  bit budget, and one P-frame reached 204 KB.

## Adapting to the network

`Sources/Controller.swift`, per stream, latency first:

- **The gate.** The viewer acknowledges every frame as it arrives. A new
  frame is sent only while the oldest unacknowledged one is younger than
  the path's usual round trip, plus one frame interval, plus room for this
  link's normal jitter (1.5 times its recent spread, 25–80 ms). So frames
  never queue in SSH, TCP or the Wi-Fi driver. While the link is stuck, the
  newest picture waits and goes out as soon as it moves.
- **The bitrate.** The link counts as congested when, for two checks in a
  row (100 ms apart), round trips grow by more than 40 ms while the stream
  uses much of its budget, or the gate holds frames back, or a frame is
  stuck for 100 ms. Then the bitrate drops to a bit under what actually got
  through: at least a fifth off, and at most half. Once the link has been
  clear for a second, it rises by 10% steps, never above the quality
  setting's bitrate.
- **The tier.** When the bitrate stays low, and the stream is really
  limited by the link rather than having little to send, it steps down:
  60 → 45 → 30 fps, then 75%, then 50% of the pixels. It goes straight to
  the tier the bitrate supports after half a second, and steps back up one
  tier at a time after two seconds with room to spare.
- `FRAME_MAC_VIEW_ADAPT=0` turns it off, for comparison.

Measured on the real Frame, 2026-09-28 (**verified**; interleaved A/B, off
versus on, medians of the runs, ms):

| Link | Scenario | Content p95, off → on | fps drawn, off → on | Result file |
|---|---|---|---|---|
| Clean Wi-Fi (3 runs each) | scroll | 32.3 → 33.6 | 57 → 56.2 | `…-ab-adapt-clean2.json` |
| Clean Wi-Fi | test | 15 → 14.2 | 60 → 59.7 | same |
| 50 → 3 → 50 Mbit/s at 8 s and 16 s (2 runs each) | scroll | **4670 → 72** | 45 → 42 | `…-ab-adapt-step3.json` |
| 50 → 3 → 50 Mbit/s | test | 66 → 16 | 60 → 60 | same |

- **Clean link.** On a clean link it costs nothing measurable. An earlier
  version with a fixed gate slack lost 9 fps to Wi-Fi jitter while
  scrolling (47 → 38 fps), and that's why the slack now follows the link's
  jitter.
- **Throttled link.** Without the controller, frames queued for up to 5.6 s
  and never caught up while the link was slow (p95 1.8–5.6 s, second by
  second). With it, in the two runs:

  | | Run 1 | Run 2 |
  |---|---|---|
  | Worst second's p95 just after the drop | 219 ms | 428 ms |
  | p95 back under 100 ms for 3 s in a row | after 1 s | after 4 s |
  | Stepped down to 1440 px at 30 fps | 1.8 s after the drop | 3.5 s after |
  | p95 per second after that, on the 3 Mbit/s link | 55–94 ms | 60–148 ms |

  Sending one frame takes about 27 ms on that link by itself. After the
  link recovered, the stream was back at full size and 60 fps in about
  7.5 s. It steps up one tier every two seconds, on purpose, so it doesn't
  bounce. The 1 s adaptation target was met in one run of two.
- **A hiccup on a small stream.** In one clean run, a Wi-Fi hiccup made an
  earlier version halve the test pattern's bitrate five times and drop it
  to half size. That cut couldn't help: the stream only sends
  0.47 Mbit/s. Now the controller estimates what a stream wants (captures
  per second × average frame size). While a stream wants about half its
  budget or less, no cut takes it below twice what it wants, so it doesn't change
  tier.

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
  - A benchmark run while wearing the headset. Only then does the Frame show
    panels at full rate, so only then can the compositor's share of the
    latency, and the frame rate you actually see, be measured.

## Limits

- Only windows on the Mac's current desktop (Space) are listed, and
  minimised windows can't be captured.
- A window's panel shows only that window. Its menus and sheets are separate
  windows on the Mac, so open them from the Mac or use **Whole screen**.
- Keys go to whichever Mac window is in front. Clicking a panel brings its
  window to the front first.
- Ctrl stays Ctrl. On the Mac, copy is ⌘C, so use Meta+C on a keyboard paired
  with the Frame.
