# SideQuest and Frame Control

Researched 2026-09-28. SideQuest is both a Quest discovery website and a desktop
sideloading/device-management app. Its Quest labels are **not** evidence that a
game works on Lepton: inspect the APK for arm64/OpenXR, Android API requirements,
VrApi and Meta services (see [VR APKs](vr-apks.md)).

## Features worth borrowing

Desktop evidence is the public [SideQuest source at af2ac70](https://github.com/SideQuestVR/SideQuest/tree/af2ac7043db122bca3c8db18f2b58f1660e9befb),
especially [ADB operations](https://github.com/SideQuestVR/SideQuest/blob/af2ac7043db122bca3c8db18f2b58f1660e9befb/desktop-app/src/app/adb-client.service.ts),
[drag and drop](https://github.com/SideQuestVR/SideQuest/blob/af2ac7043db122bca3c8db18f2b58f1660e9befb/desktop-app/src/app/drag-and-drop.service.ts),
and the [legacy repository index](https://github.com/SideQuestVR/SideQuest/blob/af2ac7043db122bca3c8db18f2b58f1660e9befb/desktop-app/src/app/packages/package.service.ts).
Website evidence: [SideQuest](https://sidequestvr.com/) and its public Angular
bundle `main-4MMXZRXL.js`, inspected locally without browser automation.
No SideQuest implementation code was copied.

| SideQuest feature | Frame Control before this change | Borrow? / effort |
|---|---|---|
| Store descriptions, screenshots, banners, trailers, ratings | F-Droid names, icons, compatibility verdicts and reports; no equivalent rich VR store | Yes, from authorised sources; medium. Search and library workers own presentation/artwork. |
| OBB expansion-file install | APK-only install | **Implemented helper and CLI**, medium. Essential for games whose assets are separate from the APK. |
| App-data backup/restore | Persistent instances and optional keep-data uninstall, no portable save archive | **Implemented private-data helper and CLI**, medium. Back up before updates or experiments. |
| File manager (list, upload, download, remove) | General Send to Frame, no Android file browser | Useful later, medium; requires clear instance selection and scoped paths. |
| Installed-app management (launch, uninstall, backup) | List, launch, stop, remove, probe | Already mostly covered. Backup added here. |
| Update notices / account library | Compatible-version lookup; no source-aware installed update notices | Useful later, medium; needs original version code and source identity recorded on install. |
| Custom repositories | Built-in F-Droid catalogue and compatible-version indexes | Separate user-repos worker. Legacy SideQuest source has a fixed SideQuestRepos index; arbitrary current custom-repo support was not verified. |
| Drag-and-drop APK/OBB install | APK drag-and-drop already works | OBB backend added here; future UI can call it. UI drop wiring is not included. |
| Tags, price, headset filters, reviews | Text search and Lepton verdicts, not Quest headset metadata | Useful, medium; search worker owns filters. Keep source headset claims distinct from tested Frame compatibility. |
| Screenshot/video capture and streaming | Frame screenshots/VR capture already present | Reuse existing tools; do not port Quest capture commands. |
| Device settings and ADB utilities | Frame/Android display settings, SSH and own-instance tools | Borrow selectively; Quest CPU/GPU presets and wireless-ADB setup do not map directly to Lepton. |

Priority: expansion files, then save backup/restore. Rich discovery and update
notices follow once a permitted metadata source and source/version persistence
are available. This patch deliberately exposes CLI/backend operations, leaving
shared UI, install(), Steam artwork and launch behavior to sibling work.

## SideQuest as a source: page-only

[Terms](https://sidequestvr.com/terms), “Prohibited Activities”, (i) prohibits
copying/distributing/disclosing the Service including automated or non-automated
“scraping”; (xi) prohibits content access through means other than those provided
or authorised by the Service; (xii) prohibits bypassing access restrictions.
The terms describe downloading developer-posted games through the Service, but
do not establish permission for this third-party API integration.

[robots.txt](https://sidequestvr.com/robots.txt) requests a three-second crawl
delay and disallows `/search/`, `/user/*` and `/sideload/*`. Robots permission
would not override the terms. The API host's robots request returned HTTP 403;
a request for the first shared website JS chunk also returned 403. No bypass,
account token, cookies, browser session or private endpoint was used.

The homepage publishes `https://api.sidequestvr.com` and
`https://cdn.sidequestvr.com`. The website bundle calls `searchApps(...)` and
`getApp(id, null)`; their actual HTTP search/detail routes could not be established
from the retrieved bundle. Do not invent endpoints. The open-source desktop
[install flow](https://github.com/SideQuestVR/SideQuest/blob/af2ac7043db122bca3c8db18f2b58f1660e9befb/electron/app.ts)
POSTs `{token: ...}` to `/install-from-key`. It consumes
`data.apps[].urls[]`, with `provider` values including `APK`, `OBB`,
`Github Release` and `Mod`, and `link_url`. This is a website-issued install-key
flow, not evidence of an anonymous download API. It is not implemented here.

`ui/apk_sources/sidequest.py` implements the shared interface conservatively:

- `sources()` marks SideQuest `page_only` and explains why.
- `search()` raises a user-readable `SourceError` with the browse URL (zero
  limit returns no rows). It does not invent app results or report a false
  “no matching games”. The aggregate search UI should surface this source error.
  In the app, Discover doesn't link to it; the **Sources** dialog lists SideQuest
  with a link to its website instead of an on/off switch.
- `details()` accepts a numeric listing id and returns its canonical page link,
  `downloadable: False`, empty versions/tags/headsets and the `images` shape
  `{icon: None, banner: None, screenshots: []}`. Name is explicitly a listing id;
  unknown facts, including free/VR status, stay `None`.
- `download()` refuses with that page link. Paid/external listings cannot be
  downloaded by this adapter either. No downloads means no verification claim.

The JSON fixture records policy evidence, **not a purported live app response**.
No listing metadata, artwork URLs, or OBB download URLs were scraped.
The requested real SideQuest → OpenXR APK → `frame_android.py info` test is
**blocked by the terms**, and was not performed. No alternate source is silently
substituted. A future integration needs SideQuest's permission or an expressly
supported third-party API, plus recorded search/detail/download fixtures,
free/direct-download classification, and size/hash verification. A calculated
local SHA-256 alone must not be called publisher verification.

## OBB files

```sh
python3 ui/frame_android.py install-obb org.example.game main.42.org.example.game.obb
python3 ui/frame_android.py install-obb org.example.game main.42.org.example.game.obb patch.42.org.example.game.obb
```

Install the APK first. The named instance must already be running; the helper
never launches an app or uses Lepton Development. It requires standard
`main|patch.<versionCode>.<package>.obb` filenames and nonempty files, validates
the entire batch before transfer, streams each file through SSH into that
instance, checks its SHA-256 **inside Android**, then renames it into
`/sdcard/Android/obb/<package>/`. `verified: True` here means transfer integrity
against the local input, not publisher authentication. Publication is atomic per
file, not for the whole batch; retry after a partial batch failure. Existing OBBs
with different version codes remain. The filename version must match the game;
the current install metadata does not expose its version code for comparison.
Restart the game yourself after the transfer if it cached missing expansion data.

Both read-only SSH attempts to the Frame timed out. Therefore the exact
host-side `/sdcard` mapping and persistence of expansion data were **not verified**.
`compatdata/<instance>/internal/<package>` is documented as `/data/data/<package>`;
it must not be mistaken for `/sdcard`. Using Android's path avoids guessing a
host layout, but device verification across restart/update is still required.
No OBB file was installed on the Frame during this work.

## Private app-data backups

```sh
python3 ui/frame_android.py stop org.example.game
python3 ui/frame_android.py backup-data org.example.game ./game-save.tar.gz
python3 ui/frame_android.py restore-data org.example.game ./game-save.tar.gz
```

Keep the instance stopped throughout either operation; do not launch it from
Steam concurrently. The remote guard fails if Podman cannot enumerate containers
or reports that instance running. The helpers use `podman unshare` to read/write
Android's mapped ownership without changing the live data's permissions.

The archive covers **only** `compatdata/<instance>/internal/<package>`, not the
APK, external `/sdcard/Android/data`, OBBs, keystore, or the full Android snapshot.
It contains a package/instance manifest and regular files/directories. Backups
are private (0600), validated before publication, and never overwrite an existing
backup. Keep them safe: app data can contain credentials and is not encrypted.

Restore checks the package and instance, rejects absolute/traversing/duplicate
paths, links and devices, caps files at 100,000 and content at 20 GiB, and validates
again on the Frame. It extracts into a separate directory, preserves numeric
ownership, ordinary modes and timestamps, then swaps the private-data directory.
Setuid/setgid bits are not restored. The previous directory remains beside it as
`.<package>.before-restore-<timestamp>`; the returned `previous` path identifies
it. This is an additional recovery copy, not an automatic deletion policy.

Locally verified: archive round trip including recovery copy, malformed archive
rejection, transfer command construction and failure handling. Not verified:
real Frame UID mappings/permissions, Android app-level recovery, live FUSE OBB
writes or persistence. Backups reject symlinks/special files; an app requiring
those needs a separately designed backup format. These CLI features still need
a real-device acceptance pass before being exposed as a polished UI workflow.
