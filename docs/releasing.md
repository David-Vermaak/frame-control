# Releasing and updates

Frame Control checks for updates itself. The desktop app offers a new version
only once it's GitHub's **latest release**, and drafts and pre-releases never
count. So a build reaches people only when you publish it, after testing it.

## Steps

1. Bump `version` in `app/package.json`, commit, and push a tag:

   ```sh
   git tag v0.4.0 && git push origin v0.4.0
   ```

   `.github/workflows/release.yml` builds macOS, Windows and Linux, and
   attaches everything to a **draft** release for that tag. Nobody is
   offered a draft.

2. Download the draft's installers and test them. An installed copy of the
   previous version won't offer the draft, so install it directly.

3. Write the release notes on the draft. The update banner links to them.

4. Publish:

   ```sh
   scripts/publish-release.sh v0.4.0
   ```

   The script checks that all eight installers are attached, each with the
   SHA-256 digest GitHub records. It attaches `update.json` (the version, the
   notes and each installer's digest), then publishes the release and marks it
   latest. From then on, running copies see the update. They check about 8
   seconds after starting, then every 6 hours, and anyone can use **Check for
   Updates…** (the app menu on macOS, the Help menu elsewhere).

To pull a bad release, mark the previous one as latest
(`gh release edit v0.3.9 --latest`) or turn the bad one back into a draft.
Copies that already updated stay on it. Nothing downgrades them.

## How a copy updates itself

`app/updater.js` reads `update.json` from
`github.com/saphid/frame-control/releases/latest/download/`. It falls back to the
REST API only when a release has no manifest, because the API allows just 60
unauthenticated requests an hour per IP address, shared by a whole household.
Then it downloads the installer for its platform and checks it
against the SHA-256 digest GitHub publishes for the asset. It refuses if the
digest is missing or doesn't match. Then:

| Installed from | Update |
|---|---|
| macOS `.dmg`, app in a writable folder such as Applications | The `.zip` is unpacked next to the app and its version checked. After the app quits, a small script swaps the new app in, putting the old one back if that fails, and reopens it. Updates don't get the download quarantine, so there's no `xattr` step. |
| Windows installer | The new `Setup` runs silently over the install (`/S --force-run`) and reopens the app. |
| Linux AppImage | The new AppImage replaces the old file and is started. |
| macOS app still on the disk image or translocated, Windows `.zip`, Linux `.deb` or `.rpm` | The banner opens the release page instead. |

Version 0.3.1 and earlier have no updater, so people on them have to download
the new version once by hand.
