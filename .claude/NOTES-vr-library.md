# vr-library implementation notes

Scope: `/tmp/vrapk/p1.md` plus parent updates mandating all-entrypoint artwork,
SteamGridDB, designed fallback art, settings/backfill and two visual iterations.
Worktree `/Users/saphid/projects/steam-frame-vrlib`, branch `vr-library`.
No delegation, push, PR or issue edits. Parent owns independent review and integration.

## Implementation

- Initial commit `c06b328`: shared artwork, collection handling, launcher
  supervision, APK icon fallback and tests. Follow-up replaces its bitmap
  artwork implementation with Steam Chromium canvas + Motiva Sans.
- `ui/frame_steamgriddb.py`: optional API provider, exact title (or trailing VR)
  match, highest-scored returned static/non-NSFW artwork, separate portrait
  and wide requests. No key means no requests/warnings. Saved settings are
  atomic 0600 on POSIX; API never returns the key; auth redirects disabled.
- Precedence: provider, source slots/banner/feature graphic/screenshots, APK
  icon/generated art. All five outputs have fixed Steam sizes. Host Python
  stays stdlib-only and Python 3.9 compatible; Frame renders consistently
  regardless of host OS. Package filters include both new JS resources.
- `frame/android/library_artwork.js`: dominant-colour gradient, blurred icon
  backdrop, large icon/shadow, real font, no title in hero, transparent logo.
  Removes only opaque near-black matte connected to icon corners. Native
  icons remain original; opaque foreground app tiles have rounded corners.
- `apply_library` is mandatory for APK and native-title installs. CLI, upload,
  catalogue, versions, web install and source install seam share it. Native
  devkit executable/runtime is preserved. Failed initial art application
  removes a newly created shortcut; no success with incomplete art.
- Refresh CLI/API/settings button repairs installed Android entries without
  reinstalling/stopping, uses cached source art, queries SGDB again if enabled,
  repairs missing shortcuts, and reports batch errors independently.
- Details: name/icon/VR flag, sort-as, Android/Android VR collections or native
  Sideloaded, Installation details note preserving other notes. No supported
  arbitrary description/store-page/developer/achievement metadata found.
  Notes are keyed by sanitized name (Steam limitation: equal-name collisions).
- Device discovery: custom-art type 4 is broken on this Steam client: it logs
  Unknown asset type and overwrites wide art with the icon. Use types 0–3 and
  SetShortcutIcon separately. Confirmed actual cache after correction.
- Launcher retains its supervising shell under Steam, traps TERM/INT/HUP,
  stops only its own container, kills its child group, preserves exit status.
  Lock/pre-existing container guards prevent duplicate session cleanup.
- VR attribution discovery: Lepton uses SteamAppId for both stable context and
  Android SteamVR identity. New shortcut.id + LEPTON_ENV_SteamAppId separates
  them using Lepton's existing passthrough; container/data paths unchanged.
  On-device source mounting.sh applies LEPTON_ENV_* after its regular setenv.

## Visual critique and iterations

Evidence `/tmp/vrlib-evidence/design-v1`, `design-v2`, `design-v3` (15 PNGs each).
Open Saber Plus/SuperTux icons came from authorized APKs. AntennaPod is a
preview only using the official F-Droid icon; it was not installed/launched.

1. v1: real typography/gradient already improves over old bitmap output, but
   Open Saber has a black square matte, and SuperTux palette looks muddy.
2. v2: removed connected black matte; rounded the opaque AntennaPod foreground.
   Inspected all three posters. SuperTux still muted; title line balance weak.
3. v3: lifted sampled saturation and balanced two-line labels. Inspected all
   three posters plus final all-slot sheet. Large recognisable artwork, readable
   typography, richer colours, textless hero and transparent title logo. Icon
   source resolution still limits detail; source/SGDB art remains preferable.

Final sheet `/tmp/vrlib-evidence/artwork-preview-final.png` is an artwork
preview, NOT a Steam UI screenshot. Rows: portrait, wide, hero, logo/icon.

## Device verification (2026-09-28, build 20260925.6191901, SteamVR 2.18.1)

Steam was initially unavailable (old notes/evidence), then recovered. Both
APKs were reinstalled and refreshed successfully with zero library warnings:

- Open Saber Plus: org.godotengine.open_saber_plus, instance 2802929330,
  shortcut 3346865537, game ID 14374678025558032384.
- SuperTux: org.supertux.supertux2, instance 2811892472,
  shortcut 2883168793, game ID 12383115674816348160.

`steam-cache-final.log`: both actual Steam cache sets are 600x900, 920x430,
3840x1240, 1280x480, with app icon 256x256. `steam-details-targets.json`:
both correct names/sort-as, Android + Android VR, existing Played preserved.
Native managed note readback result 1 (success), saved as steam-notes-final.json.

Open Saber first session: Steam tracked it for 32 seconds; process and
container remained alive until only SteamClient TerminateApp was called.
After identity fix, a second session remained alive at 22 seconds (Android
PID 992), SteamVR identified steam.app.3346865537, and screenshot shows the
actual game scene instead of blank Resume tile. Steam Stop removed tracked
process and container within ~5 seconds. No direct podman-stop fallback was
used for this verification. Same data paths; existing game play count shown
in capture, but no separate save-file sentinel added to the real game.
Evidence: opensaber-identity-session.log, opensaber-identity-headset.png,
opensaber-running.log, opensaber-steam-stop.log, opensaber-headset-running.png.

SuperTux: Steam Play tracked the wrapper for ~17 seconds. SDLClipboardHandler
crashes with NullPointerException on missing ClipboardManager during activity
creation. Lepton and supervisor then cleaned up; it never reached a VR scene.
Steam Stop was issued after the crash, so sustained SuperTux Stop is NOT proven.
Evidence: supertux-running.log, supertux-lepton.log, supertux-shot.json.

Other test activity appeared on the device during checks (other Android
container and Gravitas). Neither was stopped or modified. Final Open Saber
preflight had no Android containers; other desktop overlays later appeared
in its headset capture. No global VR standby/dashboard settings changed.
Direct Steam UI Page.captureScreenshot timed out; no library UI screenshot.

## Automated verification and limits

- Whole suite: `python3 -m unittest discover -s tests`, Python 3.9.6. Final
  result: **206 tests OK, 10.294 seconds, exit 0**. Log:
  `/tmp/vrlib-evidence/tests-final.log`.
- Offline entrypoint tests exercise real shared install/render/configure flow
  with SSH/API mocked: CLI, upload, catalogue, versions, web download, source
  seam, native title and refresh. Provider ranking/failure/settings tests.
- Node contract test runs actual renderer against explicitly synthetic canvas,
  validates all PNG dimensions and hero/logo text placement. This canvas is
  also used by fakeframe; transparent fixture images are not visual evidence.
- Launcher tests assert stable context plus shortcut passthrough, signals,
  normal exit, duplicate guard and saved-data sentinel. Earlier real Linux
  setsid/flock fixture run: 3 tests OK (linux-launcher-tests.log).
- `bash -n`, Node syntax checks, Python AST feature_version=(3,9), diff checks.
- Docker E2E not run: `docker info --format '{{.ServerVersion}}'` exits 1;
  daemon socket /var/run/docker.sock does not exist.
- Authenticated SGDB lookup/download unverified (no key configured).
- Windows/Linux packaged binaries not built/launched; filters tested.
- Native devkit art uses shared tested seam; no additional native title was
  installed on device. Sibling source-search endpoint not in this worktree:
  only its public installer contract is tested.
- Independent review not spawned: explicit brief forbids delegation and assigns
  parent review/integration. No other provider/model participation claimed.
