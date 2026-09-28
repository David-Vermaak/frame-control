# vr-library implementation notes

- Scope: `/tmp/vrapk/p1.md`; work only in steam-frame-vrlib, no delegation,
  no push/PR/issues. Parent handles independent review.
- Read source interface, APK/VR docs, catalogue docs and installer/catalogue,
  Frame skill and device architecture docs.
- Registry validation passed; no Steam Frame capability in rendered entries.
  Prior-work skill absent at both skill roots. Existing worktree explicitly
  authorized by task; no portfolio registration or out-of-worktree writes.
- Device preflight: SSH alias timed out; frame.local does not resolve. No
  sessions started or stopped; no settings changed. Device evidence pending.
- Current launcher execs setsid --wait, replacing the wrapper and providing
  no TERM/INT/HUP cleanup for its podman container. Need testable supervisor.
- Artwork: installer currently copies only APK icon; shortcut updates do not
  refresh existing name/icon. Plan stdlib PNG fallbacks and Steam grid files.

## Implementation and evidence

- Added `ui/frame_artwork.py`: bounded non-interlaced PNG decode/composition,
  five fallback slots, transparent logo, supplied PNG/JPEG bytes or HTTP(S)
  URLs. Standard library only; ASCII lettering, original label in native UI.
- Small additive `ui/frame_apk.py` fallback finds `assets/icon.png` in Open
  Saber Plus (its manifest/adaptive icon yielded no PNG before). SuperTux's
  normal APK icon works. Generated all slots from both supplied APKs and
  visually inspected the individual grids plus contact sheet.
- `install(..., artwork=None)` forwards artwork through patching, stages it,
  refreshes existing shortcuts instead of duplicating them, marks immersive
  shortcuts VR, and stores presentation warnings in meta.json. Source images
  retain their dimensions; generated slots use the requested Steam sizes.
- Frame helper uses native SetCustomArtworkForApp/ClearCustomArtworkForApp.
  Collections use collectionStore: create static Android/Android VR, preserve
  other members and dynamic/read-only names, remove obsolete membership.
- Remove clears artwork/managed memberships before app-folder deletion. A
  Steam cleanup failure leaves local metadata for a retry. Failed new install
  attempts to remove a newly created shortcut.
- Root-cause feedback loop: original launcher failed both cleanup tests
  (no `podman stop` on TERM or normal exit). It exec'd setsid without a signal
  handler. New supervising shell stays in Steam's tracked tree, handles
  TERM/INT/HUP, stops its container, terminates its child process group, and
  keeps the Lepton exit code. Lock + pre-existing container check prevent a
  second launch from stopping someone else's session. Data paths unchanged.
- Stop helper calls TerminateApp with the exact 64-bit game-ID string (not
  the 32-bit app ID, and not a lossy JavaScript number). Frame Control also
  directly stops the container if Steam is unavailable.
- API signatures/collection semantics read from public source, not guessed:
  [Steam client types](https://github.com/SteamDeckHomebrew/decky-frontend-lib/blob/main/src/globals/steam-client/App.ts)
  and [Steam UI](https://github.com/SteamDatabase/SteamTracking/blob/master/ClientExtracted/steamui/chunk~2dcc5aaf7.js).
  The fakeframe CEF/podman fixtures were extended accordingly; their new
  library methods are explicitly source-derived, not recorded device calls.

## Verification (2026-09-28)

- `python3 -m unittest discover -s tests -p test_frame_android_library.py`:
  initial two launcher cases FAILED on the old implementation; the focused
  suite passed after the changes (20 cases at that run, then one JPEG case
  added and covered by the final whole-suite run).
- `python3 -m unittest discover -s tests`: **187 tests, OK**, exit 0,
  9.948 seconds. Actual local runtime is **Python 3.9.6** (`python3 --version`),
  not just a syntax-compatibility check. Log: `/tmp/vrlib-evidence/tests.log`.
- `bash -n frame/android/lepton-app.sh`, `node --check
  tests/fakeframe/rootfs/usr/local/lib/fakeframe/cef_shim.js`, Python AST parse
  with `feature_version=(3,9)`, and `git diff --check`: exit 0.
- Generated self-contained Linux harness using the actual launcher and the
  fixture Lepton/podman, retaining the Frame's real setsid/flock:
  `ssh -o BatchMode=yes -o ConnectTimeout=8 frame 'python3 - 2>&1' <
  /tmp/vrlib-evidence/linux-launcher-check.py` — **3 tests, OK**, exit 0;
  normal exit + TERM + INT/HUP subcases, preserved saved-data sentinel.
  It uses a TemporaryDirectory; no installed game or global setting touched.
  Log: `/tmp/vrlib-evidence/linux-launcher-tests.log`.
- `ssh -o BatchMode=yes -o ConnectTimeout=8 frame python3 - <
  ui/frame_vrshot.py`: exit 0; returned `/tmp/frame-vrcap/shot-28947-vr.png`.
  Copied to `/tmp/vrlib-evidence/headset-preflight.png`, then removed only the
  temporary remote screenshot. The capture shows Steam's startup error.
- Source APKs were read from `/tmp/vrapk/opensaberplus.apk` and
  `/tmp/vrapk/supertux.apk`. Ten generated assets plus
  `/tmp/vrlib-evidence/artwork-preview.png` are local artwork previews,
  **not Steam UI screenshots**.

## Device blocker and open questions

- SSH initially timed out; later reachable on BUILD_ID 20260925.6191901,
  SteamVR 2.18.1. `podman ps` was empty and vrserver showed no current game
  scene. Steam CDP 8080 refused connections; no steamwebhelper was running.
  The updater repeatedly extracted/installed, and the headset displayed
  "There was an issue launching Steam". Bounded retries remained unavailable;
  console_log had no new game launch entries. Captures/logs live under
  `/tmp/vrlib-evidence/` (`device-preflight.log`, `device-final.log`).
- No real APK was reinstalled/launched/stopped. Therefore actual Steam Play
  and Stop/reaper tracking, app data across a real stop/relaunch, native
  library artwork display, collection persistence, VR scene attribution and
  automatic dashboard dismissal are **not verified**. No Steam library
  screenshot was possible. Next: restore Steam, reinstall the two APKs,
  test one brief Steam launch/Stop at a time, capture library/headset output,
  and check podman + console_log after each transition.
- VR shortcuts are marked through SetShortcutIsVR. Source inspection shows
  Steam Resume hides the dashboard only when scene-app ID matches the
  shortcut; whether Lepton gets that attribution remains open. No automatic
  dashboard hiding or permanent standby workaround added. Power settings
  were never changed, so no restoration was needed.
- Full fakeframe container E2E suite was not run; its updated JS fixture was
  exercised by the local Node/V8 test. Native Windows install path untested.
- No independent reviewer process launched: the explicit brief forbids
  delegation and assigns review/integration to the parent. No review claimed.
- No registry observation write outside this worktree: brief explicitly
  restricts work to this checkout (plus its requested evidence directory).
