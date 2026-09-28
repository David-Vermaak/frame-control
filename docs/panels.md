# Arranging windows in space

The confidence labels are the same as in [ssh.md](ssh.md).

## The short version

- The in-headset **Linux desktop is one flat panel**: a nested Plasma session,
  fixed at 1280×800, drawn into a single SteamVR overlay. Windows *inside* it
  are arranged by KWin inside that rectangle. They can't leave it.
- Every **Steam app gets its own panel**. gamescope runs with
  `--virtual-connector-strategy PerAppId`, so each distinct app id becomes a
  separate SteamVR overlay named `valve.steam.desktopgame.<appid>`.
- To float a Linux app on its own, run it on gamescope's X display (`:0`)
  instead of in Plasma, and tag its window with an app id of its own.
  `scripts/panel-on-frame.sh` does this:

```sh
./scripts/panel-on-frame.sh konsole                    # a terminal, as its own panel
./scripts/panel-on-frame.sh --name notes -- kate '~/notes.md'   # quote ~ so the Frame expands it
./scripts/panel-on-frame.sh org.mozilla.firefox        # a Flatpak
./scripts/panel-on-frame.sh mac-screen                 # the Mac's screen (Remmina/VNC)
```

- Then **place each panel with the SteamVR dashboard's docking controls**:
  **Float in World**, **Move**, **Size**, **Toggle Curvature**, dock on the
  left or right controller, **View in Theater**, and **Multitasking View**.

## How a panel is born (verified 2026-09-25)

gamescope's command line on the Frame includes:

```
--backend openvr --xwayland-count 2 --virtual-connector-strategy PerAppId
--vr-overlay-key valve.steam.gamepadui.fallback
--vr-app-overlay-key valve.steam.desktopgame
--vr-overlay-physical-width 2.67 --vr-overlay-enable-control-bar
--nested-width 1280 --nested-height 720
```

gamescope reads each X11 window's `STEAM_GAME` property as its app id. That's
the same property Steam sets on games it launches. On a new id, Steam's
SteamVR system UI logs:

```
[Overlays] Created: valve.steam.desktopgame.7777777
[Overlays] Created: valve.steam.desktopgame.7777777.layer1 … layer7
```

The test: an `xterm` on `DISPLAY=:0`, tagged with
`xprop -id <win> -f STEAM_GAME 32c -set STEAM_GAME 7777777`, produced the
overlay above. Two more apps with different ids (`konsole`, `xterm`) produced
two more overlays, and all three were listed together in the root property
`GAMESCOPE_FOCUSABLE_APPS`. **Not yet checked by eye:** how the new panels
look in the headset and how they handle input.

Untagged windows on `:0` get app id 0 and share the default panel. Plasma
itself (`kwin_wayland`, pid in `GAMESCOPE_FOCUSABLE_WINDOWS`) is one of those.

### What `panel-on-frame.sh` does

1. Sets `DISPLAY=:0`, unsets `WAYLAND_DISPLAY`, and forces X11 in the
   toolkits (`QT_QPA_PLATFORM=xcb`, `GDK_BACKEND=x11`, `SDL_VIDEODRIVER=x11`,
   `MOZ_ENABLE_WAYLAND=0`). A Wayland-only app would connect to gamescope's
   own Wayland socket and not get tagged.
2. Starts the app detached (`setsid nohup`), so it outlives SSH.
3. Diffs the root window's children before and after, and sets `STEAM_GAME`
   on each new mapped top-level window. It keeps watching about 3s after the
   first window (for splash screens), up to 20s in total (for slow Flatpaks).
   It gives up early if the app exits before showing a window.
4. The id comes from `--id`, or is derived from `--name`/the command in the
   range 2,000,000,000–2,000,999,999, far above real Steam app ids. The same
   label always gives the same id.

Limits:

- **Single-instance apps** (Remmina, most KDE apps with a running copy in
  Plasma) hand the request to the existing process, so the window opens
  wherever that process lives. Close the app in Plasma first.
- A window the app opens later (a dialog, a second window) isn't tagged, so it
  lands on the default panel. Tag it by hand:
  `ssh frame 'DISPLAY=:0 xprop -id <win> -f STEAM_GAME 32c -set STEAM_GAME <id>'`
  (find `<win>` with `DISPLAY=:0 xwininfo -root -children`).
- The script tags *any* new window on `:0` during its watch window, so a
  Steam popup that opens in those few seconds would join the panel too. For
  the same reason, run one `panel-on-frame.sh` at a time. If a stray window
  is tagged first, the script can report success while the app's own window
  stays on the default panel; check in the headset.
- Each panel renders at gamescope's nested size (1280×720), not the Plasma
  desktop's 1280×800.
- Steam treats the tagged id as "the current game": it applies a generic
  controller config and logs `Failed to get app info` for the made-up id. So
  far this hasn't caused anything worse.

## Placing panels: the SteamVR dashboard (inferred from SteamVR's UI code)

The Frame's SteamVR dashboard
(`/opt/steamvr/resources/webinterface/dashboard/`) wraps each overlay in a
frame with a **dock location**: `Dashboard`, `World`, `Theater`,
`LeftController`, `RightController`. The strings and handlers are there
(`dashboard_english.json`, `systemui.js`):

| Control | What it does |
|---|---|
| **Float in World** | Only shown while the panel is docked on the dashboard. Detaches it into the room, where it stays after the dashboard closes. |
| **Move** / grab handle | Push, pull and drag the panel. *Grab Handle Acceleration* in SteamVR settings speeds up push and pull. |
| **Size** | Resize the floating panel. |
| **Toggle Curvature** | Flat vs curved. |
| **Dock on Left/Right Controller** | Attach to a controller, like a wrist screen. |
| **Dock on Dashboard / Return to Dashboard** | Put it back. |
| **View in Theater** / Show/Hide Theater Screen | Shows the panel as a large theater screen. |
| **Multitasking View** | Shows every open panel together (only if `VRHTML.BSupportsMultitaskingView()`). |
| **More Options** (…) | Where the less common docking actions live. |

**Still to check in the headset:** where exactly each control appears, whether
floating positions survive a panel closing and reopening, and whether there's
a limit on the number of floating panels.

## Other routes

- **Just the desktop somewhere else**: float the Plasma panel itself. No
  script needed.
- **Inside the desktop panel**: KWin tiling (Meta+arrow keys with a Bluetooth
  keyboard) or virtual desktops arrange windows within the 1280×800 rectangle.
- **Optional overlay tools:** Desktop+, OVR Toolkit and similar software are
  separate from Frame Control. Public reports describe some Proton support;
  Windows-only does not by itself prove a Frame app cannot run. Local status
  and sources are in [VR utilities](vr-utilities.md).
- **Our performance HUD:** Home → VR comfort and performance → Open HUD in
  headset creates its own gamescope panel using built-in tools. It needs no
  third-party overlay app. [Metrics and verification](vr-utilities.md).
