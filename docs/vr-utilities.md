# VR comfort and performance

Frame Control owns its HUD and telemetry. They use SteamVR/OpenVR, gamescope,
Python and xterm already on the Frame, plus the Frame's sensors. No feature
requires fpsVR, OVR Advanced Settings, XSOverlay or another third-party app.
The software list is a separate, optional convenience.

This is **part of [#25](https://github.com/saphid/frame-control/issues/25)**,
not completion of the issue. Playspace controls remain blocked on the device
checks below. The PR stays draft.

## Our performance HUD

On **Home → VR comfort and performance**, the app shows a timestamped sample
with each status refresh (30 seconds, or Refresh). **Open HUD in headset**
starts our text HUD as a gamescope panel, refreshed every two seconds. In the
SteamVR dashboard, select **Frame Control HUD**, then Float in World or dock
it to a controller. **Close HUD**, or closing its terminal, ends it. Opening
it twice reuses the existing process.

| Value | Meaning and source |
|---|---|
| Compositor FPS / period | Differences between two `IVRCompositor_029::GetFrameTiming` frame indices and monotonic compositor timestamps, sampled 200 ms apart. Output cadence, not game FPS or a long-term average. |
| Application FPS | Reciprocal of OpenVR's client frame interval. Unavailable if there is no positive interval; not inferred from refresh rate. |
| Render GPU time | OpenVR total render GPU milliseconds, not GPU utilisation. |
| Compositor CPU | OpenVR compositor render CPU milliseconds, not game CPU time. |
| System CPU | `/proc/stat` busy-time delta across the sample, with guest time counted once and iowait treated as idle. |
| GPU clock | `3d00000.gpu/cur_freq`, converted from Hz to MHz; frequency is not load. |
| Hottest sensor / battery | Existing thermal-zone and battery sysfs reads from `frame_status.py`. |

OpenVR uses background application mode, which does not start SteamVR or keep
it running. This mode also returned live timing in a read-only device probe.

Missing sensors, a stopped or incompatible SteamVR runtime, and non-advancing
frame indices display **Unavailable**, never invented zero FPS. Failed status
refreshes clear the HUD card rather than keeping a stale live-looking sample.
The HUD itself adds CPU/GPU work; it is a diagnostic, not a zero-overhead benchmark.

**Verified 2026-09-28**, SteamOS 0.4.1, build `20260925.6191901`, SteamVR
2.18.1: the exact OpenVR interface and 192-byte timing layout returned advancing
frame indices and live GPU/CPU timing. Sensor reads, creation of overlay
`valve.steam.desktopgame.2000250025`, duplicate-open handling, and closing the
HUD passed. The temporary probe overlay disappeared after its process closed.
[Sanitized device sample](evidence/vr-utilities/device.json).

**Unverified:** visual placement while wearing the headset, controller docking,
and overhead during gameplay. The companion card was checked in the attached
preview using live Frame data. Creating an overlay does not establish that it
was visible to the wearer.

The optional HUD copies only `frame_status.py` and `frame_vr.py` into
`~/.local/share/frame-control/vr/`. It tags only the window whose X11 PID matches
its own xterm, avoiding other threads' windows. Stop checks both PID and Linux
process start time before sending SIGTERM. It does not stop Steam or SteamVR,
edit their settings, install a service, or need sudo.

## Playspace, seated height and recenter: paused

**Verified 2026-09-28:** SteamVR exposes `IVRChaperoneSetup_006` and
`IVRChaperone_004`. An initial 1 cm seated zero-pose translation committed,
read back and restored numerically. A later trial, while the shared Frame was
in use, changed universe IDs after commits and returned transforms that did
not match the requested write or restore. Journal entries reported
`CommitWorkingCopy`, `VREvent_ChaperoneUniverseHasChanged` and
`VREvent_ChaperoneRoomSetupCommitted`. The collision-bound arrays and play-area
size matched in the saved before/after records, but the origin matrices did not.

Alex confirmed the headset was in use and asked to pause control tests. No
further playspace writes were made. The exploratory control implementation was
removed from the shipping API; `recenter`, `adjust` and `restore` are rejected.
This is an **unresolved feasibility check**, not evidence that OpenVR controls
cannot work. Concurrent use and the Frame driver's coordinate-system handling
still need to be separated.

The probe's original and last-read poses remain in the Frame's
`~/.local/share/frame-control/vr/comfort.json` for investigation. This draft
neither reads nor applies that baseline. Do not blindly replay it into a room
that may have changed. No recenter test was reached in the later trial.

Before adding controls, on an idle Frame:

1. Establish current room and tracking state, and inspect the retained probe
   evidence before considering any restoration.
2. Prove seated and standing height/move operations in the Frame driver's
   current coordinates, including delayed readback, coordinate rebasing and
   recovery. Show that the physical safety boundary stays correct.
3. Verify recenter independently, and test a full apply/restore cycle plus a
   concurrent room-change refusal. Add a fake OpenVR test for those contracts.
4. Check the apparent result in a seated and a standing app before exposing UI.

**Documented:** seated and standing are tracking origins selected by an app;
changing a seated origin cannot force every game to support seated play.

**Inferred from the installed SteamVR defaults:** there is no generic snap-turn
or locomotion-vignette setting. `dashboard.verticalOffsetCm_2` and
`steamvr.panelMaskVignette` affect panels, not the player's height or game
locomotion. The app gives game-setting hints for snap-turn, teleport movement
and movement vignette instead of writing these unrelated settings.

## Optional software

**Verified 2026-09-28 (same build):** a read-only query of Steam's loaded
`appStore.allApps` found none of these five apps on the Frame account. The query
includes software, which the existing games-only library filter excludes.
Steam's public app-details API listed only Desktop+ as free. No software was
purchased, installed or launched during these ownership checks.

| Utility | Local Frame status | Public evidence / optional source |
|---|---|---|
| OVR Advanced Settings (1009850) | **Untested**, not owned | Steam edition is paid. Developer's [free source and releases](https://github.com/OpenVR-Advanced-Settings/OpenVR-AdvancedSettings). No verified Frame result in this work. |
| XSOverlay (1173510) | **Untested**, not owned | Supplied research attributes Proton support with tweaks to [Road to VR](https://www.roadtovr.com/valve-steam-frame-review/). This is a public report, not our verification. |
| OVR Toolkit (1068820) | **Untested**, not owned | Same [public Frame report](https://www.roadtovr.com/valve-steam-frame-review/); no local verification. |
| fpsVR (908520) | **Untested**, not owned | [Steam listing](https://store.steampowered.com/app/908520/). No Frame-specific result established in the supplied research. General PC VR reviews do not verify Frame support. |
| Desktop+ (1494460) | **Untested**, free | [Developer source](https://github.com/elvissteinjr/DesktopPlus). No verified Frame result in this work. |

**Documented (supplied research):** the XSOverlay/OVR Toolkit claims are kept as
attributed leads. **Verified source check 2026-09-28:** fetching the linked
review returned HTTP 200 and the expected review title, but neither utility name
appeared in the fetched HTML or extracted text. The claims could not be
corroborated from that page; this is not proof of incompatibility. They do not make those apps dependencies or mark them
locally verified. No paid utility is auto-acquired. A server-side check blocks
installation through the Steam endpoint if the paid utility is absent from the
loaded Frame library; an empty/unavailable library fails closed. Desktop+ uses
the existing free Steam install flow, which may need a license confirmation in
the headset. None is advertised as known-good without local evidence.

Compatibility reports reuse the existing `compat-db` storage and validation
with `package: "steam:<appid>"`, rather than colliding with Android package IDs.
The optional list shows the latest `works`, `issues` or `broken` report with its
date, build and notes, separately from public sources and ownership. With no
report the status stays **untested**. No shared database schema change or
production deployment is needed; no reports were published by this work.
