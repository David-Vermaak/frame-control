# Frame Control for AI agents

**Documented interface:** Frame Control's own stdlib Python MCP adapter wraps
its loopback HTTP API. No API key, hosted service, model SDK or third-party
helper app is needed. The assistant is our HTML/Python implementation hosted
in the platform Chromium browser. Its optional LLM endpoint is user configuration.
Installing other apps is an optional management action, never a prerequisite.

## Connect an MCP client

Start the HTTP server from this checkout:

```sh
python3 ui/server.py --port 47810
```

Add a stdio server to your MCP client (use absolute paths):

```json
{
  "mcpServers": {
    "frame-control": {
      "command": "python3",
      "args": ["/absolute/path/frame-control/ui/frame_mcp.py", "--url", "http://127.0.0.1:47810"]
    }
  }
}
```

The desktop app uses a random port; use that port with `--url`, or run the
checkout server above. If the HTTP server uses `FRAME_UI_KEY`, pass the same
value in the MCP process environment. This is local access control, not an LLM
API key. The adapter only accepts loopback HTTP servers, refuses redirects and
ignores environment proxies. Stdout contains newline-delimited JSON-RPC only.
It supports MCP initialization, ping, tool listing and tool calls; no sampling,
resources, prompts or streaming transport.

| Tool | Arguments | Effect |
|---|---|---|
| `status` | none | Battery, services, installed games and Flatpaks |
| `screenshot` | `view`: `headset` (default) or `desktop` | Returns PNG image content to the MCP client |
| `job` | `id` | Background install status; poll until `done`, inspect `error` |
| `launch` | `appid` | Launch an installed Steam app |
| `install` / `uninstall` | `id` | Install from Flathub / remove a user Flatpak |
| `send_text` | `text` | Frame desktop clipboard; desktop must be open |
| `send_file` | `path` | File on the HTTP server computer, up to 16 MiB, copied to Frame `~/Downloads` |
| `panel` | `id` | Launch an installed Flatpak as a panel using the existing launcher |
| `power` | `action`: `suspend`, `reboot`, `poweroff` | Open a terminal for the user to enter the sudo password |
| `keep_awake` | `action`: `on`, `off`, `status` | Optional keep-awake script interface |

Only install free software with its developer's consent. There is no purchase,
entitlement bypass or arbitrary shell tool. `install` returns a background job
ID; it does not claim the installation has finished. APK and sideloaded title
installs remain in the main UI for now.

### Approval is a separate human action

Every mutation first returns an `approvalUrl`, exact action and `confirmation`
token. Ask the user to open that URL and choose **Approve this action** or
**Reject**. Then repeat the same tool and arguments with the token in
`confirmation`. The server refuses execution before approval, changed arguments,
expired tokens and reuse. A file approval binds the content hash as well as the
path. Approvals last five minutes and disappear when the HTTP server restarts.
A failed execution also consumes the approval; review a fresh request to retry.
The panel does not execute an action merely because it was approved.

MCP has no approval tool. This is protection against accidental model tool
calls, not a sandbox against a client with independent shell/HTTP access to your
computer. Grant the MCP client only the access you intend. Status and captures
are returned directly to that client, which may forward them to its configured
model. The assistant's separate opt-in does not govern an external MCP client.

Power still requires the existing password prompt in a local terminal. MCP
never receives passwords. Power via `FRAME_LOCAL=1` is unsupported: use the main
UI. The panel launcher and keep-awake adapter require zsh on the computer.

[PR #16](https://github.com/saphid/frame-control/pull/16) owns
`scripts/keep-awake.sh on|off|status`. This branch does not copy or change it.
Until that script is present, the tool reports it unavailable. Keep-awake is
never automatic: `on` changes the shared idle timers; explicitly approve `off`
to restore them after work. It is not a per-agent lease; coordinate with other
users. No changes are made to the analytics/update interfaces in
[PR #17](https://github.com/saphid/frame-control/pull/17). Prompts, keys, model
replies, screenshots and approval payloads are not sent to analytics.

## Assistant panel

Open **Tools → Open assistant**, or `http://127.0.0.1:47810/assistant`.
To put the same page in the headset, with the HTTP server still running:

```sh
python3 scripts/assistant-on-frame.py --port 47810
```

This starts an SSH reverse forward bound to Frame loopback (port 47812 by
default), then a dedicated Chromium profile tagged as a SteamVR panel. Keep the
command running. Ctrl-C closes this browser profile and the tunnel; it leaves
other Chromium windows and the existing HTTP server alone. A failed cleanup
prints the temporary profile path so it can be removed when the Frame returns.
Use `--frame-port` if the default is busy. Chromium must already be available as
`org.chromium.Chromium`; the launcher never installs anything automatically.
Place the panel with SteamVR's normal docking controls.

Enter your full **chat-completions endpoint**, model name and optional key.
An OpenAI-compatible local server works without a key; no OpenAI account is
required. HTTP is allowed only on loopback; other endpoints require HTTPS.
Loopback refers to the computer running the HTTP server, even in the headset.
Endpoints with embedded credentials, query strings or redirects are refused.

Check the message consent box and press **Send message**. Screenshot context is
a separate unchecked box and sends one fresh capture with that request. Both
boxes reset after sending, and changing endpoint/model revokes consent. Nothing
is sent when opening the page or entering configuration. There is no model
list fetch, saved history, automatic screenshot capture or assistant telemetry.
Each send is independent: previous messages and replies are not included.

Configuration, credentials and chat remain in page memory; close/reload the page
or choose **Clear everything** to clear them. A request already sent cannot be
recalled. Only the chosen endpoint gets the request; proxy environment variables
and redirects are disabled. Its privacy and retention policy still applies.
Replies are plain text and cannot call tools or operate the Frame. A model must
support image inputs to accept screenshot context.

## Evidence and limits

**Verified 2026-09-28, SteamOS 0.4.1, BUILD_ID 20260925.6191901:** loopback HTTP
status through an SSH reverse tunnel; platform Chromium created a separate
SteamVR panel (confirmed in `GAMESCOPE_FOCUSABLE_APPS`); headset capture returned
a PNG. These checks preceded the UI implementation. No power or global settings
were changed.

**Inferred:** visual comfort and controller keyboard usability while wearing
the headset; panel creation in gamescope alone does not establish these.
Windows/Linux launcher support, live third-party model endpoints, installs,
uninstalls, power and keep-awake changes are not covered by that feasibility
check. See the PR for the final unit and end-to-end results.

**Verified end to end on the same Frame/build (2026-09-28):** a stdio MCP client
initialized, read status, retrieved a headset PNG, and transferred a test file
only after approval through the Chromium page. Remote file bytes matched;
reusing the confirmation was rejected. The actual headset Chromium page sent
text and then separately opted-in image context to a local test endpoint and
displayed its replies. Without consent there were zero endpoint requests.
The test endpoint returned canned replies: model inference and a live external
provider remain **unverified**. The launcher’s Ctrl-C cleanup was checked;
profiles, SSH tunnels and the test file were removed. No installs, removals,
launches of user games, power operations or keep-awake changes were performed.

**Verified locally:** unit coverage includes the stdio subprocess, approval
binding/expiry/replay/concurrency, file-change rejection, and a real local HTTP
endpoint for opt-in, text/image payloads and redirect refusal. Fake-Frame
regressions are in `tests/e2e/test_agents.py`; local Docker execution was blocked
because the Docker daemon was unavailable. CI runs those regressions.
