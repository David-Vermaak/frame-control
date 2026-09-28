# APK search

- Scope: dynamic source aggregator, additive server APIs, Android search view; no device calls.
- Source metadata stays unknown when missing. Unknown-package names only group with other unknown packages (avoids ambiguous package attribution).
- Queries use daemon workers with at most one in-flight request per source; deadlines don't wait for stuck workers.
- Independent review delegated to parent per task brief; no workers launched.

## Implementation

- `ui/apk_sources/search.py`: discovers KIND modules, parallel daemon queries (12 s deadline), bounded per-source workers, grouped offers, fit/rank/filter logic, persisted source enablement, repo adapter and install adapter.
- `ui/server.py`: GET `/api/sources`, GET `/api/search?q=&vr=true|false&installable=true|false&source=`, POST `/api/sources/install` and POST `/api/sources` with actions `add`, `remove`, `enable`.
- `ui/index.html`: unified Find apps in Android tab, source/VR/Flat/Installable chips, offer picker, metadata, jobs, Sources panel. Old catalogue DOM remains hidden for existing report/icon code; only the unified search is visible.
- `_demo.py` is discovered only with `FRAME_APK_SEARCH_DEMO=1`; it cannot download. `tests/search_preview.py` serves the real UI/search endpoints and rejects all device endpoints and writes.

## Evidence

- `python3 -m unittest discover -s tests`: 179 tests passed on Python 3.9.6, exit 0. Log `/tmp/apk-search-suite.log`.
- `node --check /tmp/apk-search-evidence/ui.js`: exit 0; script extracted from index.html.
- `git diff --check`: exit 0.
- Python 3.9 grammar parse: search.py, _demo.py, server.py, test_apk_search.py and search_preview.py passed; actual suite interpreter also Python 3.9.6.
- Started `FRAME_APK_SEARCH_DEMO=1 python3 tests/search_preview.py` locally; used T3 preview at http://127.0.0.1:8795/#android (1280x800).
- Browser assertions: two grouped apps, source picker updates install choice, VR excludes flat, Flat excludes VR, source filter keeps one offer, search query narrows results, pending install disables its button after re-render. Passed.
- Inspected screenshots: `/tmp/apk-search-evidence/search.png`, `/tmp/apk-search-evidence/sources.png`. Other panels show intentional device-access errors in the preview.
- Tests cover dynamic module discovery, grouping (including ambiguous names), ranking, fit, timeout/failure isolation, persistent enablement, endpoint validation, repo callbacks, background install with mocked frame_android.install, conditional artwork, and OBB support/refusal.

## Parent integration / unverified

- No source modules from sibling branches are present yet. Real network listings/downloads, signatures, merged repo persistence and physical installation were not exercised. No SSH/device installation performed.
- Assumes future `frame_android.install_obb(package, paths)`; reconcile with vr-library worker's actual signature. Artwork is passed only when install explicitly declares that parameter. OBB-required apps fail before APK installation when helper is absent.
- `app/package.json` currently copies ui with only `*.py` / `*.html`; parent must include `apk_sources/**/*.py` in packaged resources when integrating source workers. Kept package config outside this worker's scope.
- No cross-provider reviewer launched: task explicitly forbids delegation and assigns integration/review to the parent.
- Source metadata unknowns do not imply compatibility or verification. Unknown-package entries only group with other unknown-package entries with the same normalized name.

## Store redesign (follow-up)

- Read `/tmp/vrapk/p5-redesign.md` and common ground rules in full. Consulted Human Interface Craft, especially purpose/information, hierarchy, progressive disclosure and familiar navigation.
- Discover now owns the Android tab's full width; device controls/reports remain under On your Frame. Artwork cards, a featured app, browse rows, debounced search, friendly filters/count/empty state and skeletons replace the technical listing.
- Details use a native dialog with banner/icon, creator, description, screenshots with keyboard/arrow controls, badges, primary installation action and friendly source choices. Technical identifiers and compatibility facts are collapsed.
- Sources use a separate native settings dialog with aligned switches, source initials, descriptions, trust labels and repository form. A failed source produces a quiet human-readable notice.
- `_images.py` registers only source-entry artwork, returns opaque handles, permits HTTP(S) raster images only, rejects private/link-local/reserved IPs (including redirected targets), connects to the validated IP with TLS hostname checking, limits images to 8 MiB and bounds its memory cache to 64 MiB / registry to 4096 handles. Browser requests are same-origin `/source-image/<opaque handle>`; arbitrary URLs are never accepted by the HTTP endpoint.
- New GET `/api/sources/details?source=&id=` supplies richer details. Search decorates offers with `artwork` and a plain-language `verdict`.
- Optional source metadata is documented in search.py: `images: {icon, banner, screenshots}`, developer, description, popularity, open_source, requires_meta_services, frame_tested. Only explicit `frame_tested=True` earns Works on the Frame. Installability alone says Ready to try. No fabricated popularity labels when a source provides no ranking data.
- Background jobs accept an optional progress reporter; source installs report Downloading and Installing. The UI displays a percentage only if supplied by a job. Current shared download() interface provides no byte progress, so real sources show truthful indeterminate stages, not fabricated percentages.
- Demo listings include real recorded public artwork plus clearly documented illustrative listing metadata. Preview installer alone simulates percentages and completion; it never calls frame_android.install or SSH. Repo toggles in preview use an isolated temporary settings file.

### Visual review and evidence

- Iteration 1: inspected browse and detail screenshots; found too much vertical space above the app rows and description. Reduced hero/banner heights and header spacing.
- Iteration 2: inspected wide and narrow layouts; moved desktop search beside the heading, placed a full popular row before the short VR row, compacted the disconnected-headset notice for browsing, aligned settings switches, preserved radio focus after detail refresh and improved progress text contrast.
- Final visual inspection: 1440x1050 desktop and 390x844 narrow viewport, two narrow columns (172px each), document width 380px inside a 390px viewport (no horizontal overflow).
- Final PNGs in `/tmp/apk-search-evidence/v2/`: browse-home.png, search-results.png, app-details.png, install-progress.png, sources-sheet.png, empty-state.png, narrow-window.png. Iteration screenshots retained there too.
- Browser checks via T3 preview at http://127.0.0.1:8795/#android: six grouped apps; no package/API/ABI/engine/demo jargon in browse; typing `world` yields three apps after debounce; app details contain three screenshots and two source choices; source selection updates installation target and keeps keyboard focus; disabling itch.io removes its notice and persists for subsequent searches; simulated install displays Downloading 43%, then completion and Open in Steam; empty search renders illustrated recovery; narrow two-column layout has no horizontal overflow.
- `FRAME_APK_SEARCH_DEMO=1 python3 tests/search_preview.py` started the local server used above. Real device endpoints rejected.
- `python3 -m unittest discover -s tests`: 188 tests passed on Python 3.9.6, exit 0. Final log `/tmp/apk-search-evidence/v2/unittest.log`.
- `node --check /tmp/apk-search-evidence/v2/ui.js`: exit 0 (inline script extracted from final index.html).
- Python 3.9 grammar parse: seven changed/new Python files passed. `git diff --check`: exit 0.

### Still unverified / parent integration

- Real source modules, real repository add/remove writes, real downloads and physical headset launch/install are not tested. No SSH or device installation performed.
- Image fetch transport tested with mocks; preview serves recorded images through the actual HTTP image endpoint/cache. Live asset files were fetched separately to create fixtures; live proxy TLS/redirect behavior across provider CDNs remains unverified.
- Percentages in install-progress.png are explicitly simulated by the preview harness. Real provider byte-progress integration needs an extension to the shared download interface; stage reporting works now.
- Source workers must provide `images` and creator/description metadata for rich listings; fallback gradients/initials work without them. Frame-tested/popularity signals must be backed by source evidence, not inferred from installability.
- The earlier packaging (`apk_sources/**/*.py`) and install_obb signature integration notes still apply. No Electron build or cross-provider review run; parent owns integration/review and this brief prohibits delegation.
