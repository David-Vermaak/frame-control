# PC VR streaming from Linux

**Recommendation, 2026-09-28:** test Valve's current SteamVR/Steam Link path
on a Linux gaming PC before building another streamer. Valve now documents
Linux streaming fixes and USB support. We have no Linux host attached, so
Linux-to-Frame VR streaming remains **unverified here**.

This is the feasibility and options report for
[#24](https://github.com/saphid/frame-control/issues/24), not a shipped streaming
feature. Frame Control's features must use our own implementation or standard
platform components. WiVRn and ALVR are research comparisons, not dependencies.
An optional install shortcut is the most we would offer for a third-party app.
Our own streamer requires Alex's choice before implementation.

## What was checked on the Frame

**Verified** on 2026-09-28: aarch64, SteamOS **0.4.1**, BUILD_ID
`20260925.6191901`, SteamVR **2.18.1**. Version and build are recorded separately;
earlier docs associate this build with other SteamOS version labels.

| Client | Installation | Runtime result |
|---|---|---|
| WiVRn **26.9**, upstream `WiVRn-release.apk` | API 29, arm64-v8a; installed in its own immersive Lepton instance | OpenXR instance creation fails: missing `XR_KHR_convert_timespec_time`. Both 1.1.58 and 1.0.58 attempts return `XR_ERROR_EXTENSION_NOT_PRESENT` |
| ALVR **20.14.1**, upstream `alvr_client_android.apk` | API 26, arm64-v8a; installed in its own immersive Lepton instance | Same missing extension. Client panics at `client_openxr/src/lib.rs:220` with `ERROR_EXTENSION_NOT_PRESENT` |

The [evidence excerpt](evidence/linux-vr/2026-09-28.txt) includes APK SHA-256s,
upstream release links, loader errors and cleanup results. These are failures
before an OpenXR session, not successful VR clients. WiVRn was launched twice;
ALVR's container remained up despite its client panic. Container liveness alone
does not establish VR compatibility.

Both APKs already declare `MAIN` and `LAUNCHER`. They were installed unmodified
using this branch's existing `python3 ui/frame_android.py install APK --vr`,
then launched through their Steam shortcuts. Logs came from the instance's
`podman exec … /system/bin/logcat`; the user journal also retained WiVRn's errors
after its container exited. No headset was worn and no host was connected.

All test app files, compatdata, shortcuts and containers were removed afterwards.
SteamVR's original process remained running. No global settings changed.

### Relation to the VR APK branch

**Documented from source:** [PR #20](https://github.com/saphid/frame-control/pull/20)
was read, not edited (branch inspected at
[`038dcd4`](https://github.com/saphid/frame-control/commit/038dcd48cd75336f6a86c63c7878bfc9c52deec9)).
Its compatibility layer handles OpenXR version negotiation, some controller
profiles and refresh-rate requests. It does **not** implement
`XR_KHR_convert_timespec_time`. Its launcher fix is unnecessary for these APKs.
This report has **no unmerged code dependency** on that PR, and neither APK was
tested with its layer injected.

**Documented from upstream source:** WiVRn requests the extension in
[`application.cpp`](https://github.com/WiVRn/WiVRn/blob/bbc6e4cc36c355fa6180980abd231673dc15115d/client/application.cpp#L1286)
and uses it to convert `CLOCK_MONOTONIC` into `XrTime` in
[`instance::now()`](https://github.com/WiVRn/WiVRn/blob/bbc6e4cc36c355fa6180980abd231673dc15115d/client/xr/instance.cpp#L335).
ALVR also [requests it unconditionally](https://github.com/alvr-org/ALVR/blob/a9f6542fa507a841f40ab4f3fcb531427cd02550/alvr/client_openxr/src/lib.rs#L188).
Simply deleting the extension request or returning made-up timestamps would
not prove correct tracking or timing. A real fix needs a valid clock mapping
and further runtime tests. No such patch was made.

### Native SteamOS aarch64 clients

**Verified:** the Frame has a native OpenXR runtime manifest at
`~/.config/openxr/1/active_runtime.json`, pointing to SteamVR's
`bin/linuxarm64/vrclient.so`.

**Documented:** WiVRn's [26.9 README](https://github.com/WiVRn/WiVRn/blob/bbc6e4cc36c355fa6180980abd231673dc15115d/README.md)
describes its Linux client as debugging-only, without audio or hardware decode.
ALVR 20.14.1's [non-Android decoder](https://github.com/alvr-org/ALVR/blob/a9f6542fa507a841f40ab4f3fcb531427cd02550/alvr/client_core/src/video_decoder/mod.rs)
returns no decoded frames. The inspected releases ship Android clients, not a
ready-to-run native Frame client.

**Inferred:** a native port is possible research, but neither release offers a
demonstrated native alternative to the blocked APKs. Native builds, native
extension enumeration, hardware decoding and audio were **not tested**. The
Android extension failure does not establish that the native runtime lacks it.

## (a) Valve's own path — recommended first

**Documented**, from Valve's release notes rather than launch-window reports:

- [SteamVR 2.17.8 beta](https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/1842212951314598)
  says “Fix crash using Steam Link on Linux when games submit invalid textures”
  and “Improve streaming recovery when using Steam Link on Linux.” It also
  adds initial USB streaming, with Steam Client Beta required to use USB
  without Wi-Fi.
- [SteamVR 2.17 release](https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/1843481262693486)
  repeats Linux streaming fixes and initial USB support. USB is no longer
  solely a claim about an old beta, but version/channel requirements still
  need checking on the actual host.
- [SteamVR 2.18.1 beta](https://steamstore-a.akamaihd.net/news/externalpost/steam_community_announcements/1844751498219787)
  adds USB-tethered **Quest** support with Steam Link Beta. That entry is not
  proof of a Frame/Linux combination.
- The [Steam Link page](https://store.steampowered.com/app/353380/Steam_Link/)
  lists Linux desktop clients, while its Quest VR requirements still say
  Windows 10 or newer. Desktop Steam Link support is not equivalent to VR host
  support, and the Quest requirements are not a Frame support matrix.

**Inferred:** Valve has a Linux VR streaming path worth testing. The old blanket
claim “Linux cannot stream VR” is no longer justified by the evidence. These
release notes do not establish which Linux GPU/driver/Frame combinations work.
USB changes the transport; it does not by itself prove host encoder support.

**Not verified:** Linux host discovery, pairing, wireless or USB streaming,
stereo rendering, controllers, haptics, audio, latency, or a game. Frame-only
inspection cannot establish any of these. Flat Remote Play and a desktop shown
on a panel are not substitutes for this test.

Next test, once a Linux gaming PC is available: record distro, GPU/driver,
Steam client channel/version and SteamVR version; use the Frame's built-in
Steam connection flow, first wirelessly and then over a data-capable USB cable.
Launch a free OpenXR sample or developer-consented VR game. Verify stereo,
head/controller tracking, haptics and audio while worn; retain both ends' logs
and measure latency and recovery after a link interruption. Restore any test
channel changes. Do not change the shared headset's channel just for this report.

If that works, Frame Control can provide our own host checks, setup guidance
and session controls around Valve's existing platform. First establish which
controls have a usable interface; no stable automated pairing API has been
verified. **Estimate (inferred):** 2–5 engineer-days for the hardware feasibility
pass; another 1–2 weeks for a small integration if those interfaces exist.

## (b) Our own streaming — proposal only

This is a new VR transport and device integration, not a desktop capture feature.
A plausible first target is **one Linux GPU family, one host, one Frame**, using
SteamVR on both ends. Our host driver would expose a remote HMD/controllers,
receive poses and inputs, and obtain stereo textures for hardware encoding.
Our Frame OpenXR app would decode, submit the correct eye views and render poses
at predicted display times, and return tracking/input. SteamVR/OpenXR, bundled
codec/transport libraries and platform GPU APIs fit the ownership rule; a
WiVRn/ALVR/Monado server dependency would not.

**Inferred design risks:** Linux SteamVR texture-sharing/driver interfaces and
Frame decode-to-GPU interoperability need a spike before committing to this
architecture. Sending an already-composited desktop mirror loses the stereo,
pose and timing information we need. Late reprojection, clock conversion,
backpressure, controller bindings, audio sync and reconnects are substantial
work. A runtime shim must not assume `XrTime` equals monotonic nanoseconds.

### Reuse from `mac-in-headset`

**Documented from our code**, read-only at
[`1b90c64`](https://github.com/saphid/frame-control/commit/1b90c64b73bace54c63a3aae154c5d29a9448d72):

- Reuse the ideas for low-latency encoding without B-frames, dropping work
  before encoding, bounded queues, keyframe recovery, adaptive bitrate,
  per-frame timing and authenticated session setup.
- Its VideoToolbox encoder and ScreenCaptureKit capture are macOS-specific.
  Linux needs a new GPU encoder path (for example VA-API or NVENC via bundled
  libraries) and VR texture capture, not a port of window capture.
- Its WebSocket over SSH is useful for a first controlled transport experiment
  and control messages. Reliable TCP can stall behind lost packets; a VR media
  path needs measured deadline behaviour, likely datagrams with loss recovery
  using an ordinary bundled transport library. Do not invent cryptography.
- Its Chromium/WebCodecs panel viewer is not a VR client. That branch reports
  software H.264 decoding and occasional long Wi-Fi stalls on the Frame.
  Its desktop latency measurements are not motion-to-photon measurements or
  evidence that a 90/120 Hz stereo stream will work.

**Size/effort estimate (inferred, one experienced full-time engineer, hardware
available):**

| Phase | Deliverable / stop condition | Effort |
|---|---|---|
| Feasibility | Linux driver texture access, Frame hardware decode into OpenXR, pose/clock loop; stop if any cannot meet frame deadlines | 2–4 weeks |
| First end-to-end prototype | One GPU/codec, stereo sample over a controlled LAN, head/controllers, logs and teardown | 4–8 additional weeks |
| Usable limited beta | Audio/haptics, pairing, recovery, bitrate/loss handling, installer, worn testing and latency work | 6–12 additional weeks |
| Wider support | Multiple GPU vendors/distros, USB and Wi-Fi variation, long-session stability | 2–4 additional months |

Planning range: **12–24 engineer-weeks for a limited beta**, roughly
**10–25k lines of our code plus tests/tooling**, excluding bundled libraries.
This is a low-confidence scope estimate, not a delivery promise; an unsupported
driver or decode interface could block it entirely. Foveated streaming,
eye tracking and parity with Valve are excluded. A Linux gaming PC and repeatable
worn-headset testing are prerequisites. **Do not build this until Alex chooses.**

## (c) Optional “install WiVRn” shortcut only

Allowed as a clearly optional convenience, never a prerequisite for a Frame
Control feature. **Documented:** WiVRn's server Flatpak ID is
`io.github.wivrn.wivrn`; its client/server versions must match, and its Flatpak
includes xrizer/OpenComposite. Those are properties of an independently
installed third-party stack, not components of our implementation.

**Recommendation:** defer the shortcut while the current client fails before
session creation. If offered later, label that compatibility result and let
the user choose the install; do not present “install” as “streaming works.”
**Estimate (inferred):** 1–2 engineer-days for an optional host-side shortcut
with package/version detection and honest status, excluding third-party fixes.
No shortcut, host install, pairing automation or streaming UI was built here.

Choose **(a)** for the next hardware test. Keep **(b)** as a separately approved
project if Valve's path fails or lacks a required capability. **(c)** does not
solve the verified client blocker and should not be the product's foundation.
