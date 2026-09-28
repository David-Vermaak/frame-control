# Frame OpenXR compatibility API layer

`XR_APILAYER_FRAME_compat` is an APK-local implicit API layer for Steam Frame's
OpenXR 1.0 Android runtime. It translates selected 1.1 functionality; it is not
a conformant replacement for a complete 1.1 runtime. Device facts are in
[../../docs/vr-apks.md](../../docs/vr-apks.md). No headset was contacted during
implementation. Loading, Valve-layer coexistence, controller usability, and
Wolvic startup/rendering on Lepton remain **unverified on device**.

## Behavior

- The negotiated `createApiLayerInstance` hook handles `xrCreateInstance`.
  API requests >= 1.1 become `XR_API_VERSION_1_0` (1.0.63 with these headers),
  rather than carrying an arbitrary application's patch into the 1.0 request.
  Requests below 1.1 are unchanged. The loader may reject future API versions
  before any layer runs.
- Enumerate downstream extensions through the **next layer's** GIPA before
  creation. Only advertised promoted extensions are added, with duplicates
  removed. All other application extensions remain, except the two stubs below.
  Log added, stubbed and missing extensions with tag `FrameXrCompat`.
- Missing `XR_KHR_android_thread_settings` is advertised and accepted;
  `xrSetAndroidApplicationThreadKHR` logs and returns success without changing
  thread scheduling. Missing `XR_OCULUS_android_session_state_enable` is
  advertised and accepted, with no commands or additional session behavior.
  If downstream supports either extension, preserve it and its real commands.
  Stubbing these capabilities is intentionally limited to accepting the app's
  request; it does not provide Meta services.
- The manifest's `instance_extensions` makes both stubs visible during the
  loader's pre-instance enumeration and validation. The layer also exposes
  an enumeration implementation with count/capacity handling. Before a next
  dispatch is available it can enumerate only its own extensions; normal app
  global enumeration is performed by the loader, merging runtime and manifest
  entries. See [loader_core.cpp L106–145](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/loader_core.cpp#L106)
  and [manifest parsing L549–558](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/manifest_file.cpp#L549).
- Unknown missing extensions are never silently removed. Khronos may reject
  them **before** entering the layer and name them in `OpenXR-Loader` logs
  ([loader_instance.cpp L149–176](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/loader_instance.cpp#L149)).
  If they reach this layer, it logs their names and preserves them for the
  downstream failure.
- `xrLocateSpaces` dispatches to `xrLocateSpacesKHR` per session/instance.
  This is the only command added in 1.1. The three relevant structure types
  (`SPACES_LOCATE_INFO`, `SPACE_LOCATIONS`, `SPACE_VELOCITIES`) and their KHR
  versions are **numeric aliases**, and the structs are typedef aliases in
  these headers. Explicit type conversion of local input/output copies is
  therefore an identity conversion; the velocity chain passes through without
  mutating its types or linkage. Output arrays remain shared and receive the
  runtime's results, including on error; caller struct headers remain intact.
- With palm pose enabled, rewrite both hand paths ending in
  `/input/grip_surface/pose` to `/input/palm_ext/pose` in `xrStringToPath` and
  in binding suggestions (including paths obtained before this layer rewrote
  them). Unknown paths are left alone.
- `xrGetInstanceProperties` and unrelated commands pass through. The real
  runtime's identity is not spoofed. Dispatch tables and session ownership are
  locked, and removed after successful destruction. Downstream calls occur
  outside the lock.

## Spec basis and interaction profiles

The pinned [1.1 promotions appendix, versions.adoc L16–156](https://github.com/KhronosGroup/OpenXR-Docs/blob/release-1.1.63/specification/sources/chapters/versions.adoc#L16)
includes a generated promotion list. Its source is the
[1.1.63 XML registry](https://github.com/KhronosGroup/OpenXR-Docs/blob/release-1.1.63/specification/registry/xr.xml),
selecting `extension[@promotedto='XR_VERSION_1_1']`. The full list implemented:

| Promoted extension | Handling |
| --- | --- |
| `XR_KHR_locate_spaces` | Enable if advertised; core command alias |
| `XR_EXT_local_floor` | Enable if advertised; reference-space enum is an alias |
| `XR_EXT_uuid` | Enable if advertised; type alias, no commands |
| `XR_EXT_palm_pose` | Enable if advertised; rewrite grip-surface paths |
| `XR_VARJO_quad_views` | Enable if advertised; view-configuration enum alias, support remains optional |
| `XR_EXT_samsung_odyssey_controller` | Enable if advertised |
| `XR_EXT_hp_mixed_reality_controller` | Enable if advertised |
| `XR_HTC_vive_cosmos_controller_interaction` | Enable if advertised |
| `XR_HTC_vive_focus3_controller_interaction` | Enable if advertised |
| `XR_ML_ml2_controller_interaction` | Enable if advertised |
| `XR_FB_touch_controller_pro` | Enable if advertised; old profile still usable |
| `XR_META_touch_controller_plus` | Enable if advertised; old profile still usable |
| `XR_BD_controller_interaction` | Enable if advertised |
| `XR_KHR_maintenance1` | Enable if advertised (included in the pinned registry's promotion list) |

`XR_EXT_hand_interaction` is **not** promoted to 1.1. Preserve it if requested;
it is not auto-enabled merely because the runtime advertises it. The parent
provided its availability; the device notes list extensions non-exhaustively.

The registry's `XR_VERSION_1_1` feature introduces 13 profile paths. For
Samsung Odyssey, HP mixed reality, HTC Cosmos/Focus3, ML2 and the three
ByteDance Pico profiles, drop a whole suggestion call with success when the
corresponding extension is absent. Keep it when the extension is enabled.
Drop the five new Meta paths: `touch_pro_controller`, `touch_plus_controller`,
`touch_controller_rift_cv1`, `touch_controller_quest_1_rift_s`, and
`touch_controller_quest_2`. The verified runtime notes do not list these;
Pro/Plus promotion also renames several components, so the old extension's
presence alone does not prove support for the new profile. This layer does
not implement those component conversions. Existing old Pro/Plus profile
paths are passed through if the app uses them.

Preserve the documented `oculus/touch_controller`, `khr/simple_controller`,
`valve/frame_controller` and original PC profile paths. The notes' "usual PC
controllers" is not a complete enumerated list: availability of promoted PC
profiles is inferred from the runtime's extension advertisement, not invented
from that phrase. OpenXR has no general profile enumeration API. Dropping
suggestions prevents an unsupported 1.1 profile from aborting initialization;
it does not create bindings, so the app must also suggest a supported fallback.

## Headers and licensing

The required public headers (`openxr.h`, `openxr_platform.h`,
`openxr_platform_defines.h`) are unmodified from
[OpenXR-SDK release-1.1.63](https://github.com/KhronosGroup/OpenXR-SDK/tree/release-1.1.63/include/openxr),
commit [f2448a8](https://github.com/KhronosGroup/OpenXR-SDK/commit/f2448a8797c85814aa892efc1ab8707900fbcc78).
The requested filename `loader_interfaces.h` no longer exists in 1.1 SDK
releases: negotiation was ratified and moved to `openxr_loader_negotiation.h`
in 1.0.33 ([spec history L166–186](https://github.com/KhronosGroup/OpenXR-Docs/blob/release-1.1.63/specification/sources/chapters/versions.adoc#L166)).
To retain that requested interface filename, the layer uses the unmodified,
ABI-compatible [last legacy header from SDK-Source release-1.0.32](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.0.32/src/common/loader_interfaces.h).
Only these four headers are vendored. They offer Apache-2.0 OR MIT; this
vendoring uses Apache-2.0, reproduced in [vendor/LICENSE](vendor/LICENSE).

## Android discovery and coexistence

**Minimum asset-discovery loader release: 1.0.25 (2022-09-02).** Its
[release commit, 15c3d8e](https://github.com/KhronosGroup/OpenXR-SDK-Source/commit/15c3d8eb99994e5365d6b6f96eefaf4c51a65a9d)
explicitly announces APK-packaged API layers;
[manifest_file.cpp L665–723](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.0.25/src/loader/manifest_file.cpp#L665)
implements discovery, and [L934–940](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.0.25/src/loader/manifest_file.cpp#L934)
adds asset manifests after filesystem manifests. This is not a 1.1-only feature.

**For an app requesting API 1.1, use a 1.1 loader (first release 1.1.36) or
newer.** A 1.0 loader rejects the request before the layer can downgrade it;
see [loader_core.cpp L217–230](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/loader_core.cpp#L217).
The runtime can remain 1.0.

Source inspection at release-1.1.63 confirms:

- [manifest_file.cpp L720–783](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/manifest_file.cpp#L720)
  uses the initialized Android asset manager and scans
  `openxr/1/api_layers/implicit.d/` for JSON. The application must initialize
  the loader with its Android context (`xrInitializeLoaderKHR`). In the APK,
  the entry is `assets/openxr/1/api_layers/implicit.d/XrApiLayer_FRAME_compat.json`.
- A bare `library_path`, as used here, is **not explicitly concatenated** with
  `nativeLibraryDir`. [L878–900](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/manifest_file.cpp#L878)
  leaves a bare name for normal dynamic-linker search in the application's
  namespace (including its native library directory). Relative paths with a
  slash use [LocateLibraryInAssets L943–952](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/manifest_file.cpp#L943),
  which resolves against `GetAndroidNativeLibraryDir()`.
  [loader_init_data.cpp L87–96](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/loader_init_data.cpp#L87)
  obtains the asset manager and `ApplicationInfo.nativeLibraryDir` via JNI.
  Wolvic's supplied manifest has `android:extractNativeLibs="true"`.
- [android_utilities.cpp L267–322](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/android_utilities.cpp#L267)
  handles runtime broker discovery, not APK layer discovery. Its
  `native_lib_dir + so_filename` and `dlopen` refer to the runtime package.
  They must not be confused with the app's layer library resolution.
- [manifest_file.cpp L1008–1023](https://github.com/KhronosGroup/OpenXR-SDK-Source/blob/release-1.1.63/src/loader/manifest_file.cpp#L1008)
  retains filesystem manifests and then appends asset manifests. This leaves
  `/vendor`'s `XR_APILAYER_VALVE_fdm_injection` discoverable. This layer advances
  `nextInfo` exactly once, uses next-layer GIPA, preserves the rest of the
  create-info chain and never opens `vrclient.so` directly. No environment
  layer-list override or assumed ordering is needed.

`DISABLE_FRAME_XR_COMPAT` is the manifest's disable environment variable.
Leave it unset to activate the implicit layer. No `enable_environment` is
required.

## Rebuild and verify

NDK **r30 / 30.0.16248370**, installed at
`~/Library/Android/ndk/30.0.16248370`, was the latest stable/LTS package shown
by the [Android download page](https://developer.android.com/ndk/downloads)
on 2026-09-28. Downloaded
[android-ndk-r30-darwin.dmg](https://dl.google.com/android/repository/android-ndk-r30-darwin.dmg)
(1,072,970,961 bytes). The page publishes SHA-1, not SHA-256:

```
expected: 48591224b6657f46eebbc9d95d4af09bbed8d107
actual:   48591224b6657f46eebbc9d95d4af09bbed8d107  (PASS)
```

Mounted read-only with `hdiutil`; copied
`AndroidNDK16248370.app/Contents/NDK` into that version directory. Despite the
`darwin-x86_64` toolchain directory name, clang is universal arm64/x86_64 and
ran on this Apple Silicon Mac.

```sh
frame/openxr-compat/build.sh
# Or set ANDROID_NDK_HOME to an installed NDK.
cmake -S frame/openxr-compat -B frame/openxr-compat/build-host -G Ninja
cmake --build frame/openxr-compat/build-host
ctest --test-dir frame/openxr-compat/build-host --output-on-failure
```

The script produces arm64-v8a / android-24, C++17, `-O2`, static libc++, hidden
internal symbols, stripped output, and `-Wl,-z,max-page-size=16384`.
The committed prebuilt is `prebuilt/arm64-v8a/libXrApiLayer_FRAME_compat.so`.
Its SHA-256 is recorded below with the APK checks.

[Host test output](evidence/ctest.txt): two tests passed, covering pure logic
and the actual layer with a fake next layer and host-only log/JNI shims.
[Full llvm-readelf -d -s -l output](evidence/readelf.txt) records three LOAD
segments aligned `0x4000`, only `xrNegotiateLoaderApiLayerInterface` exported,
and only `libc.so`, `libm.so`, `libdl.so`, `liblog.so` as NEEDED libraries.
All 46 undefined imports are versioned `@LIBC` except `__android_log_print`.
There is no `libc++_shared.so` dependency and no unstripped `.symtab`.

No independent model review was run: the task explicitly prohibits delegation.
No `CODING_STANDARDS.md` exists in this worktree or the primary worktree.

## Wolvic injection artifact (Mac only)

Copied `/tmp/vrapk/wq-dec` to `/tmp/vrapk/wq-layer-dec`; retained its existing
LAUNCHER fix and native-library extraction setting. Added only:

```
assets/openxr/1/api_layers/implicit.d/XrApiLayer_FRAME_compat.json
lib/arm64-v8a/libXrApiLayer_FRAME_compat.so
```

Built using `/tmp/vrapk/jdk/Contents/Home/bin/java` and
`/tmp/vrapk/apktool_3.0.3.jar`, then signed in place using
`/tmp/vrapk/uber-apk-signer-1.3.0.jar --overwrite` (embedded debug certificate):

```sh
/tmp/vrapk/jdk/Contents/Home/bin/java -jar /tmp/vrapk/apktool_3.0.3.jar \
  b /tmp/vrapk/wq-layer-dec -o /tmp/vrapk/wolvic-quest-compat.apk
/tmp/vrapk/jdk/Contents/Home/bin/java -jar /tmp/vrapk/uber-apk-signer-1.3.0.jar \
  -a /tmp/vrapk/wolvic-quest-compat.apk --overwrite
/tmp/vrapk/jdk/Contents/Home/bin/java -jar /tmp/vrapk/uber-apk-signer-1.3.0.jar \
  -a /tmp/vrapk/wolvic-quest-compat.apk --onlyVerify
```

Final APK: `/tmp/vrapk/wolvic-quest-compat.apk` (not committed).
The bundled `lib/arm64-v8a/libopenxr_loader.so` is byte-for-byte unchanged.
Its strings include both `openxr/1/api_layers/implicit.d/` and
`openxr/1/api_layers/explicit.d/`, `AddManifestFilesAndroid` error messages,
and `xrLocateSpaces`. Thus it contains APK asset discovery and evidence of
1.1 command support. Its exact upstream release number was not established
from the binary; runtime execution of those paths remains unverified.

Prebuilt library SHA-256:

```
aace99b7bcdb3c79fa844d75a9483f8f4b16bd6e669fb3003515df8c673ce54b
```

APK SHA-256:

```
355a681625e38b71563dcffecb031799eff27894d6ab910a659bace057e82c1f
```

Final `--onlyVerify` exited 0: zip alignment verified, v2/v3 signatures
verified, one APK processed and zero errors. ZIP readback confirmed that the
injected library and manifest match the committed inputs, the loader is
unchanged, and the binary Android manifest retains the LAUNCHER category.
See [APK verification evidence](evidence/apk-verification.txt).
