# Family and comfort

Frame Control's Home tab has a **Family and comfort** card, on desktop and
on iPhone. No third-party notification or parental-control app is needed.
This is Frame Control code using Python, Steam and SteamVR already on the Frame.

![Family and comfort controls in the desktop app](img/comfort-desktop.png)

## Sessions

Set a limit of 1–240 minutes, optional break and check-in intervals, then
**Start session**. Break and check-in intervals of 0 turn those reminders off.
**Cancel session** cancels the timer and monitoring without changing the game.
Cancel before starting a session with different settings.

The Frame shows a one-minute warning, then opens Steam Home in its dashboard.
**Games stay running**: save and pause before the limit. Some games pause when
the dashboard opens; others do not. There is no kill, power-off, Steam restart,
account restriction or parental lock. The wearer can return to the game.

**Documented implementation:** the timer is a single, opt-in Python worker in
the Frame user's account. Desktop and iPhone share its state. It keeps going
when the companion disconnects, closes or is suspended. It exits after
completion or cancellation (normally within five seconds). Cancellation waits
for any in-flight SteamVR action to finish within its timeout; it is not a boot
service. A Frame reboot invalidates the session. Suspend counts toward the
limit, using Linux's boot-time clock. If a warning was delayed by suspend or a
SteamVR failure, Home waits until at least a full minute after a successful
warning. A failed Home transition remains active and retries, with an error
shown in the companion. A stale worker is reported as unverified enforcement.

## Alerts and breaks

During a session, battery, overheating and check-in alerts go to connected
companions. Break reminders and session warnings also appear on the headset.

- **Low battery:** 15% or below while discharging. One alert until charging or
  recovery to 20%, so values around 15% do not produce repeated notifications.
- **Overheating:** a thermal zone reaches its own kernel-reported hot/critical
  trip, or the battery reports `Overheat`. Missing sensors mean unknown, not
  safe. These are status alerts, not medical advice or an extra thermal governor.
- **Check in:** an alert after the chosen number of active minutes.
- **Breaks:** a SteamVR reminder and companion notification at the chosen interval.

**Inferred:** SteamVR activity levels 1 and 2 are a useful proxy for use, not
proof someone is wearing the headset. Inactive readings reset continuous use;
missing readings add no time. Long gaps count at most 30 seconds. Breaks and
check-ins are distinct from the elapsed-time session limit.

Click **Enable / test notifications** on each companion. iOS asks for permission;
macOS, Windows and Linux follow their notification settings. The page also shows
recent events and errors. Keep Frame Control open and connected for companion
alerts. **Phone alerts are local, not push notifications:** iOS suspension,
force-quit or a lost SSH connection prevents live delivery. Old alerts are not
replayed as a notification burst on reconnect. Headset warnings and the session
limit continue without the phone. A physical iPhone's background delivery has
not been verified and is not guaranteed.

## Casting

**Cast to this screen** (Home → **Right now**) starts the existing headset Live view and requests full
screen where supported. Show that screen to people in the room, or use the
computer/phone's own screen mirroring. It creates no new stream transport,
public URL or LAN server. iPhone uses the inline viewer if full screen is not
available. The image includes private content visible to the wearer.

## What is installed

The shared authenticated `/api/comfort` endpoint copies three bundled Python
files to `~/.cache/frame-control/comfort/<content-hash>/`. Session state and
locks live in `~/.local/state/frame-control/comfort/`, with a private directory
and 0600 state file. There is no network listener or system service. Cancel a
session before removing these directories. The iPhone's normal server still
exits on disconnect; the explicitly started comfort worker is the exception.

## Verification

**Verified 2026-09-28**, SteamOS 0.4.1, build `20260925.6191901`: shipped
`/opt/steamvr/bin/linuxarm64/vrcmd --notify TEXT` reported success for a custom
reminder. Steam's CDP `SteamUIStore.Navigate('/library/home')` and
`SteamClient.OpenVR.VROverlay.ShowDashboard('valve.steam.gamepadui.main')`
opened Home while the running app ID stayed unchanged. Prior page and dashboard
visibility were restored. Kernel hot/critical trips and SteamVR activity were
read from the real device. No temperature or battery fault was induced.

**Verified locally:** deterministic fake-Frame tests cover late warnings,
failed warnings/Home actions, cancellation, activity gaps, thresholds, duplicate
suppression, reboot invalidation, shared session state and the exact Home
JavaScript. `python3 -m unittest discover -s tests` runs them. The iOS Simulator
build tests notification content and bounds. Physical iPhone delivery and
wearer-perceived headset notification visibility remain unverified.

**Verified end to end on the same Frame:** a two-minute session with no companion
connection for 135 seconds emitted its warning, break and check-in, then opened
Home. The running app ID was unchanged; the test restored the previous page and
dashboard visibility and confirmed the worker exited. Casting through the Home
shortcut decoded the existing headset stream at 30 fps.

**Verified on the iOS 26.5 Simulator:** connected to the real Frame, approved the
notification prompt, and saw the native Frame Control test banner. Seven iOS
tests passed.

![Native test notification in the iOS Simulator](img/comfort-notification-ios.png)

Desktop and 390-pixel phone layouts had no horizontal overflow.
On macOS the development Electron app's real notification attempt was denied
(`UNErrorDomain` 1); the bridge now returns that failure instead of reporting
success. Successful macOS/Windows/Linux notification display remains unverified.

**Verified on the real Frame:** its naturally discharging 15% battery produced
one low-battery event during a short session; the test then cancelled the
session. Overheating alerts use fake sensor samples in tests: the shared
headset was not deliberately overheated.

**Verified 2026-09-29 on the same Frame:** a fresh one-minute session opened
Home more than 60 seconds after the successful warning. The test restored the
previous page and dashboard visibility. Local regression coverage now includes
slow notification delivery, a total Home-action timeout, failed worker startup,
unreadable saved state, malformed activity samples and notification UX: 173
Python tests passed. Desktop and 390-pixel layouts were checked again; system
notification-denial guidance stayed visible across polls. Initial event history
did not replay notifications, and only the latest new event was announced.
