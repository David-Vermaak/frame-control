# Library fixtures

`icon.png` is a synthetic 2×2 RGBA fixture with opaque, half-transparent and
transparent pixels. `steam-responses.json` includes the Open Saber Plus
shortcut ID/name and home path read from the Frame's existing metadata on
2026-09-28; the `configure` response is synthetic, matching our helper's
contract. Tests never contact the network.

The existing fakeframe CEF shim models artwork and collection methods from
SteamTracking's `ClientExtracted/steamui/chunk~2dcc5aaf7.js` and
SteamDeckHomebrew/decky-frontend-lib's `src/globals/steam-client/App.ts`, read
2026-09-28. These methods were not captured from this headset: Steam's client
was unavailable. The Node-based test runs the actual generated JavaScript
against that fixture; it is skipped when Node is absent.

`icon.jpg` is the same synthetic icon converted with macOS `sips` to exercise
JPEG SOF parsing. `sips` is not used by the product or tests.
