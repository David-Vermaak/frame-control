# Testing

Frame Control is tested in three layers, from fast and fake to slow and real.
A fourth, a SteamOS VM, may come later ([issue #6](https://github.com/saphid/steam-frame/issues/6)).

| Layer | Runs | Needs | Covers |
|---|---|---|---|
| Unit tests (`tests/*.py`) | `python3 -m unittest discover -s tests` | Nothing | Parsing, validation, request guards; SSH and HTTP are mocked |
| Fake Frame (`tests/e2e`) | `scripts/e2e.sh` | Linux with Docker | The real server and scripts against a container that behaves like a Frame |
| Headset smoke test | `scripts/frame-smoke.sh` | A Frame on the `frame` alias | Install, launch and remove on the real device, recorded with its BUILD_ID |

## Unit tests

```sh
python3 -m unittest discover -s tests
```

About 120 tests, a few seconds, on Python 3.9 and newer. GitHub Actions runs
them on macOS, Windows and Linux. They don't pick up `tests/e2e`.

## The fake Frame

`tests/fakeframe/` builds a container that stands in for the headset, and a
second one for the computer Frame Control runs on. `scripts/e2e.sh` builds
both, starts them with `docker compose`, runs `tests/e2e` in the host
container and takes everything down, exiting with the tests' status:

```sh
scripts/e2e.sh                                   # everything, about 2 minutes plus the first build
scripts/e2e.sh test_titles                       # one module
scripts/e2e.sh test_faults.Faults.test_disk_full # one test
FAKEFRAME_KEEP=1 scripts/e2e.sh                  # leave it running afterwards
```

It needs a Linux host with Docker and `docker compose`, and zsh. The images
are `fakeframe-frame` and `fakeframe-host`; the compose project, network and
volumes are `fakeframe-e2e*`. CI runs it on a native arm64 runner
(`ubuntu-24.04-arm`, the `e2e` job in `.github/workflows/checks.yml`).

The host container exists because OpenSSH reads `~/.ssh/config` from the
passwd home directory, not `$HOME`. There, `ssh frame` reaches the fake Frame
through the same `Host frame` block `ui/frame_connect.py` writes, the
repository is mounted read-only at `/repo`, and each test module starts the
real `ui/server.py` (Python 3.9) and talks to it over HTTP with the headers
its guards want.

### What's real and what's fake

| On the fake Frame | |
|---|---|
| Arch Linux (`archlinux:base`, or Valve's Holo Core aarch64 preview on arm64), user `steamos`, `/etc/os-release` with BUILD_ID 20260922.6101926 | Real OS, Frame's identity |
| `sshd` with key and password logins, `rsync`, `python3` | Real |
| Valve's steamos-devkit-service on port 32000 and its hooks, vendored unmodified in `tests/fakeframe/steamos-devkit-service` | Real; only its `dbus` import (for mDNS through systemd-resolved) is a stand-in that logs the registration |
| Valve's devkit-utils, copied over by Frame Control itself | Real |
| **fakesteam**: `~/.steam/steam.pid`, `steam.token` and the `steam.pipe` FIFO; answers `approve-ssh-key`, `create-shortcut`, `run-game`, `list-shortcuts` and `delete-shortcut` with the response files devkit-utils waits for; takes `steam://rungameid`, `install` and `store` URLs | Fake |
| DevTools on `127.0.0.1:8080` with a `SharedJSContext` target. The JavaScript Frame Control sends runs for real in Node against stand-in `SteamClient`, `appStore` and `downloadsStore` objects (`cef_shim.js`), so async functions, optional chaining and `Map`s behave as in Steam's CEF | The JS engine is real; the objects are fake |
| `steam`, `wpctl`, `flatpak`, `podman`, `nmcli`, `qdbus6`, `gamescopectl`, SteamOS's `steamos-enable-sshd` helper, and Lepton's launcher | Stubs that record their calls |
| Battery, charger and thermal zones under `/sys/class` | Files the supervisor writes. `/sys` is read-only in a container and Docker's AppArmor profile refuses writes under it, so each folder is a volume mounted twice: over `/sys/class/...` for `frame_status.py` to read, and under `/var/lib/fakeframe/sys` for the supervisor to write |
| `vrserver` and `plasmashell` | Renamed `sleep` processes, so the status page and the clipboard find them |

Every fake behaviour copied from the device has a comment citing the doc or
observation and the BUILD_ID it came from; anything not seen on a headset is
marked as a guess. The fake keeps its state in `/var/lib/fakeframe/state.json`
(shortcuts, devkit titles, compat tool mapping, launches, pairing requests,
Lepton instances, volume, Flatpaks, clipboard) and logs stub calls to
`calls.jsonl` beside it.

Native programs really run: a launched aarch64 title executes on an arm64
host, and an x86-64 one on x86-64 (the container shares the host's kernel).
Proton titles are recorded with the command Steam would run, not run.

### Fault switches

`fakeframe-ctl` works over SSH (`ssh frame fakeframe-ctl help`) and from the
host container (`FAKEFRAME_CTL=http://fakeframe:9999`), so a test can flip a
switch while SSH is down:

| Command | Effect |
|---|---|
| `pairing on\|off` | Steam's **Pair new host** screen open or not; off gives the device's 403 text |
| `answer approve\|deny\|timeout` | How the pairing prompt is answered |
| `steam on\|off` | Steam client running (pid file, pipe, DevTools) |
| `sleep on\|off` | Headset asleep: ports 22 and 32000 accept and never answer, so SSH times out |
| `sshd on\|off` | sshd stopped: new connections are refused, open ones stay |
| `devkit-service on\|off` | Port 32000 closed |
| `disk-full on\|off` | Fills the small (64 MB) filesystem on `~/devkit-game` |
| `runtime NAME installed\|missing` | Proton, the Steam Linux Runtimes, Lepton |
| `battery KEY=VALUE...` | e.g. `capacity=15 status=Discharging current_now=-900000` |
| `keys harness\|none`, `authorized-keys` | Set or read `~/.ssh/authorized_keys` |
| `reset`, `state`, `calls [TOOL]` | Start over; read the state and call log |

### What the fake can't show

- Rendering: the headset view, desktop capture content, live video, SteamVR,
  gamescope and panels. The capture stub returns a placeholder PNG.
- Proton and FEX: whether a Windows or x86-64 program actually runs.
- Android: there's no Android in the Lepton stand-in, so no ADB, display
  settings, probes or app crashes.
- The real Steam client's UI and anything it does that isn't modelled, and
  mDNS discovery.
- `sudo` and the power buttons, Tailscale, and the Windows and macOS sides of
  the app (the host container is Linux, so the `rsync` paths are tested and the
  `scp` fallback isn't).

## Headset smoke test

```sh
scripts/frame-smoke.sh           # needs `ssh frame` to work without a password
scripts/frame-smoke.sh --pair    # also pairs a throwaway key: approve it in the headset
```

It checks `properties.json` and the status, then installs, launches and
removes three tiny titles built from bytes by `tests/smoke/tiny_programs.py`
(an ARM64 and an x86-64 static Linux program that sleep for ten seconds, and
an x86-64 `.exe` that exits at once). A launch passes only with fresh evidence:
the ARM64 program running, the `.exe` started (its process or Steam's log),
and the x86-64 program running or Steam logging that its runtime isn't
installed, which is what the Frame does today. Steam's log lines about each
title are kept.

Everything it installs is removed again, also after a failure: the titles and
their Steam shortcuts, a paired key, and `~/devkit-utils` if it wasn't there
before (if it was, it stays, synced to this checkout as Frame Control always
does). A cleanup that fails counts as a failed step. Results go to
`tests/smoke/results/<time>-<BUILD_ID>.json` (not committed) with a summary on
screen; it exits 0 when every step passed, 1 if one failed, 2 if the headset
isn't reachable.

`--pair` asks the devkit service to pair a new RSA key, which needs someone
in the headset to open **Settings → Developer → Pair new host** and approve
it; the key is checked and then taken out of `authorized_keys` again.

## When the device disagrees with the fake

The fake is only as good as what's been seen on a headset. When the smoke
test (or anyone) finds the Frame doing something else:

1. Record what the device did, with the date and BUILD_ID, in the doc that
   covers it (`docs/sideloading.md`, `docs/ssh.md` and so on).
2. Change the fake to match, with a comment citing that observation. The
   behaviours are in `tests/fakeframe/rootfs/usr/local/lib/fakeframe/`
   (`fakesteam.py` for Steam, `cef_shim.js` for DevTools, `init.py` for the
   switches, the stubs in `rootfs/usr/local/bin`).
3. Run `scripts/e2e.sh`. If the app is wrong, the tests now fail the way the
   device did; fix the app and add a unit test.

For example, on 2026-09-27 the smoke test found that Steam's `create-shortcut`
refuses ids with a hyphen (`missing/invalid arguments`), which the fake had
accepted. The fake now refuses them the same way, and Frame Control makes ids
Steam accepts.

## PC panel host tests

`tests/test_pcview.py` checks source-bound tickets, reconnect/Stop revocation,
input release, source-coordinate mapping and use of the existing panel
launcher. The `PC host libraries` workflow builds the bundled native adapter
on Windows, Ubuntu x64 and Ubuntu ARM64, then runs a real x264 synthetic
stream through the HTTP/WebSocket agent. `FRAME_PC_REQUIRE_NATIVE=1` makes a
missing native bundle fail CI instead of skipping. These tests do not grant a
portal dialog, capture a desktop, exercise a hardware encoder or validate
laser alignment. See [PC host evidence](pc-in-headset.md#evidence).
