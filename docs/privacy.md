# Privacy and analytics

Frame Control sends anonymous analytics to [PostHog](https://posthog.com)
(US cloud) so the maintainer can see how many people use it, which features
matter and where installs fail. You choose how much in **Privacy & updates**,
the last panel on the page. `ui/frame_telemetry.py` is the whole
implementation.

## The three levels

| Level | Default | What it sends |
|---|---|---|
| Anonymous usage statistics | On, after a notice on first run | The events in the table below |
| Share compatibility results | Off | Your Android compatibility reports and tests |
| Send error details | Off | Scrubbed error messages and tracebacks |

Nothing is sent until the first-run notice has been shown. The notice's
**Share more to help fix problems** button turns on the second and third
levels together. Either can be turned off later. Turning a level off
deletes that level's events that haven't been sent yet.

**Show what's been sent** in the panel lists the last 50 events that left your
computer, exactly as they were sent.

## Anonymous

- Events carry a random id, made when Frame Control first runs and kept in
  its data folder (`telemetry/settings.json`). It isn't derived from your
  computer, account or network. To get a new one, delete that file.
- Events are sent without person profiles (`$process_person_profile: false`)
  and without location lookup (`$geoip_disable: true`). Each carries a
  placeholder address (`$ip: 0.0.0.0`), so PostHog stores that instead of
  yours.
- Every event includes the app version, OS name (macOS, Windows or Linux),
  CPU architecture and Python version.

## Usage events

| Event | When | Properties besides the common ones |
|---|---|---|
| `app_installed` | First run | |
| `app_updated` | First run of a new version | `from_version` |
| `app_opened` | At most once a day | |
| `frame_connected` | The first time a SteamOS build is seen | `steamos_build`, `steamos_version` |
| `tab_viewed` | The first click on each tab in a session | `tab` |
| `install_finished` | Any install finishes, working or not | `kind` (apk, flatpak, steam, title, web), `ok`, `seconds`, `error_category`, `installer_code`, and see below |
| `update_offered`, `update_started`, `update_failed` | The update banner | `to_version`, `error_category` |

`install_finished` never includes a file name, path or error message. An
error becomes one category from a fixed list (for example `apk_wrong_abi` or
`frame_unreachable`), plus Android's own `INSTALL_FAILED_…` code when there
is one. It names what was installed only when that's already public:

- F-Droid catalogue apps: `package`. Never the version, since a local build can reuse a
  catalogue app's package name
- Flathub apps: `flatpak_id`
- Steam games: `steam_appid`
- A sideloaded title: only its runtime (Proton or Linux)

Any other APK is sent as `catalog: false`, with no name.

## Compatibility results (opt-in)

Each report becomes a `compat_report` event with the fields the Report dialog
shows: package, version, result or rating, your notes, how it was run, and the
SteamOS and Lepton builds. Before sending:

- the notes, app name and version are scrubbed like error messages (see
  below)
- the APK's source is kept only if it's `F-Droid` or the public host name of
  a download link (`https://example.com/…`). File names, user names,
  passwords, ports, paths, IP addresses and local host names are dropped

When you turn this on, reports you made earlier on this computer are shared
too.

The maintainer's `python3 ui/frame_compat_db.py sync` copies these events
into the compatibility database, marked `via=community…`. It takes at most
30 per reporter per day.

## Error details (opt-in)

`$exception` events carry an error message, the Frame Control file, line and
function it came from, and the request that failed (for example
`POST /api/android install`). Before anything is sent, the message is
scrubbed:

- your home folder becomes `~`, and any user name becomes `<user>`
- IP and MAC addresses, email addresses, `.local`, `.lan` and Tailscale host
  names, Steam ids, SSH and PEM keys, API tokens and long hex strings are
  replaced
- URLs are cut down to their scheme and a public host name, or `<url>`. User
  names, passwords, ports, paths and queries are dropped
- `token=`, `key=`, `password=` and similar values are replaced

The same error is sent at most once every 10 minutes.

## Report a problem

**Report a problem** is the warning-sign button in the header, also in the
Privacy panel and under **Help → Report a Problem…**. It sends the report
privately to Frame Control's PostHog project as a `problem_report` event, the
same way as the analytics above, so only the maintainer can read it and
nothing is published. It works whatever the analytics settings are, because
the person sends it deliberately. The report has the kind, title and text you
wrote, how to reach you if you gave it, a short reference shown after sending,
and the diagnostics below. It has its own random id, so it isn't linked to
your analytics events.

With **Include diagnostics** ticked (the default), the report adds:

- the app version and whether it's a built app
- the OS, its release and CPU, and the Python version
- the Frame's SteamOS build, if it has connected since the app started
- which analytics levels are on

**Also include recent activity and the server log** is off by default,
because those lines can name files and apps. When ticked, it adds the newest
Activity lines and server log lines, without the request lines.

Everything is scrubbed like error details and limited to what fits in the
report. Environment details are kept first, then the newest lines. **Show
exactly what's included** shows the snapshot that will be sent, and later
activity isn't added to it. If PostHog can't be reached, **Copy report** puts
the whole report on the clipboard.

The maintainer reads reports on the Frame Control dashboard in PostHog, or
with `python3 ui/frame_report.py inbox [days]`, which uses the same personal
API key as `frame_compat_db.py sync`.

## Turning it all off

Untick the boxes, or set `DO_NOT_TRACK=1` or `FRAME_CONTROL_TELEMETRY=0` in
the environment that starts Frame Control. A copy run from a source checkout
never sends anything unless `FRAME_CONTROL_TELEMETRY=1` is set.

## Update checks

The desktop app asks GitHub for the latest release shortly after starting,
then every 6 hours: the latest release's `update.json` on GitHub, or
`api.github.com/repos/saphid/frame-control/releases/latest` if that fails.
Those requests carry no id. To stop it, set
`FRAME_CONTROL_NO_UPDATE_CHECK=1`. See [releasing.md](releasing.md).
