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
- **Windows-only overlay tools** (Desktop+, OVR Toolkit, OVRdrop) do this for a
  PC's desktop in SteamVR. They don't run on the Frame's standalone Linux.

## Frame Control's panel switcher

**Verified 2026-09-28**, SteamOS 0.4.1, BUILD_ID `20260925.6191901`,
SteamVR 2.18.1: **Tools → Panel switcher** lists SteamVR's open main panels,
including panels that are currently hidden. **Show** asks SteamVR to bring one
forward. **Open in headset** opens the same switcher as its own panel; choose
it again from Steam's dashboard after switching away. Refresh updates the list.
This is a list, not thumbnail Exposé.

![Frame Control's switcher rendered on the Frame](img/panel-switcher.png)

This is our own Python/HTML implementation (`ui/frame_panels.py`), using the
Frame's shipped `vrcmd` OpenVR client and gamescope. The headset page uses
Chromium (Chromium XR when present, then system Chromium, then the existing
Chromium Flatpak). No XSOverlay, OVR Toolkit, WayVR or other overlay application
is needed. This dependency boundary also applies to future layout and panel
persistence work: platform APIs and bundled libraries are fine; another app
must not implement the feature for us.

The companion runs the helper over SSH. Opening it in the headset installs a
copy under `~/.local/share/frame-control/panels/` and starts a loopback HTTP
server and an isolated Chromium profile. There is no startup service or global
setting change. Close the switcher to stop its server and browser. Other
Chromium profiles, Steam and SteamVR are left alone. If the window or runtime
closes, use **Open in headset** again.

The page carries a random, per-process access key in its URL fragment, removes
it from the address bar, keeps it in tab session storage for page reloads, and
sends it in a header. Panel lists and actions need
that key; Host and Origin checks reject other sites. The key permits only
listing panels, requesting focus and closing this switcher. Like Mac viewer
launch tickets, it is initially readable by another process running as the
same Frame user. Panel titles are rendered as text, never HTML. The companion
retains its existing request guards. No Mac capture credentials cross this API.

**Verified:** the real headset page rendered its panel list (image above), its
HTTP focus request changed `GAMESCOPE_FOCUSED_APP` to `2000999030`, a request
without the key returned HTTP 403, and Close stopped the helper and its browser.
Opening an already running switcher requests its focus rather than creating a
second one. The companion uses the same list/focus helper. **Unverified:** laser
selection while wearing the headset, physical placement, and non-XR Chromium.
The API reports that focus was *requested*: another action can take focus before
we observe the result. Closed panels are rejected after re-enumeration.

### Shared-device recheck, 2026-09-29

**Verified:** the follow-up's atomic `mkdir /tmp/frame-test.lock` attempts
failed because another thread held the lock. The existing lock was left alone;
no applications were installed, launched or stopped in this follow-up. The last
read-only battery check showed 62%, charging. The 180 Python and 8 website tests
passed again locally.

**Unverified in this follow-up:** the prepared browser-button test (Refresh,
selection, reload and Close) and repeated OpenXR transition could not run under
the shared lock. The device results elsewhere in this page are the earlier
2026-09-28 observations, not results from this blocked recheck. In particular,
HTTP focus is not evidence of worn-headset laser input. Follow the
[shared-device test procedure](testing.md#headset-smoke-test) for the next run.

## Saved spatial layouts: blocked on the current panel route

**Verified 2026-09-28**, same build, using a temporary xterm panel with
`STEAM_GAME=2000999031` and `FnTable:IVROverlay_028` from
`/opt/steamvr/bin/linuxarm64/libopenvr_api.so`:

| OpenVR call | Result |
|---|---|
| `FindOverlay("valve.steam.desktopgame.2000999031")` | Success |
| `GetOverlayWidthInMeters` | Success, 2.67 m |
| `SetOverlayWidthInMeters` (same width) | Success |
| `GetOverlayTransformType` | Success, type 5 (`VROverlayTransform_DashboardTab`) |
| `GetOverlayTransformAbsolute` | 18 (`WrongTransformType`) |
| `SetOverlayTransformAbsolute` (identity rotation, 1.2 m up, 1.5 m forward) | 12 (`PermissionDenied`); type remained 5 |

The public interface names type 5 **DashboardTab**; SteamVR's dashboard code
places these panels through its scene graph. It owns the frame/docking
transforms. A successful width setter does not grant permission to restore the
position. `vrcmd --dock-overlay world <key>` dispatched a docking request but
the dashboard logged `Failed to get SGTransform in setInitialTransformForLocation.
Invalid transform ID`. This does not establish working world placement.

**Inferred:** saving X11 pixel rectangles or Mac window IDs would not restore
this spatial arrangement. Mac window IDs also change when an application
reopens; viewer tickets and reconnect keys must not go into a layout file.
The base Mac stream reconnects after a network break, but that is different
from recreating windows and their room positions after a reboot.

There is consequently no Save/Restore control yet. A durable layout needs a
working transform restore path, stable source identity, and a fresh capture
permission/ticket flow. The tested gamescope-owned overlay route denies that
transform operation. A future Frame Control-owned overlay renderer, or a
supported platform API for dashboard frame transforms, needs its own device
proof before building layout UI. This is a blocker for the current approach,
not a claim that all possible implementations are impossible. Reboot recovery
was not tested: the shared headset was not rebooted.

## Panels during an immersive session

**Verified 2026-09-28**, same build: our Chromium switcher panel remained in
OpenVR's overlay list before, during and after the Frame's shipped `helloxr -g
Vulkan` sample. During the test `vrcmd --stats` identified
`system.generated.openxr.helloxr.helloxr`, with 242 frame submissions. The test
ended only its own sample process; no SteamVR, Steam, power or global settings
were changed. The switcher was still selectable afterwards.

This proves survival of that panel across an OpenXR scene session, **not** that
it stayed visibly composited over the scene: OpenVR reported it `not_visible`
before, during and after. **Verified:** calling `ShowOverlay` on our
*gamescope-owned* switcher overlay returns 12 (`PermissionDenied`). A helper
cannot force that panel visible using the public overlay call. Use the
switcher/dashboard to request access to it; we do not fight the runtime with a
repeated force-focus loop.

**Verified in a second controlled run:** a live H.264 test-pattern stream from
this checkout's Mac helper, through its own SSH tunnel and a temporary Chromium
profile, survived the same OpenXR sample (257 scene-frame submissions). Its
panel `2000999032` changed from `visible` before launch to `not_visible` during
and after the scene. The Mac helper still reported the same `test` stream;
captured frames increased from 35 to 232, with 29.5 decoded/drawn fps afterwards.
The test did not capture personal Mac windows or inject Mac input. The sample,
viewer, temporary profile, tunnel and Mac helper were cleaned up. This proves
stream survival, and also shows why it must not be advertised as always visible.

**Unverified:** persistent visible placement while playing a Steam-launched VR
game, Plasma desktop and real Mac-window behavior during that launch, and worn
headset input. Other threads were launching games and changing the runtime on
the shared device, so those transitions were not treated as controlled evidence.
A runtime/X-server restart can destroy the viewer windows; a network reconnect
cannot recreate them. No “always visible during games” guarantee is shipped.

## Keyboard passthrough feasibility

**Verified 2026-09-28**, same build, using `FnTable:IVRTrackedCamera_006`:
`HasCamera(0)` returned success and true. `GetCameraFrameSize` returned 100
(`OperationFailed`), with zero dimensions, for all three public frame types
(distorted, undistorted and maximum-undistorted), including after acquiring the
video service. Acquisition returned success and a handle; release returned 101
(`InvalidHandle`). The probe shut down its OpenVR client afterwards. No camera
frames were captured and no camera settings were changed.

**Documented:** the public OpenVR camera interface provides camera frame sizes,
intrinsics, projections and streaming handles; these are prerequisites for a
spatially aligned camera cutout. See Valve's
[OpenVR C API](https://github.com/ValveSoftware/openvr/blob/master/headers/openvr_capi.h).

**Inferred:** camera presence alone does not establish access to camera pixels.
The failed frame-size path blocks a keyboard cutout in our current panel
implementation. We have not established a keyboard detector or a calibrated
camera-to-panel mapping. Built-in full-room passthrough is not proof of a
public, selectively masked camera stream. No keyboard cutout is offered, and
no third-party camera/overlay app is substituted for it.
