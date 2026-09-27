# A Frame stand-in for testing without the headset

Valve publishes no Steam Frame OS image. This builds the closest thing: Valve and
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
