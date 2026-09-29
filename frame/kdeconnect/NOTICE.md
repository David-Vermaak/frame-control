# KDE Connect for the Steam Frame

Frame Control's keyboard and trackpad work through KDE Connect running on the
Frame. The Frame doesn't come with it, so Frame Control ships it: the six
packages below, unchanged, exactly as Valve builds them for the Frame's
SteamOS (aarch64, packaged by "GitLab CI Package Builder
<ci-package-builder-1@steamos.cloud>"). On first use, Frame Control copies them
to the Frame over SSH and unpacks them into
`~/.local/share/frame-control/kdeconnect`.

They are separate programs and libraries under their own licences, listed
below; their licence texts are in `LICENSES/<package>/`. Frame Control itself
is MIT-licensed. It doesn't link to or include any of this code: it starts
`kdeconnectd` and talks to it over KDE Connect's network protocol, as a phone
would.

| Package | Version (Valve's build) | Licence | Complete source (Valve's source package) | Upstream |
|---|---|---|---|---|
| kdeconnect 24.02.2-1 | 24.02.2-1 | GPL-2.0-only or GPL-3.0-only or any later version accepted by KDE e.V. (LicenseRef-KDE-Accepted-GPL); some files LGPL-2.1/3.0, MIT, BSD-3-Clause, Apache-2.0, CC0-1.0 | [kdeconnect-24.02.2-1.src.tar.gz](https://github.com/saphid/frame-control/releases/download/kdeconnect-frame-24.02.2-1/kdeconnect-24.02.2-1.src.tar.gz) | [KDE](https://download.kde.org/stable/release-service/24.02.2/src/kdeconnect-kde-24.02.2.tar.xz) |
| kcontacts 6.1.0-1 | 1:6.1.0-1 | LGPL-2.0-or-later; some files MIT, BSD-3-Clause, CC0-1.0, Unicode-DFS-2016 | [kcontacts-6.1.0-1.src.tar.gz](https://github.com/saphid/frame-control/releases/download/kdeconnect-frame-24.02.2-1/kcontacts-6.1.0-1.src.tar.gz) | [KDE](https://download.kde.org/stable/frameworks/6.1/kcontacts-6.1.0.tar.xz) |
| kpeople 6.1.0-1 | 6.1.0-1 | LGPL-2.1-or-later; some files BSD-3-Clause, CC0-1.0 | [kpeople-6.1.0-1.src.tar.gz](https://github.com/saphid/frame-control/releases/download/kdeconnect-frame-24.02.2-1/kpeople-6.1.0-1.src.tar.gz) | [KDE](https://download.kde.org/stable/frameworks/6.1/kpeople-6.1.0.tar.xz) |
| modemmanager-qt 6.1.0-1 | 6.1.0-1 | LGPL-2.1-only or LGPL-3.0-only (or later accepted by KDE e.V.); some files BSD-3-Clause, CC0-1.0 | [modemmanager-qt-6.1.0-1.src.tar.gz](https://github.com/saphid/frame-control/releases/download/kdeconnect-frame-24.02.2-1/modemmanager-qt-6.1.0-1.src.tar.gz) | [KDE](https://download.kde.org/stable/frameworks/6.1/modemmanager-qt-6.1.0.tar.xz) |
| pulseaudio-qt 1.4.0-3 | 1.4.0-3 | LGPL-2.1-only or LGPL-3.0-only (or later accepted by KDE e.V.) | [pulseaudio-qt-1.4.0-3.src.tar.gz](https://github.com/saphid/frame-control/releases/download/kdeconnect-frame-24.02.2-1/pulseaudio-qt-1.4.0-3.src.tar.gz) | [KDE](https://download.kde.org/stable/pulseaudio-qt/pulseaudio-qt-1.4.0.tar.xz) |
| libfakekey 0.3-2 | 0.3-2 | LGPL-2.0-or-later (Copyright © 2004 OpenedHand) | [libfakekey-0.3-2.src.tar.gz](https://github.com/saphid/frame-control/releases/download/kdeconnect-frame-24.02.2-1/libfakekey-0.3-2.src.tar.gz) | [Yocto Project git, tag 0.3](https://git.yoctoproject.org/libfakekey/tag/?h=0.3) |

## Source code

Each source package is Valve's, copied unchanged: the upstream release
tarball (with its signature where upstream signs it), the PKGBUILD Valve built
from, and the signing keys. None of them carry patches. Frame Control
publishes them next to the binaries, in the
[kdeconnect-frame-24.02.2-1 release](https://github.com/saphid/frame-control/releases/tag/kdeconnect-frame-24.02.2-1),
so the source is available from the same place as the binaries for as long as
Frame Control distributes them. `packages.json` pins each binary and source
package by SHA-256.

If a link ever stops working, open an issue at
https://github.com/saphid/frame-control/issues and we'll send you the source.

## Replacing them

These are ordinary files in your home folder on the Frame: you may replace
them with your own builds, and Frame Control will use whatever `kdeconnectd`
is at `~/.local/share/frame-control/kdeconnect/root/usr/lib/kdeconnectd`
(or the Frame's own `/usr/lib/kdeconnectd`, if SteamOS ever ships one).
Deleting `root/.frame-control-packages` there makes Frame Control copy its
bundled build back on next use.
