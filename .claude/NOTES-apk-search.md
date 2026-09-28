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
