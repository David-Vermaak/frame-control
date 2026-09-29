# compat-db: Frame Control's compatibility database

A private [Lakebed](https://docs.lakebed.dev/) capsule holding compatibility
reports for Android apps on the Steam Frame. Only the maintainer's copy of
Frame Control has the key to read or write it (see `shared()` in
`ui/frame_compat_db.py`). Everyone else's reports stay on their computer
unless they turn on **Share compatibility results**. Then the reports also go
to PostHog as `compat_report` events, and the maintainer syncs them in (below).

## Community reports

```sh
python3 ui/frame_compat_db.py sync --dry-run   # what would be added
python3 ui/frame_compat_db.py sync             # add them
```

`sync` reads `compat_report` events through PostHog's query API and adds
them with `via` set to `community`, `community-probe` or `community-install`.
It skips invalid reports and anything over 30 per reporter per day. Each run
re-reads the last 30 days, because an offline copy sends its reports late,
with the time they were made. `posthog-sync.json`, next to the outbox,
remembers which reports it has handled and each reporter's daily count, so
nothing is added twice and the cap holds across runs.

It needs:

- the PostHog project id: `"project"` in `ui/telemetry.json`
- a personal API key with `query:read`: `POSTHOG_PERSONAL_API_KEY`, or in the
  Keychain (service `frame-control-posthog`, account `personal-api-key`)

- Live: `https://frame-compat.lakebed.app` (deploy `dep_dDmcsosVSiFirpW6`,
  claimed, so it doesn't expire). The browser page only says it's private.
- Access: `GET /v1/reports?since=<createdAt>` and `POST /v1/reports` with
  `{"reports": [...]}`. Both need the `x-frame-control-key` header. There are
  no Lakebed queries or mutations, so nothing else can reach the rows.
- Key: `FRAME_CONTROL_KEY` in `.env.lakebed.server` (git-ignored, synced on
  deploy) and in the Mac's login Keychain (service `frame-control-compat-db`,
  account `app-key`), where `ui/frame_compat_db.py` reads it.
- Duplicates: each report carries a `clientId`, and a report already stored is
  skipped, so retries and restores are safe to repeat.
- Free-plan limits: 1 MiB of data and 16,384 rows per deploy, 1,000 writes a
  day. A report is about 300 bytes, so roughly 3,000 reports fit.

## Backups

`scripts/compat-db-backup.sh` exports every report through the app key and
keeps dated copies in
`~/Library/Application Support/Frame Control/compat-db/backups` (newest 60).
When the data has changed, it also uploads them with `gog` to the Google
Drive folder named by `DRIVE_FOLDER_ID` (set it in the LaunchAgent's
`EnvironmentVariables`). A LaunchAgent runs it daily at 03:40 and logs to
`~/Library/Logs/frame-compat-backup.log`. If an export has fewer reports than
the last good backup (`backups/.last-good`), it's kept as `refused-*.json`,
nothing is uploaded, and every later run refuses too until you rerun with
`--accept-shrink`.

Reports that can't be sent (unreadable outbox lines, or ones the server
rejects, which it lists by `clientId`) are never dropped: they move to
`~/Library/Application Support/Frame Control/compat-db/compat-outbox.jsonl.rejected`,
with the reason.

Restore (to this deploy or a new one):

```sh
python3 ui/frame_compat_db.py import BACKUP.json   # duplicates are skipped
python3 ui/frame_compat_db.py count
```

`npx lakebed db export dep_dDmcsosVSiFirpW6 --out full.json` is a second,
owner-only export path through the Lakebed CLI.

## Change and deploy

```sh
cd compat-db
npx lakebed dev --port 3917                 # local; data resets on restart
npx lakebed deploy                          # updates frame-compat.lakebed.app
```

To rotate the key: generate a new one, update the Keychain item and
`.env.lakebed.server`, then deploy.
