# Sideloading Linux and Windows games

A game you have as files (an itch.io download, your own build, a DRM-free
release) can go into the Frame's Steam library without a Steam store page.
Frame Control uses the same path as Valve's
[SteamOS Devkit Client](https://gitlab.steamos.cloud/devkit/steamos-devkit):
the title becomes a Steam **Devkit Game**, with a runtime (Proton or a Steam
Linux Runtime) chosen from the program itself.

For Android APKs, see [apks.md](apks.md) instead.

**Status: nothing here has run on a headset yet.** Every device-side step is
**inferred from Valve's steamos-devkit source** (release v0.20260925.1). The
local steps (reading the zip, picking the program and runtime, building the
request) are covered by `tests/test_frame_titles.py`.

## Using it

Drop a game's `.zip`, folder or `.exe` on **Send to Frame**. (Folders need the
desktop app, which knows where a dropped folder lives; in a plain browser, zip
it.) A dialog shows:

- **Name**: what Steam shows. Steam uses the title id as the name, so it's
  limited to letters, digits and `_`, and can't start with a digit; the
  dialog shows the result.
- **Launches**: the program picked to start the game, with the other
  candidates in the list.
- **Runtime**: picked from the program, see below. Windows programs can switch
  between Proton Experimental and Proton (stable).

Install copies it to the Frame and registers it with Steam; progress shows in
the bar and the activity log. **Sideloaded titles** lists what's installed,
with Launch and Remove. **Copy to ~/Downloads instead** keeps the old
behaviour for a zip that isn't a game.

From a terminal:

```sh
python3 ui/frame_titles.py inspect Game.zip          # what would be installed, no headset needed
python3 ui/frame_titles.py install Game.zip [--name N] [--exe REL] [--runtime R]
python3 ui/frame_titles.py list | launch ID | remove ID
```

## Choosing the runtime

The program's header decides, not its file name:

| Program | Runtime (Steam compat tool) | `steam_play` | Confidence |
|---|---|---|---|
| Windows `.exe`, x86-64 (PE machine `0x8664`) | `proton-experimental` | 1 | Inferred: ARM64 Proton runs x86-64 code through FEX |
| Windows `.exe`, 32-bit x86 (`0x14c`) or ARM64 (`0xaa64`) | `proton-experimental` | 1 | Inferred |
| Linux ELF, aarch64 (`e_machine` `0xB7`) | `SteamLinuxRuntime_4-arm64` | 0 | Verified: starts, but natively (see below) |
| Linux ELF, x86-64 (`0x3E`) | `SteamLinuxRuntime_4` | 0 | Verified not to start: the runtime isn't installed (see below) |
| Shell script | the runtime of the Linux binary beside it, else `SteamLinuxRuntime_4-arm64` | 0 | Guess |
| Anything else (32-bit Linux, other CPUs, DLLs, data) | refused with a message | | |

Proton Experimental is the default rather than stable because the Frame's
ARM64 Proton and FEX stack is new and Proton fixes reach Experimental first.
If a game misbehaves, reinstall it with Proton (stable).

The aliases and settings are the ones Valve's client sends: `RUNTIME_ALIASES`
in `devkit_client/__init__.py`, and `gui2._update_game`, which sets
`steam_play=1, steam_play_debug=0, steam_play_debug_version=2019` for Proton
and `steam_play=0` otherwise, plus `compat_tool=<alias>`. Valve's client only
offers `SteamLinuxRuntime_4-arm64` and Lepton when the device reports itself
as Deckard (the Frame).

## Picking the program

`ui/frame_titles.py` reads every file's header: ELF executables (PIE ones are
told from shared libraries by their `PT_INTERP` segment), PE executables (not
DLLs) and scripts with `#!`. A zip with a single top-level folder is treated
as that folder. Candidates are ranked by:

1. Not a helper: names like `UnityCrashHandler64`, `CrashReportClient`,
   `*setup*`, `unins*`, `vc_redist*`, `dxsetup`, `*prereq*`, and anything under
   `_CommonRedist`, `Redist`, `DirectX` or `Engine` go last.
2. Platform: native ARM64 Linux, then Windows x86-64, then x86-64 Linux, then
   other Windows builds.
3. Name: a program named like the zip or folder (build words such as
   `-linux-arm64` or `_v1.2` are dropped from the name).
4. Depth, then size: Unreal's top-level `Game.exe` beats
   `Game/Binaries/Win64/Game-Win64-Shipping.exe`.

A top-level shell script beats a Linux binary one folder down (`run.sh` +
`bin/game`); a binary next to a script wins. The list in the dialog lets you
pick another.

## What happens on the Frame (inferred)

1. **Tools.** `frame/devkit-utils/` (Valve's scripts, vendored unmodified, MIT)
   is copied to `~/devkit-utils`, where Valve's client puts it, unless the
   stamp file there already matches. Files are merged, not replaced, so a
   newer copy from Valve's client keeps its extra files.
2. **Folder.** `python3 ~/devkit-utils/steamos-prepare-upload --gameid ID`
   makes `~/devkit-game/ID` and prints `{user, directory}`.
3. **Copy.** The files go there with `rsync -a --delete` on macOS and Linux,
   or `scp -r` into a fresh folder that then replaces it on Windows. Then
   `chmod -R 755`, the modes Valve's client gives an upload.
4. **Register.** `python3 ~/devkit-utils/steam-client-create-shortcut --parms JSON`
   with `{gameid, directory, argv: [target], env: {}, settings, clear_settings,
   force_appid: "", lepton_args: ""}`. It writes `ID-argv.json`,
   `ID-env.json` and `ID-settings.json` next to the folder, then sends
   `create-shortcut` to the running Steam client over `~/.steam/steam.pipe`
   (authenticated by `~/.steam/steam.token`) and waits up to 5 s for Steam's
   answer file. Its `error`, for example "The Steam client is not running",
   is shown as the install error. The files stay, so installing again with
   Steam running finishes the job.
5. **Launch** is `steam-devkit-rpc run-game gameid=ID`. **Remove** is
   `steamos-delete --delete-title ID`, which deletes the folder and has Steam
   drop shortcuts with no folder. Frame Control then removes the `ID-*.json`
   files that Valve's script leaves behind.

Frame Control also writes `~/devkit-game/ID-framecontrol.json` (name, source
file, target, runtime, size). **Sideloaded titles** lists every folder in
`~/devkit-game`, including titles uploaded with Valve's client.

`argv` is one string, as in Valve's client (the start command may carry
arguments), so a program path with spaces is sent in double quotes. How Steam
splits that string is **not checked**.

## Safety

- Zips are unpacked on your computer first. Entries with absolute paths, `..`,
  drive letters or `:` anywhere in the path, or links that point outside the
  zip (or at a folder they're in) are refused. So are zips over 64 GB
  unpacked, over 200,000 entries, more than 200× compressed past 1 GB, or
  bigger than the free space.
- No symlink is created while unpacking, so no write can be redirected
  through one. A link to a file inside the zip (`libfoo.so.1 → libfoo.so.1.2`)
  becomes a copy of that file, which also works on Windows. Links to folders,
  loops and dangling links are left out.
- A dropped folder that contains symlinks (or Windows junctions) is copied on your computer first,
  with the same rule, because `scp -r` would follow a link out of the folder
  and upload whatever it points at.
- Installs run one at a time, and Remove is refused while one runs.
- The title id is limited to letters, digits and `_`, doesn't start with a
  digit (one that would gets `_` in front), and is 2 to 64 characters. That's
  what Steam's `create-shortcut` accepts: on the Frame it refused
  `fc-smoke-exe` with `missing/invalid arguments` and registered the same
  program as `FCSmokeProbe` (2026-09-27, BUILD_ID 20260922.6101926), and
  Valve's client only allows `^[A-Za-z_][A-Za-z0-9_.]+$`. Valve's scripts
  also pass the id to a shell (`steamos-delete` runs `rm -r` on it). Valve's
  reserved sideload names (`steam`, `steamvr`, and their `deckard` forms,
  which would replace the Steam client itself) get `_game` added.
- Nothing needs `sudo`; everything goes to your home folder on the Frame.
- In the app, a dropped folder is read from its local path by the app's own
  server, which only accepts requests from its own page (see
  [frame-control.md](frame-control.md#how-it-works)).

## Checked on a headset

Tested 2026-09-26 on a Frame (BUILD_ID 20260922.6101926) with small static test
programs and PuTTY's official 64-bit `putty.exe`, through both the command line
and the app (inspect, install job, ▶, Remove, and install links):

- [x] `create-shortcut` registers a title; it shows in the Steam library and in
  **Sideloaded titles**, and Steam maps it to the chosen compat tool.
- [x] `steam-devkit-rpc run-game` starts it (Steam logs `devkit run-game: started
  devkit game "<id>"`), and Remove (`steamos-delete`) deletes the files, the
  shortcut and the Proton prefix.
- [x] A quoted path in the start command is fine: Steam runs
  `proton waitforexitandrun "/home/steamos/devkit-game/<id>/<exe>"`.
- [x] An x86-64 Windows `.exe` runs under **Proton 11 (stable)** through FEX
  (ARM64EC) inside the Steam Linux Runtime 4.0 ARM64 container; PuTTY stayed up.
  Proton Experimental wasn't installed at the time (it was downloading), so it's
  untested. A Go-built x86-64 test program crashed in `libarm64ecfex.dll`
  (a FEX limitation with that program, not the sideloading).
- [ ] **An aarch64 build runs natively, not in `SteamLinuxRuntime_4-arm64`**:
  Steam records the mapping (`CompatToolMapping`, `compat_log.txt`) but launches
  the devkit title without the runtime's `_v2-entry-point` prefix. Fine for a
  self-contained build; a build that needs the runtime's libraries may not start.
- [ ] **An x86-64 Linux build doesn't start**: Steam logs `Tool 4183110 "Steam
  Linux Runtime 4.0" is found for appID …, but is not installed`, and the Frame
  doesn't install that x86-64 runtime for a devkit title (a `steam://install/4183110`
  request did nothing).
- [ ] Whether these titles open as flat panels or need anything VR-specific.
