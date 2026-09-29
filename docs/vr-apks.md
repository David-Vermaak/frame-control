# VR APKs and Quest games in Lepton

What it takes to run an immersive (OpenXR) Android app, including Meta Quest
builds, on the Frame. Checked on SteamOS BUILD_ID 20260925.6191901, Lepton
v2.8.14 (rootfs v2.8.11), SteamVR 2.18.1, on 2026-09-28, unless marked
**inferred**.

## How a VR APK reaches SteamVR (verified)

- Lepton ships a standard Khronos system runtime manifest,
  `/vendor/etc/openxr/1/active_runtime.json`, pointing at SteamVR's Android
  client, `/data/steamvr/runtime/bin/androidarm64/vrclient.so`. That is the
  host's `/opt/steamvr/bin/androidarm64/`, bind-mounted in.
- An APK's own Khronos-style `libopenxr_loader.so` tries the runtime brokers
  (`org.khronos.openxr.runtime_broker`, `…system_runtime_broker`), finds
  neither, then falls back to that manifest. Nothing in the APK has to change
  for discovery.
- Install the APK without the flatscreen marker
  (`python3 ui/frame_android.py install app.apk --vr`). The marker only
  controls Lepton's 2D Android surface; the app itself has to start an
  OpenXR session.
- Lepton also loads Valve's `XR_APILAYER_VALVE_fdm_injection` layer from
  `/vendor/etc/openxr/1/api_layers/implicit.d/`. Only layers in Valve's own
  directories are picked up (`liblepton/vulkan_layers.sh`), so a third-party
  layer has to ship inside the APK.

**Open Brush 2.32.29, the Quest APK from its GitHub release (Unity OpenXR,
Vulkan), works unmodified.** Its manifest already has `LAUNCHER` next to
`com.oculus.intent.category.VR`. Unity asked for OpenXR 1.1, got
`XR_ERROR_API_VERSION_UNSUPPORTED`, retried with 1.0 and succeeded. SteamVR
took it as the scene app, created Touch, simple-controller and Frame-controller
bindings, and the session reached `XR_SESSION_STATE_FOCUSED`. A headset capture
(`ui/frame_vrshot.py`, after waking the compositor and closing the dashboard)
showed a dark sky over a mountain horizon; nobody wore the headset to confirm
it was Open Brush's scene or to try drawing.

**Khronos `hello_xr` (Vulkan, 1.1.63 release APK) works unmodified:**
`Instance RuntimeName=SteamVR/OpenXR RuntimeVersion=2.18.1`, 1728×1728
swapchains per eye, session `IDLE → READY → SYNCHRONIZED` (the headset was
not being worn, so it did not reach `FOCUSED`).

## What SteamVR's Android runtime supports (verified, from `vrclient.so`)

- **OpenXR 1.0 only.** An app requesting `XR_API_VERSION_1_0` works; one
  requesting 1.1 (`XR_CURRENT_API_VERSION` in a 1.1 SDK) gets
  `XR_ERROR_API_VERSION_UNSUPPORTED` from the runtime.
- Extensions include `XR_KHR_opengl_es_enable`, `XR_KHR_vulkan_enable{,2}`,
  `XR_KHR_composition_layer_depth`, `XR_KHR_locate_spaces`,
  `XR_EXT_local_floor`, `XR_EXT_uuid`, `XR_EXT_palm_pose`,
  `XR_EXT_hand_tracking`, `XR_EXT_eye_gaze_interaction`, and these Meta ones:
  `XR_FB_display_refresh_rate`, `XR_FB_foveation{,_configuration,_vulkan}`,
  `XR_FB_space_warp`, `XR_FB_swapchain_update_state`,
  `XR_META_foveation_eye_tracked`, `XR_META_recommended_layer_resolution`,
  `XR_META_vulkan_swapchain_create_info`, `XR_META_performance_metrics`.
- Not present: `XR_FB_passthrough`, `XR_FB_hand_tracking_*`,
  `XR_FB_spatial_entity*`, `XR_FB_color_space`,
  `XR_KHR_android_thread_settings`, `XR_OCULUS_*`.
- Interaction profiles include `oculus/touch_controller`, `khr/simple_controller`,
  `valve/frame_controller` and the usual PC controllers. Valve documents Touch
  bindings as a working fallback on the Frame controllers.

## What stops a Quest APK (verified with Wolvic 1.9, `oculusvr` build)

1. **Lepton won't start it.** Lepton's `apk-info-extractor` only accepts an
   activity whose intent filter has `android.intent.action.MAIN` and
   `android.intent.category.LAUNCHER`. Quest apps use
   `com.oculus.intent.category.VR` instead, so Lepton logs `APP_ACTIVITY is
   empty` and exits. There is no override. **Fix:** add the `LAUNCHER`
   category to that intent filter and re-sign. After that, Wolvic started.
2. **OpenXR 1.1.** Wolvic's Quest build then requested OpenXR 1.1 and aborted
   on `XR_ERROR_API_VERSION_UNSUPPORTED`. Unity's OpenXR plugin retries with
   1.0 (Open Brush, above), so this mostly bites native and non-Unity apps. **Fix (inferred):** an API layer
   inside the APK that asks the runtime for 1.0 and maps the 1.1 core
   functions to the extensions the runtime does have (`XR_KHR_locate_spaces`,
   `XR_EXT_local_floor`, `XR_EXT_uuid`, `XR_EXT_palm_pose`).
3. **Lepton's missing clipboard service** still applies to VR apps. The Godot
   XR Tools demo's Quest build (itch.io) dies in `Godot.<init>` casting the
   null clipboard service to `ClipboardManager`, before any OpenXR call. See
   the clipboard table in [apks.md](apks.md).
4. **Not yet reached:** required Meta-only extensions (each app differs),
   swapchain formats (the Lynx Wolvic build needed `GL_SRGB8_ALPHA8`), and
   Meta platform services.

The loader was never the problem: Wolvic's Quest `libopenxr_loader.so` is a
Khronos-style loader and found SteamVR through `/vendor`.

## Out of scope

- **Meta entitlement.** Apps that call the Oculus Platform SDK
  (`libovrplatformloader.so`) to check the Quest store licence need Meta's
  services. Frame Control won't work around that.
- **VrApi-era apps** (`libvrapi.so`, before OpenXR) need an API translator,
  not a patch.

## Frame Control does this for you

APK uploads and `python3 ui/frame_android.py install app.apk` detect VR
manifest categories, Samsung's `vr_only` flag and the arm64 OpenXR loader.
VR apps default to immersive mode without the flatscreen marker. The upload
selector or CLI `--flat` / `--vr` overrides that choice. Compatibility notes
identify legacy VrApi, Meta platform SDK and OpenXR libraries.

Lepton only starts an `<activity>` whose MAIN intent filter has LAUNCHER; it
ignores `<activity-alias>`, which is where Godot 4 exports put LAUNCHER. When no
real activity qualifies, Frame Control adds LAUNCHER to the VR activity's MAIN
filter, or to the activity the launcher alias targets, then repacks and v2-signs
the APK locally before copying it; `meta.json` records
`"patched": ["launcher"]`. Unchanged ZIP members retain their compressed
bytes; stored libraries are aligned to 16 KiB. The RSA signing identity lives
in Frame Control's per-user app-data directory as `apk-signing-key.json`
(mode 0600). Keep this key to preserve the signer on subsequent patched
updates. A re-signed APK cannot update an installation signed by its original
publisher; Android also treats it as a different signer for signature checks.

VR apps with an arm64 OpenXR loader also get the OpenXR compatibility layer
([frame/openxr-compat](../frame/openxr-compat/README.md)): an implicit API
layer in the APK's `assets/openxr/1/api_layers/implicit.d/`, which the app's
own loader picks up next to Valve's layer. It asks SteamVR for OpenXR 1.0 when
the app wants 1.1 and enables the extensions that became 1.1 core; maps
`xrLocateSpaces` to `xrLocateSpacesKHR` and `grip_surface` to `palm_ext`; drops
1.1 controller profiles SteamVR doesn't know; stubs
`XR_KHR_android_thread_settings` and `XR_OCULUS_android_session_state_enable`;
and keeps the current refresh rate when SteamVR refuses a requested one.
`meta.json` records `"patched": ["openxr-compat"]`. Skip it with
`install … --no-xr-compat`. Its decisions go to logcat under `FrameXrCompat`.

Verified on the headset (2026-09-28):

- **Wolvic 1.9, Quest build**, installed as downloaded: Frame Control added
  `LAUNCHER` and the layer. The layer turned OpenXR 1.1.48 into 1.0.63, the
  instance and session were created, and a 144 Hz refresh request that SteamVR
  refused was kept at the current rate. The session reached `SYNCHRONIZED`;
  then Wolvic's Gecko engine crashed (null SIGSEGV on its Gecko thread, the
  same crash its Lynx build has), which is Wolvic's, not OpenXR's.
- **Open Brush, Quest build**, with the layer: 1.1.54 → 1.0.63, the thread
  settings stub in use, `bytedance/pico4_controller` bindings dropped, and the
  session reached `FOCUSED`, the same as without the layer.

Inspect or prepare an APK without contacting the headset:

```sh
python3 ui/frame_android.py info app.apk
python3 ui/frame_android.py patch app.apk patched.apk
python3 ui/frame_android.py patch app.apk patched.apk --add assets/openxr/1/api_layers/implicit.d/X.json=X.json --add lib/arm64-v8a/libX.so=libX.so
```

The patch fixes Lepton's launch-category requirement. It does not supply an
OpenXR 1.1 translation layer, Meta services or a VrApi implementation.
