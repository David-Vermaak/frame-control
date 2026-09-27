# Scripts and headset setup

The command-line side of this repo: how SSH gets set up with as little typing on
the headset as possible, what to use for each job, and the helper scripts that
Frame Control is built on. The scripts are zsh/bash and run on macOS; most also
run on Linux. On Windows, use the app.

## Minimum typing on the headset

Valve's own developer docs say SSH, ADB, and RDP are all turned on through a
**UI toggle**. You don't need a terminal, `passwd`, or `systemctl`. The only
thing you type on the headset is a password you choose.

On the Frame:

1. **Steam Settings → System → Enable Developer Mode** (a toggle, no typing).
2. Scroll down to the **Developer** section and click **Set User Password**.
   Type a password. **This is the only thing you type on the headset.** Pick
   something short, because you'll type it once more on the Mac and then
   never again.
3. (Optional, no typing) Note the IP address from **Quick Settings** or
   **Steam Settings → Internet**, in case `frame.local` doesn't resolve.
4. (Optional) Check **Steam Settings → System → Hostname**. Leaving it as
   `frame` means the scripts work without any extra setup.

On the Mac:

To use the scripts from a checkout instead of the app:

```sh
git clone https://github.com/saphid/steam-frame.git && cd steam-frame
./scripts/connect.sh              # or: ./scripts/connect.sh 192.168.1.50
ssh frame                          # passwordless from now on
```

`connect.sh` does four things:

- finds the headset (`frame.local`, then `frame`, or the IP/host you pass in)
- creates dedicated keys (`~/.ssh/id_ed25519_frame`, plus `~/.ssh/id_rsa_frame_devkit` for pairing)
- adds a `Host frame` block to `~/.ssh/config`
- tries SteamOS devkit pairing (approve on the headset, no password; **inferred**,
  see [SSH](ssh.md#password-free-pairing-steamos-devkit-service)), else runs
  `ssh-copy-id`, which asks for the Developer Mode password once

Run `./scripts/connect.sh --harden` later if you want to turn off SSH password
logins.

Sources: [Valve: Setting up your Steam Frame for development](https://partner.steamgames.com/doc/steamhardware/steamframe/setup),
[Valve: Steam Frame Debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging)
(both **confirmed on Steam Frame**, Valve official).

**Fallback, only if the Developer Mode toggle doesn't give you SSH.** From the
Mac, run `./scripts/serve-bootstrap.sh`. It prints a one-liner of about 30
characters, like `curl -fsS mac.local:8765|bash`, to type into Konsole on the
Frame's Linux desktop. The script it serves installs your Mac's public key and
enables `sshd`. See [docs/ssh.md](ssh.md#fallback-bootstrap-one-liner).

## Recommended options

| Goal | Recommended | Confidence |
|---|---|---|
| Shell on the Frame | `ssh frame` (user `steamos`) | Confirmed (Valve docs) |
| **See/control the Frame from the Mac** | **Steam Link for macOS → connect to `frame`** (Valve names this). Alternatives: RDP to `xrdp` with Microsoft *Windows App* for the Linux desktop, or `adb`/`scrcpy` for the Android (Lepton) layer only | Steam Link and xrdp confirmed on Frame; the Mac RDP client is inferred |
| **Show the Mac's desktop inside the Frame** | **macOS Screen Sharing (built-in VNC) → Remmina (Flatpak, aarch64) on the Frame's Linux desktop**, installed over SSH | Inferred: each piece is documented, but the combination hasn't been tested on a Frame |
| File transfer | `scp` / `rsync` over the `frame` alias (`scripts/push.sh`) | **Verified** (rsync is on the image) |
| Paste Mac clipboard into the headset | `scripts/paste-to-frame.sh` (`pbpaste` → `ssh` → Klipper over D-Bus), or the clipboard sync in an RDP session | **Verified** (script); RDP untested |

Details: [docs/ssh.md](ssh.md), [docs/streaming.md](streaming.md),
[docs/file-transfer.md](file-transfer.md),
[docs/open-questions.md](open-questions.md). For how the Frame's software
fits together, see [docs/how-the-frame-works.md](how-the-frame-works.md).

## Windows anywhere in the room

The in-headset Linux desktop is a single 1280×800 panel, and its windows can't
leave it. Each Steam app, though, gets its own SteamVR panel. That also works
for any Linux app tagged with an app id of its own:

```sh
./scripts/panel-on-frame.sh konsole
./scripts/panel-on-frame.sh mac-screen      # the Mac's screen, in its own panel
```

Then use the SteamVR dashboard's **Float in World**, **Move** and **Size**
controls to place each panel. See [docs/panels.md](panels.md).

## Scripts

| Script | Runs on | Purpose |
|---|---|---|
| `scripts/tailscale-on-frame.sh` | Mac → Frame | Install Tailscale in `~` as a userspace user service so `frame` works from anywhere; `--uninstall` (**verified** on the LAN) |
| `scripts/connect.sh` | Mac | Discover, set up key and `~/.ssh/config`, copy key, optional `--harden` (**verified**; `--harden` untested) |
| `scripts/install-apps.sh` | Mac → Frame | Install Flatpaks (Remmina, Moonlight, …) on the Frame over SSH as `--user` (**verified** with Remmina) |
| `scripts/paste-to-frame.sh` | Mac → Frame | Send the Mac clipboard (or stdin) to the Frame clipboard (**verified**) |
| `scripts/install-apk.sh` | Mac → Frame | Install APKs, each as its own persistent Lepton instance with a Steam library shortcut (`--dev`: old ADB path into Lepton Development) (**verified**; see [docs/apks.md](apks.md)) |
| `scripts/panel-on-frame.sh` | Mac → Frame | Start an app as its own floating VR panel, outside the desktop (**verified**, including `mac-screen` in the headset) |
| `scripts/mac-cursor-ring.lua` | Mac | Hammerspoon script: a ring around the Mac pointer so it shows in the VNC mirror (**verified**) |
| `scripts/run-on-frame.sh` | Mac → Frame | Start an app on the headset desktop, e.g. `mac-screen` opens Remmina straight into the Mac (**verified**) |
| `scripts/frame-ui.sh` | Mac | Start the Frame Control web UI (`ui/server.py`) and open it (**verified**) |
| `scripts/apk-catalog.sh` | Mac | Refresh the rated F-Droid catalogue that Frame Control's Android section shows (**verified**) |
| `scripts/compat-db-backup.sh` | Mac | Maintainer-only: back up the shared compatibility database locally and to Google Drive (**verified**) |
| `scripts/push-vr-video.sh` | Mac → Frame | Upload VR180/360 videos to `~/Videos/VR`, linked into DeoVR's Proton prefix; `--launch` starts DeoVR (**verified**: upload and link; in-headset playback of local files not yet checked). See [docs/vr-video.md](vr-video.md) |
| `scripts/push.sh` | Mac → Frame | `rsync` files to `~/Downloads` (or a given path) on the Frame (**verified**) |
| `scripts/serve-bootstrap.sh` | Mac | Fallback: serve `bootstrap-on-frame.sh` with your public key embedded |
| `scripts/bootstrap-on-frame.sh` | Frame | Fallback: install the key and enable `sshd` |

