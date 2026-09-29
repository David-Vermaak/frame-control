# Installing and buying Steam games from the Mac

Frame Control's **Get games** section lists the games you own with each one's
Steam Frame rating, installs them on the Frame, and searches the Steam store.
This page covers how it works underneath, so you can do the same from a shell.

Installed Unreal Engine games also have a **VR mod** button that installs,
starts and removes UEVR; see [VR mods](mods.md) for how it works and the
per-game table. Mod support is separate from Steam's Frame rating.

## How it works

The Frame's Steam client runs with `-cef-enable-debugging`. So its UI, a
Chromium page, answers the Chrome DevTools protocol on the Frame's loopback,
`127.0.0.1:8080`. The page titled **SharedJSContext** holds the client's own
state and API:

| Object | What it gives you |
|---|---|
| `appStore.allApps` | Every app the account owns (868 games here), with `local_per_client_data.installed`, playtime, `vr_supported`/`vr_only` and `steam_hw_compat_category_packed` |
| `downloadsStore.m_DownloadOverview` | A Map keyed by client ID. `"0"` is this machine: current app, percent, ETA, bytes/s |
| `SteamClient.Installs.*` | The install wizard: `GetInstallManagerInfo`, `ContinueInstall`, `CancelInstall`, `OpenInstallWizard` |
| `SteamClient.User.GetIPCountry()` | The store country (`AU` here), which store search needs |

`ui/frame_steam.py` is a stdlib-only WebSocket client for this page. It's piped
over SSH like the other helpers:

```sh
ssh frame 'python3 - owned'          < ui/frame_steam.py   # owned games + download
ssh frame 'python3 - install 274190' < ui/frame_steam.py   # install Broforce
ssh frame 'python3 - store 1145360'  < ui/frame_steam.py   # store page in the headset
```

The debugger port only listens on the Frame's loopback, so it's reachable over
SSH and not from the network.

## Installing a game you own

`steam steam://install/<appid>`, run over SSH, hands the URL to the running
client, which opens its install wizard. The wizard's state
(`GetInstallManagerInfo().eInstallState`) then tells you what happens next:

| State | Meaning | What `frame_steam.py` does |
|---|---|---|
| 14 complete | Steam skipped the options dialog and queued the download | Reports "queued" |
| 7 config | The options dialog is showing in the headset (library folder, compatibility note) | Calls `ContinueInstall()` when the game fits on disk, as the headset's Install button does |
| 3, 4, 6, 8, 13 | Free license, CD key, password, EULA, signup | Leaves them for you to answer in the headset |
| 15 failed | Error | Reports `errorDetail` |

**Verified 2026-09-25 (SteamOS 0.3.0, build 20260922.6101926):**

- Balatro (2379780, 67 MB) went straight to state 14 and installed in about 7 s,
  with nothing to answer in the headset.
- Broforce (274190, 0.6 GB) stopped at state 7. Calling `ContinueInstall()` over
  DevTools queued the download, and the game installed.
- Calling `SteamClient.Installs.OpenInstallWizard([appid])` directly did nothing:
  the state stayed at 0. Go through the `steam://install` URL instead.

**Inferred** from the client's JS: Steam skips the options dialog when there's
one library folder, the game fits, and there's no compatibility note to show.
Broforce is Deck "Playable", which probably explains why it stopped.

## Frame ratings

`steam_hw_compat_category_packed` holds two bits per device. The client decodes
it like this (from `steamui/chunk~2dcc5aaf7.js`):

| Device | Bits |
|---|---|
| Steam Deck | `packed & 3` |
| SteamOS | `packed >> 4 & 3` |
| Steam Machine | `packed >> 6 & 3` |
| **Steam Frame** | `packed >> 8 & 3` |

The values are 0 unknown, 1 unsupported, 2 playable and 3 verified. On
2026-09-25 this library had 12 Frame Verified, 2 Playable, 6 Unsupported and 848
Unknown games.

For games you don't own, the store's public
`saleaction/ajaxgetdeckappcompatibilityreport?nAppID=<id>` returns
`frame_resolved_category` on the same scale, along with `resolved_category`
(Deck), `steamos_resolved_category` and `machine_resolved_category`. No key or
login is needed.

## Buying

Frame Control doesn't buy anything. Purchases happen on Steam's own store page,
signed in as you:

- **Buy on Steam ↗** opens `store.steampowered.com/app/<id>/` in the Mac's
  browser (the Electron app sends `target=_blank` links there).
- **Store on Frame** runs `steam steam://store/<id>`, which opens the page in the
  Steam client on the headset. **Verified 2026-09-25:** a "Hades on Steam" page
  appeared in the DevTools page list. It wasn't visible in the headset capture
  because an app was in the foreground; it opens in Steam's dashboard.

After buying, press **Refresh** in Get games. The game shows up as owned, and
**Install on Frame** installs it.

Store search uses `store.steampowered.com/api/storesearch/?term=…&cc=…`. It
returns nothing without `cc`, so Frame Control takes the country from
`SteamClient.User.GetIPCountry()` on the Frame.

## Not yet checked

- Free-to-play games: `steam://install` should stop at state 3 (free license)
  for you to accept in the headset. Not tried, because it adds a license to the
  account.
- Games with a EULA (state 8).
- Installing when there's more than one library folder, such as a microSD card.
- Uninstalling. `steam://uninstall/<appid>` should open a confirmation in the
  headset.
