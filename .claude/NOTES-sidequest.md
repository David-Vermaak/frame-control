# SideQuest implementation notes

2026-09-28. Worktree steam-frame-sidequest, branch sidequest. No delegation,
Frame mutations, installs, launches, pushes or issue edits.

- Read shared source interface, APK/VR docs, catalogue README and Python backend.
- SideQuest robots: crawl delay 3; disallow /search/, /user/*, /sideload/*.
- Current /terms Angular text (main-4MMXZRXL.js): Prohibited Activities (i)
  prohibits scraping; (xi) limits access to provided/authorised technologies;
  (xii) forbids bypass. Public API address is not permission for a third-party
  integration. Page-only source; no automated store downloads or metadata crawl.
- api.sidequestvr.com/robots.txt returned HTTP 403. First shared JS chunk also
  returned 403. No attempt to bypass either response.
- Public SideQuest desktop source cloned inside .claude/research for inspection.
  /install-from-key takes a website-issued token and returns apps[].urls[] with
  provider APK/OBB/Github Release/Mod and link_url. Do not reproduce token flow.
- Two SSH read-only attempts to frame timed out (exit 255). Exact host-side
  /sdcard mapping cannot be claimed. internal/<package> is private app data,
  not evidence of an OBB mapping. Use the running container's /sdcard path for
  OBB writes, without launching it; backup only documented internal/<package>.
- Scope: OBB helper/CLI, private-data backup/restore helper/CLI, compliant
  SideQuest page-only source. No search UI, artwork installer or install edits.
- Cross-provider review not launched: task explicitly forbids delegation;
  parent brief reserves integration and review for the parent.

## Implementation and verification

- Added ui/frame_android_data.py and frame/android/app-data.py; additive wrappers
  and CLI branches only in frame_android.py (install/_install unchanged).
- OBB transfers to an already-running named container, SHA-256 check before
  per-file rename. No claimed host sdcard mapping or device persistence.
- Backup/restore covers private internal/<package> only, requires a stopped
  instance, uses podman unshare, validates archive paths/types/package/instance,
  preserves numeric owners/modes, retains the prior data directory on restore.
- SideQuest adapter intentionally raises a page-only SourceError on search and
  download; details gives a numeric listing page with unknown facts, images
  schema and downloadable=False. Needs aggregate search to surface the error.
- docs/sidequest.md contains source links, feature comparison, command examples,
  terms/robots findings and outstanding device/API questions.
- Fixture tests/fixtures/sidequest-policy.json records observed policy excerpts;
  no API response is fabricated.
- `python3 -m unittest discover -s tests`: final run 181 tests, OK (exit 0).
  Includes real local shell execution of the OBB checksum/publish sequence,
  rejecting a changed input without replacing the previous file; archive
  round-trip/retained previous save, 0600 backup, malformed archive rejection.
  No test contacts the network or Frame.
- `ast.parse(..., feature_version=(3,9))`: four implementation files passed.
  Runtime python3 is Xcode Python 3.9.
- `python3 ui/frame_android.py install-obb` and `... backup-data`: both exit 1
  with the intended required-arguments error, without contacting Frame.
- `git diff --check`: passed before commit.
- No SideQuest game downloaded and no `info <download>` run: terms blocked that
  requested E2E. No Frame app was installed or launched; no live OBB, namespace
  ownership or restore acceptance test. Independent review reserved for parent,
  per this task's explicit no-delegation instruction.
