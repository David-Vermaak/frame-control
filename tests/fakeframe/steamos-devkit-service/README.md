# Valve's steamos-devkit-service (vendored, for the fake Frame only)

Unmodified copy of [steamos-devkit-service](https://gitlab.steamos.cloud/devkit/steamos-devkit-service):
the HTTP service on port 32000 (`src/`) and its hooks (`hooks/`), which the
fake Frame image installs at `/usr/lib/steamos-devkit/` and
`/usr/share/steamos-devkit/hooks/`, where the service looks for them.

- Source: commit `74cf8fe` (2025-09-16, tag `v0.20250916.0`).
- Licence: the repository's `LICENSE` is the LGPL 2.1 (copied here).
  `src/steamos-devkit-service.py` itself carries an MIT header, and
  `hooks/devkit-1-identify` and `hooks/install-ssh-key` carry LGPL-2.1+
  headers. Nothing here ships in Frame Control; it's only built into the
  test image.

The service imports `dbus` to advertise itself over mDNS through
systemd-resolved, which a container doesn't have. The image puts a small
stand-in `dbus` module on its `PYTHONPATH` (`rootfs/usr/local/lib/fakeframe/pystubs`)
that records the registration instead. Everything else runs as Valve wrote it.

To update: copy `src/`, `hooks/` and `LICENSE` from a newer checkout and update
the commit line above.
