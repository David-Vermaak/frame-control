# PC in the headset

Windows and Linux hosts use **Tools → PC in the headset**. The host shares a
window or screen, and the existing Frame viewer makes it a SteamVR panel.
Move it with the dashboard's Float in World, Move and Size controls.

**Inferred / not yet verified on a desktop host:** the Windows and Linux
capture and input paths below. This work was developed on a Mac with no
Windows or Linux desktop VM. A native build or test-pattern test in CI does
not establish that desktop capture, a permission dialog, hardware encoding
or laser input works. Keep this feature in the draft/testing stage until
those paths have been tried on real hosts.

## Own implementation, platform APIs and bundled libraries

Frame Control owns the host agent, input routing, authentication, streaming
protocol, panel launcher and adaptation. It does not launch or require
Sunshine, OBS or another desktop-streaming app. GStreamer and its codec
plugins are ordinary libraries bundled with the Windows and Linux app;
users do not install a GStreamer application. The shared library build keeps
license texts and package provenance alongside the libraries. Linux also
bundles the PipeWire client’s dynamically loaded SPA/protocol modules and a
private client configuration; it does not change the desktop’s configuration.

First-party alternatives considered (**documented**): Valve Remote Play
streams a game/desktop, rather than providing this per-window panel protocol;
Windows Remote Desktop opens a remote session; Linux's desktop portal is the
consent mechanism for sharing the current desktop. The chosen paths are:

| Host | Capture | Encoding | Input |
|---|---|---|---|
| Windows | Windows.Graphics.Capture, through `d3d11screencapturesrc capture-api=wgc`; HWND or HMONITOR | Hardware Media Foundation (`mfh264enc`), low latency, no B-frames | `SendInput`, with source bounds and per-monitor DPI awareness |
| Linux | RemoteDesktop + ScreenCast portal, then the returned PipeWire fd/node | VA-API (`vah264enc`) where registered; x264 otherwise | RemoteDesktop portal notifications, using only granted pointer/keyboard devices |

API choices are **documented**, not device verification:
[Windows capture](https://learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture),
[GStreamer WGC](https://gstreamer.freedesktop.org/documentation/d3d11/d3d11screencapturesrc.html),
[Media Foundation encoder](https://gstreamer.freedesktop.org/documentation/mediafoundation/mfh264enc.html),
[ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html),
[RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html).

Windows needs a WGC-capable Windows 10/11 desktop and an available hardware
Media Foundation H.264 encoder. Elevated windows and the secure desktop
cannot be driven by an ordinary Frame Control process. If Windows refuses
to focus a selected window, input is refused rather than sent to the app
covering it; bring the selected window forward, then Stop and Show again. Minimized/closed
windows may stop producing frames. Protected content is not supported.

On Linux, press **Choose a window or screen…** and approve the desktop's
sharing dialog. Choose another source to add another panel. Stop releases
that source's portal session; sharing it again asks for consent again.
A desktop must implement both ScreenCast and RemoteDesktop for this path;
a ScreenCast-only compositor cannot provide laser input through this API.
Cancelling or denying a dialog is reported on the card. No portal permission
is bypassed, and Frame Control does not open `/dev/uinput` or the unrestricted
PipeWire daemon on the host.

The host's own keyboard still works. Input from the viewer uses normalized
picture coordinates, maps through the selected source's bounds, and releases
held buttons/keys on blur, disconnect and Stop. Linux requires the pointer
and keyboard grants. **Untested:** desktop-specific consent, mixed-DPI
Windows input alignment, multi-monitor layouts, hardware encoder behavior,
window resize/minimize, and non-US keyboard layouts.

## Shared pieces

- `ui/frame_macview.py` owns the SSH tunnel, reconnect supervision, quality
  presets and panel launch for all hosts. `ui/frame_pcview.py` selects the PC
  helper; `/api/macview` remains the compatible endpoint.
- `ui/mac-view.html` is the one viewer. The 17-byte big-endian frame header,
  Annex-B H.264/JPEG payloads, `hello`/`ack` reconnect handshake, clock sync,
  `rx`/`fd` timing reports and input messages are unchanged.
- `desktop/controller.c` is the rate controller shared by the Mac Swift
  binding and the PC Python binding. Capture is gated **before** encoding;
  encoded reference frames are never discarded. A bounded raw-frame queue
  keeps the newest picture, including the last update of an idle window,
  until the gate opens. A native one-frame-source test covers that case.
  It keeps the Mac's bitrate demand protection and tier hysteresis.
- PC records use the existing `Stats.swift` JSON schema, with bounded
  4096-frame/512-input storage in `ui/frame_stream_stats.py`. The benchmark's
  analysis, targets and network shaping are shared, not reimplemented.
  Capture timestamps describe the native pipeline's source time; they do
  not prove the time at which the host compositor displayed the pixels.
- Mac virtual-display separation remains Mac-only. Windows WGC and the
  Linux portal share the selected window directly.

PC capture follows the shared frame-rate and resolution tiers. x264 updates
bitrate while running. Hardware encoders are drained and reopened when the
budget changes materially, at most once a second, because their live property
support varies. Reopening starts a new keyframe and retains the consented
portal session. **Untested:** hardware reconfiguration latency and whether a
particular desktop permits reconnecting its PipeWire stream this way.

## Build and measure

Packaged Windows/Linux builds include `desktop/bundle/pc-host` and its shared
libraries. Source checkouts build them with `python3 desktop/build.py` after
installing GStreamer development packages (see the `PC host libraries` CI
workflow). The feature reports a missing bundle; it does not download or
install a streaming app on first use.

The existing benchmark now accepts a PC host:

```sh
python3 scripts/macview-bench.py run --pc --scenario test --label pc-test
# Linux: select a real source in the desktop's sharing dialog
python3 scripts/macview-bench.py run --pc --scenario capture --source choose --label linux-window
# Windows: use the HWND/monitor source ID shown by the host's /windows or /displays
python3 scripts/macview-bench.py run --pc --scenario capture --source window:12345 --label windows-window
```

The synthetic PC pattern uses bundled x264 so headless CI can verify the
wire protocol without claiming that a GPU was exercised. The `capture`
scenario measures the selected real source without injecting input or
assuming that it animates at 60 fps. Mac-only Chrome/virtual-display typing
and scrolling automation is not run on PC hosts. Results retain the same
latency stages and record `host_platform`, `pc_host` and `source`. CPU sampling
on PC hosts is explicitly unavailable. `--net` and `--delay` still use the
same bounded shaping relay, without administrator privileges.

## Evidence

- **Verified, Mac, 2026-09-28:** the final full unit/integration suite ran
  183 tests successfully, including the real Mac helper's H.264, ticket, timing
  and input-echo tests. The native PC test class was skipped locally because
  its libraries were absent; the PC adapter and shared-controller tests passed.
- **Verified, real Frame, 2026-09-28, BUILD_ID 20260925.6191901:** the base
  helper's synthetic source created panel `valve.steam.desktopgame.2001639889`,
  and the shared Chromium viewer decoded H.264. It recorded 286 frames over
  the short probe, with a two-second summary of 19.5 fps shown and total
  latency p50/p95 83/156.5 ms. This establishes the existing viewer/transport
  route, not Windows/Linux capture, input or a latency target. The probe's
  helper, tunnel and viewer were stopped afterward.
- **Verified in CI:** native library builds and real x264 protocol tests passed
  on Windows, Ubuntu x64 and Ubuntu ARM64 in
  [run 36422214445](https://github.com/saphid/frame-control/actions/runs/36422214445).
  All four installer builds passed in
  [run 36422214425](https://github.com/saphid/frame-control/actions/runs/36422214425).
  These are build/synthetic tests, not desktop-host verification. No VM was used.
- **Verified, real Frame, same build/date:** the ARM64 PC agent and its
  bundled libraries ran from a temporary user directory, using x264's moving
  test pattern. Traffic travelled Frame → Mac SSH relay → Frame viewer.
  The shared bench recorded 351 drawn frames, content p50/p95 15.1/80.3 ms,
  and 34.8 fps. The frame-rate and late-frame targets failed. This checks the
  new agent and real viewer together; it is not a representative PC link.
  The helper exited 0, its viewer/tunnels stopped, and its directory was removed.
  [Raw benchmark result](../bench/results/2026-09-28-c5fc552-dirty-pc-agent-frame-arm64-hairpin.json).
  This first probe's input echo measured message receipt to the next capture,
  not a visible pattern response; later builds make test clicks change its color.
- **Verified, Mac:** all 600 states in a 60-second congestion/recovery trace
  matched the original Swift controller. `tests/test_pc_controller.py` retains
  the original trace digest as a regression check.
- **Verified, real Frame, agent at `cd20243`:** the repeated synthetic
  probe drew 400 frames at 39.3 fps, content p50/p95 15.1/28.6 ms, and synthetic
  input-to-drawn p50 76.6 ms. Test clicks now change the pattern color before
  injection is timestamped. The frame-rate/late-frame targets still failed;
  this remains a Frame-hosted x264 test through a Mac relay, not a desktop or
  physical-laser measurement. Helper exit 0 and cleanup succeeded.
  [Latest device probe result](../bench/results/2026-09-28-cd20243-pc-agent-frame-arm64-final.json).
- **Untested:** real Windows WGC → Media Foundation → Frame; real Linux
  portal → PipeWire → VA-API/x264 → Frame; physical laser input on either.
  No benchmark numbers for those desktop paths are claimed.

## Independent review availability

A direct read-only review was attempted with
`devin -p --model swe-2-max --permission-mode auto --prompt-file …`. The first
attempt exited 0 after rejecting a tool that needed interactive permission;
it did not inspect the diff. A full inline-diff attempt returned no output
for 15 minutes and was terminated (shell exit 143). A smaller inline native
code review returned no output within 300 seconds (process exit -15).
SWE-2 Max was requested; no completed review or findings were received, so
independent review is **unverified**, not a passed check. The PR remains draft.
