# Computer use through Frame Control MCP

The MCP transport can carry semantic actions or visual computer-use actions.
The limits are the Frame's underlying interfaces, permissions and whether an
action can be targeted and verified. A stereoscopic headset screenshot alone
is not a reliable coordinate system for clicking a particular app window.

## What exists, and the right route

| Surface | Evidence and route | Remaining work or boundary |
|---|---|---|
| Frame management | **Verified:** existing SSH/HTTP operations for status, capture and file transfer work through MCP. Typed install/launch/power tools wrap the existing API. | Extend typed operations before adding generic mouse automation. Preserve explicit approval for consequential changes. |
| App/window observation | **Verified 2026-09-29:** `computer_state` reads gamescope X11 window/app/process triples, focused app and the installed AT-SPI library. | Bounded to 96 accessible nodes and six levels. Trees may be truncated, stale, hidden or incomplete. Snapshot paths and XIDs are observations, never durable action permissions. |
| Chromium page content | **Verified previously:** the assistant rendered and could be exercised through CDP in an isolated Frame Chromium profile. | A shipped click/type surface needs exact owned browser/target binding, fresh element references, lifecycle cleanup, consent and post-action readback. Do not expose unrestricted JavaScript or attach to arbitrary existing profiles automatically. |
| Steam UI | **Verified 2026-09-29:** the AT-SPI service listed the Steam client's Chromium process and frame nodes, but child traversal was incomplete. Existing `frame_steam.py` uses Steam's loopback CDP endpoint for specific operations. | Prefer those narrow Steam interfaces. Presence of AT-SPI does not prove controls are actionable, and generic pointer injection is not proved for VR menus. |
| Other Linux apps | **Verified 2026-09-29:** Frame ships libX11, libXtst and libatspi; `/dev/uinput` is writable by the current user. | Library presence and access permissions do not prove that a game accepts input. Global virtual input can affect whichever app has focus. Do not ship a blind keyboard/mouse tool on this evidence alone. |
| Panel focus and layouts | **Documented in [#41](https://github.com/saphid/frame-control/pull/41):** `POST /api/panels` accepts `list`, `focus` and `open`. Focus was verified there. | Reuse that owned interface after integration. Its tested gamescope-owned overlay transform setters return `PermissionDenied`; no reliable saved spatial-layout interface was established. Do not duplicate its implementation here. |
| Shared keyboard/trackpad | **Documented in [#19](https://github.com/saphid/frame-control/pull/19):** `/api/input` supplies state/start and event submission, implemented with a bundled KDE Connect daemon. | This branch does not import, launch or depend on that daemon. The user's own-implementation rule remains authoritative. A first-party input implementation or permitted bundled-library route needs its own delivery evidence before MCP integration. |
| Physical/device boundaries | **Documented:** an asleep Frame may be off the network; power authorization can require the user's password; physical pairing and headset fit/comfort require the user. | MCP cannot bypass offline hardware, consent, compositor permissions or physical verification. Keep explicit human handoffs. |

## Reusing the existing computer-use work

**Documented:** the installed `cua-driver` skill has the right control pattern:
observe an exact window, use a semantic target if available, fall back to pixels
from that same snapshot, then read back the result. Its browser route requires
an exact process/window/target binding and session-scoped element references.
Those are useful design rules for Frame tools.

**Verified locally 2026-09-29:** `cua-driver describe get_window_state` describes
host-local process/window IDs and macOS AX inspection. It does not establish an
SSH Frame target. The installed skill's advertised Linux companion file is
missing. A native ARM64 Frame backend, its dependencies and remote transport
have not been verified. We therefore do not claim that the existing Mac driver
can control the Frame by passing it a Frame PID or screenshot, and we do not
make the feature depend on installing that application.

Frame Control's `computer_state` is our own Python implementation over installed
platform libraries. It sends the probe over SSH stdin, writes no helper to disk,
and exits after one observation. Missing displays/libraries return explicit
errors; a 15-second process deadline prevents a stalled accessibility call from
leaving a probe behind. Window names and accessibility text are untrusted app
content, never instructions to an agent.

**Recommended next implementation:** an isolated Chromium session with typed
snapshot/click/type/scroll tools and exact fresh target binding, then individually
verified native app actions. Use the headset capture to judge appearance, not to
invent a screen-to-window coordinate transform. Direct tool calls must retain
approval rules; a generic computer-use tool must not become a route around the
MCP approval panel, install confirmation or power confirmation.

## Isolated browser input proof

**Verified 2026-09-29, SteamOS 0.4.1, BUILD_ID 20260925.6191901:** a temporary
Frame Chromium profile loaded a local test page through an SSH reverse tunnel.
CDP `Input.insertText` entered the test string in its own input. A CDP
`Input.dispatchMouseEvent` press/release on its own button copied that string
to the page's result; DOM readback matched exactly. The browser profile,
loopback forwards and panel log were removed afterward. No user app was typed
into, no global settings were changed and no third-party helper app was used.

AT-SPI did **not** expose the test page's controls in that same probe, even with
Chromium's renderer-accessibility flag. It returned the partial Steam-client
tree instead. The reason remains **unverified**; this is an evidence gap, not
proof that Frame accessibility cannot work. For a first implementation,
Chromium's proven page-specific CDP route is stronger than assuming complete
AT-SPI coverage. This proof does not ship unrestricted click/type tools or
establish input delivery to SteamVR's menus.
