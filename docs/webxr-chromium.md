# WebXR in Chromium on the Frame

Goal: open a web VR180 or 360 player (DeoVR and DL8 embeds, WebXR samples),
press its VR button, and watch in 3D in the headset.

The build and installer now live in their own public repo,
[saphid/chromium-webxr-steam-frame](https://github.com/saphid/chromium-webxr-steam-frame):
a build script for an x86-64 Linux host, the SO_PEERCRED patch, and a
Frame-side installer that adds "Chromium XR" to the Steam library. This page
keeps the findings and what was verified on this Frame.

## Why Flathub Chromium can't

**Verified 2026-09-25** (Frame BUILD_ID 20260922.6101926, Flathub
`org.chromium.Chromium` 154.0.8037.57 aarch64):

- `navigator.xr` exists, but `isSessionSupported("immersive-vr")` is `false`.
  Flags don't change that, and neither does `--force-webxr-runtime=openxr`
  with SteamVR exposed to the Flatpak.
- The binary has no OpenXR loader: no `XR_RUNTIME_JSON`,
  `xrGetInstanceProcAddr` or `XR_LOADER_DEBUG` strings. The only OpenXR
  strings are the `chrome://flags` entries.

**Cause (verified against Chromium source, same date).** M154 is the first
release that compiles OpenXR on Linux:

- `device/vr/buildflags/buildflags.gni` adds `is_linux` to `enable_openxr`.
- Flathub's tarball sets `checkout_openxr = true`.
- Flathub's GN args don't turn it off.

But `content/services/isolated_xr_device/xr_runtime_provider.cc` only creates
the OpenXR device under `ENABLE_OPENXR && IS_WIN`. That's true on 154, 155 and
`main`. Nothing on Linux calls the OpenXR code, so the linker drops it. The
rest of the Linux port is in two unmerged CLs (bug 506004811):

- [8441736](https://chromium-review.googlesource.com/c/chromium/src/+/8441736)
  runs the XR device service in a Linux sandbox that allows SteamVR's
  sockets, `/dev/shm` and `flock`.
- [8132979](https://chromium-review.googlesource.com/c/chromium/src/+/8132979)
  wires the provider to `OpenXrPlatformHelperLinux`. `kOpenXR` stays off by
  default, so it needs `--enable-features=OpenXR`.

8132979 sits on top of 8441736, so fetching `refs/changes/79/8132979/<ps>`
gets both.

**The Frame side is ready.** `~/.config/openxr/1/active_runtime.json` names
SteamVR (`bin/linuxarm64/vrclient.so`, `VALVE_runtime_is_steamvr`). The
Linux backend uses Vulkan (`XR_USE_GRAPHICS_API_VULKAN`).

## Building and installing it

Follow the [public repo's README](https://github.com/saphid/chromium-webxr-steam-frame#build).
In short: `build/build.sh` on an x64 Linux host (no sudo, about 90 GB of
disk) produces `chromium-xr-arm64.tar.xz` (about 145 MB), and
`frame/install.sh` on the Frame unpacks it to `~/chromium-xr`, installs the
`chromium-xr` launcher in `~/.local/bin`, and adds the Steam library shortcut
through the Steam client's DevTools port, the same way as T3 Code
([apks.md](apks.md)). Launching the shortcut gives Chromium its own panel,
`valve.steam.desktopgame.<appid>`, like any other app.

First build, 2026-09-25, on a 12-thread, 31 GB x64 Linux box: 9 h 33 min for
94,835 steps, giving Chromium 156.0.8071.0. A rebuild after a one-file change
takes under a minute, plus about 4 minutes to repack.

To debug from the Mac, launch it as a panel with DevTools on the Frame
(verified 2026-09-27):
`scripts/panel-on-frame.sh -- '~/.local/bin/chromium-xr' --remote-debugging-port=9223 URL`
([panels.md](panels.md)). DevTools has no authentication. It listens on
loopback, but with the userspace Tailscale from [tailscale.md](tailscale.md)
running, loopback ports are reachable from your tailnet. Close the browser
when you're done. Chromium runs one browser per profile, so close the
Steam-launched one first or the flag is ignored.

**The SO_PEERCRED fix.** The XR seccomp policy refuses `getsockopt`. SteamVR's
client calls `getsockopt(SOL_SOCKET, SO_PEERCRED)` inside `xrCreateInstance`,
so the XR process died with a seccomp crash (arm64 syscall 209). The patch
allows that one option. It's needed but not enough: the launcher still turns
seccomp off (below), so the patch only matters once that's fixed too.

**Seccomp is off.** The launcher passes `--disable-seccomp-filter-sandbox`.
With the XR seccomp policy on, SteamVR's client reads `/proc/self/status`
through Chrome's file broker and gets the broker's pid. SteamVR then binds
the app to the wrong process ("Unable to init path manager:
VRInitError_Init_Internal") and `xrCreateInstance` fails. The broker can't
answer `/proc/self` for another process, so fixing this needs a change in
Chromium's broker client or in the CL. The namespace sandbox stays on, but
seccomp is off for every process, so use this profile for VR sites rather
than everyday browsing.

**Upstream (2026-09-27).** CL 8441736 (the XR sandbox) has merged into
Chromium, still refusing `getsockopt`; CL 8132979 is still in review. Valve
and the CLs' author are working on Steam Frame support
([utzcoz/chromium-webxr-linux#5](https://github.com/utzcoz/chromium-webxr-linux/issues/5)).
Both sandbox problems above, with the patch, are reported in
[utzcoz/chromium-webxr-linux#7](https://github.com/utzcoz/chromium-webxr-linux/issues/7).

**Verified 2026-09-26** (Frame BUILD_ID 20260922.6101926, SteamVR 2.17.10,
this build):

- `isSessionSupported('immersive-vr')` is `true`. The WebXR samples page
  shows "VR support detected".
- `requestSession('immersive-vr')` succeeds after the prompt. With a WebGL
  layer, the first XR frame has a viewer pose with 2 views and a
  2880 × 1440 framebuffer (1440 × 1440 per eye).
- SteamVR moves the app from `VRApplication_OpenXRInstance` to
  `VRApplication_OpenXRScene` and gives it scene focus. `xrEndFrame` submits
  both projection views, and the compositor receives the 2880 × 1440 scene.
- The OpenXR runtime uses Vulkan (`XR_KHR_vulkan_enable2`). Chromium's own GPU
  process uses ANGLE on GL, running on zink over Turnip Vulkan (Adreno 750);
  Chromium's Vulkan backend is off. That doesn't stop the session.
- Unprivileged user namespaces work (`unshare -Ur true`), so the namespace
  sandbox runs without the setuid `chrome_sandbox`.
- **With the headset on** (same day, seccomp sandbox off): the WebXR
  samples' Immersive VR Session showed its scene in the headset, and SteamVR
  loaded the Frame controller bindings for the app. The three.js
  [`webxr_vr_video`](https://threejs.org/examples/webxr_vr_video.html) demo,
  a stereo 360 video, played in 3D after pressing Enter VR.

- **Launched from the Steam library, verified remotely 2026-09-27** with
  nobody wearing the headset (standby workaround in
  [how-the-frame-works.md](how-the-frame-works.md)). The installer's Steam
  shortcut starts Chromium, and SteamVR takes it as scene app
  `steam.app.<shortcut id>`. A minimal WebXR session that clears every frame
  to red ran at about 75 frames per second, and the stereo headset capture
  showed both eyes solid red. Steam preloads its overlay
  (`gameoverlayrenderer.so`), which crashed Chromium's zygote about 30 s after
  a Steam launch. The public repo's launcher now removes it from
  `LD_PRELOAD`. With the headset outside its playspace, SteamVR shows
  passthrough wherever the page leaves transparent pixels.

- **Frame rate and input, measured 2026-09-27** (standby workaround, red
  test session): 72 fps with every frame at 13.9–14 ms over 16 s, and SteamVR
  dropped frames only at startup. The right controller showed up as an
  `oculus-touch` `tracked-pointer` with an `xr-standard` gamepad and a
  25-joint hand, with poses on every frame. A real squeeze reached the page
  as `squeezestart`/`squeeze`. Haptics aren't exposed (no actuators).
  Details are in the public repo's technical notes.

**Not verified yet:** trigger, thumbstick and face buttons, the left
controller, bare-hand tracking, and third-party VR180 players.

- Frame rate and dropped frames during playback (nothing was measured; it
  looked fine).
- Third-party VR180 players (DeoVR and DL8 web embeds).
- Controller and hand input inside a WebXR page.
