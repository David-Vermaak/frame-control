# Recovery images and OS images for the Frame

Where to get the Steam Frame's operating system, what's inside it, and how to
run it for testing without the headset. For recovering a Frame that won't boot,
see the boot menu and boot-loop entries in
[how-the-frame-works.md](how-the-frame-works.md#facts-worth-knowing).

## Downloads

Valve's SteamOS download page (`store.steampowered.com/steamos/download`)
redirects to the [Installation and Repair FAQ](https://help.steampowered.com/en/faqs/view/65B4-2AA3-5F37-4227),
which offers the Steam Deck image. The **Steam Frame images are on the same
server** but aren't linked from that page:
**https://steamdeck-images.steamos.cloud/recovery/** (a plain directory
listing, checked 2026-09-27).

| File | Size | Use |
|---|---|---|
| `steamframe-oobe-repair-20260922.5153644-0.3.0.img.bz2` (or `.img.zip`) | 3.8 GiB | Write to an 8 GB+ USB-C stick, then **Boot from USB** in the Frame's boot menu |
| `steamframe-oobe-repair-qdl-20260922.5153644-0.3.0.tar.gz` (or `.zip`) | 3.8 GiB | Flash over a USB-C cable in Qualcomm EDL mode with `flash.sh` (Linux) or `flash.cmd` (Windows), which use [qdl](https://github.com/linux-msm/qdl). **Wipes everything** |

All four are dated 2026-09-22. Everything else there is for the Steam Deck
(`steamdeck-…`, x86-64), which won't run on the Frame. Valve publishes **no
checksums**. These are the SHA-256s of our downloads (2026-09-26), which passed
`bzip2 -t` and `tar -t`:

```
3a4a077f1b1f40688ab3279affcb56776bd97c54db1573e7c65fc52a97106676  steamframe-oobe-repair-20260922.5153644-0.3.0.img.bz2
d3323bfa8efe9ece1954948421cdf5f705e8942eb50c960e2916d935d1b850ab  steamframe-oobe-repair-qdl-20260922.5153644-0.3.0.tar.gz
```

Our copies, with a Mac EDL flashing script built on qdl (untested), are in
`~/Downloads/steam-frame-recovery/` on the Mac.

## What's inside the USB image

A GPT disk with 512-byte sectors and one A slot (a Frame has A and B slots;
the installer makes the rest). **Verified 2026-09-27** from
`steamframe-oobe-repair-20260922.5153644-0.3.0.img.bz2`:

| # | Name | Start sector | Size | Type GUID |
|---|---|---|---|---|
| 1 | `esp` | 34 | 256 MiB | `c12a7328-f81f-11d2-ba4b-00a0c93ec93b` (EFI system) |
| 2 | `efi-A` | 524322 | 64 MiB | `ebd0a0a2-b9e5-4433-87c0-68b6b72699c7` |
| 3 | `rootfs-A` | 655394 | 5120 MiB | `4f68bce3-e8cd-4db1-96e7-fbcaf984b709` |
| 4 | `var-A` | 11141154 | 256 MiB | `4d21b016-b534-45c2-a9fb-5c16e091fd2d` |
| 5 | `home` | 11665442 | 100 MiB | `933ac7e1-2eb4-4f13-b844-0e14e2aef915` |

The partitions start at sector 34, not on MiB boundaries, so compute offsets
from the table (sector × 512), not from rounded sizes. `rootfs-A` is **btrfs**
(label `rootfs-A`, 9.2 GB of files), mounted read-only on the Frame.
Its `/etc/os-release` says `NAME="SteamOS"`, `ID=steamos`, `ID_LIKE=arch`,
`VERSION_CODENAME=holo`; the running system reports version 0.3.0, variant
`vr`, build **20260922.5152327**, which is a different number from the
`5153644` in the file name. Our headset reports build 20260922.6101926.

Inside, it matches a real Frame:

- User `steamos` (uid 1000) is in `wheel` (gid 998), and sudoers has
  `%wheel ALL=(ALL) ALL`, so sudo asks for the Developer Mode password.
- `sshd_config` includes `sshd_config.d/*.conf`, uses `.ssh/authorized_keys`
  plus `AuthorizedKeysCommand /usr/bin/userdbctl ssh-authorized-keys %u`,
  and sets `KbdInteractiveAuthentication no` and `UsePAM yes`. So sshd offers
  `publickey,password`, the same as the headset.
- `/usr/bin` has `sshd`, `sudo`, `python3` and `podman`.

Get just the root filesystem without unpacking the whole 5.8 GB image (the
partition's start and size, in sectors, come from the table above):

```sh
bzcat steamframe-oobe-repair-*.img.bz2 | tail -c +$((655394 * 512 + 1)) | head -c $((10485760 * 512)) > rootfs-A.img
```

A Mac can't mount btrfs; a Linux machine or VM can (`mount -o ro -t btrfs`).

## Running it without the headset

The image can't boot in a generic virtual machine: its kernel and bootloader
are built for the Frame's Qualcomm Snapdragon 8 Gen 3 (**inferred**; not
attempted). Its **userland** runs fine on any ARM64 Linux, which covers
anything that talks to the Frame over SSH.

[`tests/frame-container/frame-image.sh`](../tests/frame-container/frame-image.sh)
extracts `rootfs-A`, mounts it read-only with a throwaway writable layer, and
starts the image's own `sshd` on port 2223 (user `steamos`, a test password;
`systemctl` only records requests). On a Mac, run it in Colima's ARM64 VM (see
[tests/frame-container/README.md](../tests/frame-container/README.md)).
**Verified 2026-09-27:** the iPhone app paired with it by password (the image's
sshd logged `Accepted password`, then `Accepted publickey … ED25519`), ran
Frame Control's server on the image's Python, and the image's sudo rejected a
wrong power password and passed the right one to `systemctl`. Without the
Frame's hardware there's no SteamVR, Steam client, battery or Lepton, so those
parts stay untested this way.

## Holo Core aarch64 (Valve and Collabora)

The ARM64 port of Arch Linux that the Frame's SteamOS is built on, published as
a preview in July 2026 ([Collabora's announcement](https://www.collabora.com/news-and-blog/news-and-events/building-an-arch-linux-aarch64-port-for-holo-core.html)).
It's a base system and build environment, not the Frame's OS:

- Source: `https://gitlab.steamos.cloud/holo/holo-core-aarch64-preview`
- Packages: `https://holo-packages.steamos.cloud/holo-core-aarch64-preview/mash-20251118`
- Container: `registry.gitlab.steamos.cloud/holo/holo-core-aarch64-preview/base-devel:latest`
  (1.7 GB; `/etc/os-release` says "Holo core Aarch64 port (preview)"; `pacman`
  installs OpenSSH 10.2, Python 3.13 and sudo from its repositories. Checked 2026-09-27.)

[`tests/frame-container/Dockerfile`](../tests/frame-container/Dockerfile) builds a
lighter Frame stand-in on it (a `steamos` user with a password and sudo, sshd
with keys and passwords), handy when you don't have the 4 GB image.
