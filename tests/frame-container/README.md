# Testing without the headset

## Valve's own Frame OS, from its recovery image

Valve publishes a Steam Frame recovery image at
https://steamdeck-images.steamos.cloud/recovery/ (`steamframe-oobe-repair-*.img.bz2`,
~4 GB). `frame-image.sh` extracts its `rootfs-A` partition (btrfs), mounts it
read-only with a throwaway writable layer, and starts the image's own sshd on
port 2223, so the iPhone app can pair with, and run its server on, the real
SteamOS for Frame userland (Python, sudo, sshd, PAM). It needs Linux with btrfs,
e.g. Colima's VM on a Mac:

```sh
colima start --arch aarch64 --vm-type vz
colima ssh -- sudo sh tests/frame-container/frame-image.sh ~/Downloads/steamframe-oobe-repair-<build>.img.bz2
# pair with 127.0.0.1:2223, user steamos, password frame-test-pw
```

The image's kernel is built for the Frame's Qualcomm chip, so this runs its
userland, not the whole OS: no SteamVR, Steam client, battery or Lepton.

## Holo Core stand-in

A lighter option: Valve and
Collabora's [Holo Core aarch64 preview](https://www.collabora.com/news-and-blog/news-and-events/building-an-arch-linux-aarch64-port-for-holo-core.html)
(the Arch Linux ARM64 base the Frame's SteamOS is built on) with the Frame's SSH
surface: a `steamos` user with a password and sudo, OpenSSH taking keys and
passwords, Python and rsync. `systemctl` only records what it's asked to do.

It exercises pairing with the password, the host-key pin, the server running on
the "Frame" (FRAME_LOCAL=1) and the power password check. It has no SteamVR,
Steam, battery, cameras or Lepton, so those panels are empty.

```sh
docker build --platform linux/arm64 -t frame-holo-test tests/frame-container
docker run -d --name frame-holo -p 127.0.0.1:2222:22 frame-holo-test
# password: frame-test-pw. In the iPhone app (Simulator), pair with 127.0.0.1:2222.
docker exec frame-holo cat /tmp/power-requests.log   # what power actions asked for
```

On a Mac without Docker: `brew install colima docker && colima start --arch aarch64 --vm-type vz`.
