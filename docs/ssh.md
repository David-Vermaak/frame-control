# SSH into the Steam Frame

Confidence labels:

- **Confirmed (Frame)**: Valve's Steam Frame docs or a Frame-specific source.
- **Inferred (Deck/SteamOS)**: true on Steam Deck or SteamOS generally, but
  not checked on a Frame.
- **Guess**: reasoned, with no source.

## How access is turned on

| Claim | Confidence | Source |
|---|---|---|
| **Steam Settings → System → Enable Developer Mode** enables SSH, ADB, and RDP | Confirmed (Frame) | [setup](https://partner.steamgames.com/doc/steamhardware/steamframe/setup), [debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging) |
| A password is set in **Developer → Set User Password**. There is no default password. | Confirmed (Frame) | [setup](https://partner.steamgames.com/doc/steamhardware/steamframe/setup) |
| The default user is **`steamos`**, not `deck` | Confirmed (Frame) | [debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging): `ssh steamos@frame` |
| The default hostname is **`frame`**, and can be changed in **Steam Settings → System → Hostname** | Confirmed (Frame) | [setup](https://partner.steamgames.com/doc/steamhardware/steamframe/setup), [adb_lepton](https://partner.steamgames.com/doc/steamhardware/steamframe/adb_lepton) |
| The IP address is shown in Quick Settings or **Steam Settings → Internet** | Confirmed (Frame) | [adb_lepton](https://partner.steamgames.com/doc/steamhardware/steamframe/adb_lepton) |
| The rootfs is read-only. `sudo steamos-readonly disable` makes it writable. | Confirmed (Frame) | [debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging) |
| `sudo pacman` works. Helper aliases `cdd` (Frame scripts dir), `cdl` (Steam logs), and `lepton` exist. | Confirmed (Frame) | [debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging) |
| There's a full KDE Plasma Linux desktop inside the headset, reachable from the SteamVR dashboard | Confirmed (Frame, press) | [Road to VR review](https://roadtovr.com/valve-steam-frame-review/), [UploadVR](https://www.uploadvr.com/flatpaks-open-source-steam-frame/) |
| On Deck, the manual route is Desktop Mode → Konsole → `passwd` → `sudo systemctl enable --now sshd` | Inferred (Deck) | [pimylifeup](https://pimylifeup.com/steam-deck-ssh/), [gist](https://gist.github.com/chphr/9c0791de6d2c659af3bf5890d9080973) |

On Deck, SSH needs the manual terminal steps. On the Frame, the Developer Mode
UI handles both the password and the SSH service. That's why the headset-side
checklist in the README involves no terminal at all.

## Name resolution from a Mac

Valve's examples use a bare `frame`. That works on Windows through
LLMNR/NetBIOS. **On macOS, a bare single-label name usually doesn't resolve**
unless your router's DNS registers DHCP client names.

- **Verified on device (2026-09-25):** `avahi-daemon` is running on the Frame
  and `frame.local` resolves from the Mac over mDNS.
- `scripts/connect.sh` tries `frame.local`, then `frame`, then an mDNS browse for
  the devkit service (below). If none works, it tells you to re-run it with the IP.
  Once you have a working address, the `Host frame` alias means you just type
  `ssh frame`.
- To check discovery yourself: `dns-sd -G v4 frame.local` (Ctrl-C to stop), or
  `dscacheutil -q host -a name frame.local`.
- A DHCP reservation for the headset on your router makes the IP stable. That's
  the most reliable fallback.

## Key-based login (done by `scripts/connect.sh`)

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_frame -N '' -C "mac->steam-frame"
ssh-copy-id -i ~/.ssh/id_ed25519_frame.pub steamos@frame.local
```

`~/.ssh/config` block (managed between marker lines by the script):

```
Host frame
  HostName frame.local
  User steamos
  IdentityFile ~/.ssh/id_ed25519_frame
  IdentityFile ~/.ssh/id_rsa_frame_devkit
  IdentitiesOnly yes
  ServerAliveInterval 30
```

The script only asks for the password if the pairing below doesn't work.

## Password-free pairing (SteamOS devkit service)

From Valve's source ([steamos-devkit-service](https://gitlab.steamos.cloud/devkit/steamos-devkit-service),
[steamos-devkit](https://gitlab.steamos.cloud/devkit/steamos-devkit) client). **Verified on a
Frame 2026-09-26** (BUILD_ID 20260922.6101926): the service runs with Developer Mode
on, `properties.json` answers with `"login": "steamos"`, the headset advertises
`_steamos-devkit._tcp` as `frame`, and `/register` needs pairing mode (below). The
approve prompt and key install are not verified yet. SteamOS's devkit service is
what Valve's Devkit Client uses to pair. `scripts/connect.sh` and
`ui/frame_connect.py` try it first:

- The headset serves HTTP on port **32000** and advertises mDNS
  `_steamos-devkit._tcp`. `GET /properties.json` gives the `login` user; the
  script uses it as `User` (unless you set `FRAME_USER`, or it says `root`),
  for the password fallback too, and keeps it on re-runs.
- **Open Steam Settings → Developer → Pair new host in the headset first.**
  Otherwise `/register` answers at once with `403` `"please put the Steam client
  in pairing mode: Settings -> Developer -> Pair new host"` (verified). The
  scripts say so and keep asking for 2 minutes while you open it.
- `POST /register` with `ssh-rsa <key> <comment> 900b919520e4cf601998a71eec318fec`
  (a fixed token from Valve's client) shows an approve prompt inside the
  headset naming the comment (`frame-control@<your computer>`). It waits 30 s,
  then installs the key for the device user and turns `sshd` on. The reply is
  `200 Registered`, or `403` with `{"error": ...}` (declined, timed out, Steam
  not running).
- It only accepts **RSA** keys, hence the second key,
  `~/.ssh/id_rsa_frame_devkit` (3072-bit).
- A host counts as found if port 22 **or** 32000 answers. With no host given,
  and `frame.local`/`frame` unreachable, it browses `_steamos-devkit._tcp` with
  `dns-sd` (macOS) or `avahi-browse` (Linux) for a few seconds if installed.
- Port 32000 closed, a timeout, or an error: the script says why and falls back
  to copying the ed25519 key with the Developer Mode password, as before.

Anyone on your network can send the request, so only approve a prompt you
started. `curl http://<frame-ip>:32000/properties.json` shows whether the service is up.

`~/.ssh/authorized_keys` lives under `/home`, which SteamOS keeps across OS
updates (inferred from Deck; the Frame uses the same A/B image scheme).

## From an iPhone or iPad

The iPhone app ([iphone.md](iphone.md)) makes its own ed25519 key and adds it
with the Developer Mode password, once, over a password login; the Frame's sshd
offers `publickey,password` (OpenSSH 9.7p1, keyboard-interactive off). It can't
use the devkit pairing above: that installs an RSA key, and the Swift SSH
library signs RSA only with SHA-1, which OpenSSH 8.8 and later refuse by default.
The app pins the Frame's host key on first use and asks you to pair again if it
changes. **Verified 2026-09-27** against the Frame's recovery image
([recovery-and-images.md](recovery-and-images.md)); on the headset, the add-the-key-yourself
route was used.

## Keeping `sshd` enabled across updates

- **Frame**: SSH is tied to the Developer Mode toggle, so it should survive
  updates as long as Developer Mode stays on. (Inferred: Valve doesn't say how
  the toggle is implemented.)
- **Deck (for comparison)**: `systemctl enable sshd` usually persists because
  `/etc` is an overlay that survives updates. Changes under `/usr` do not.
- Don't `pacman -S` anything you depend on for access. Packages installed into
  the read-only rootfs are **wiped by OS updates** on SteamOS. Use Flatpaks
  (`--user`) or `~/` for anything that needs to persist.

## Hardening (optional: `./scripts/connect.sh --harden`)

The script writes `/etc/ssh/sshd_config.d/01-frame-keys-only.conf` with
`PasswordAuthentication no` and `KbdInteractiveAuthentication no`, then reloads
`sshd`. First, it checks that key login works in BatchMode, so you can't lock
yourself out.

- Needs `sudo` (Developer Mode password), entered on the **Mac**.
- It assumes `/etc/ssh/sshd_config` includes `sshd_config.d/*.conf`, which is
  the Arch default. The script checks for this and stops if the include is
  missing.
- `/etc` drop-ins normally persist across SteamOS updates (inferred from Deck).
- It doesn't affect RDP (xrdp) or `sudo`, which still use the password.
- Undo: `ssh frame 'sudo rm /etc/ssh/sshd_config.d/01-frame-keys-only.conf && sudo systemctl reload sshd'`.

## Other shells

- **ADB over USB-C** to the native Linux OS:
  `adb shell`. Plug the headset into the Mac. Valve notes that USB power may be
  insufficient. Install with `brew install android-platform-tools`. This is
  useful if Wi-Fi SSH is broken.
  (Confirmed (Frame): [debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging))
- **ADB over Wi-Fi** reaches the **Lepton (Android) container**, not Linux:
  `adb connect frame:5555`. It only works while "Lepton Development" or an
  Android app is running.
  (Confirmed (Frame): [adb_lepton](https://partner.steamgames.com/doc/steamhardware/steamframe/adb_lepton))
- **RDP**: xrdp with user `steamos` and the Developer Mode password (see
  [streaming.md](streaming.md)).

## Fallback bootstrap one-liner

Use this only if the Developer Mode toggle doesn't give you SSH (for example,
an OS build without it).

1. On the Mac: `./scripts/serve-bootstrap.sh`. It serves
   `bootstrap-on-frame.sh`, with your `~/.ssh/id_ed25519_frame.pub` embedded,
   on port 8765, and prints the exact one-liner.
2. On the Frame's Linux desktop, open **Konsole** and type the printed line,
   roughly `curl -fsS mac.local:8765|bash` (~30 characters). If `mac.local`
   doesn't resolve, the script prints an IP form instead.
3. The bootstrap installs the key into `~steamos/.ssh/authorized_keys`, and
   then runs `sudo systemctl enable --now sshd`. `sudo` asks for a password,
   and if none is set yet, it tells you to run `passwd` first. That means
   typing the password on the headset one more time.
4. Stop the server on the Mac with Ctrl-C.

This is plain HTTP on your LAN, and it only serves a public key, so the
content isn't secret. Anyone on the LAN who can spoof your Mac's address could
serve a different script, though, so use it only on a trusted network.
