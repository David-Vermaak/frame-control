# Linux desktop in the headset

Frame Control on Linux can show a screen or a window of your desktop as its
own panel in the Steam Frame, as [Mac in the headset](mac-in-headset.md) does
on a Mac. You place the panel with the SteamVR dashboard; the laser clicks and
scrolls, and you type on the computer's keyboard. Find it under
**Tools → Desktop in the headset**.

The confidence labels are the same as in [ssh.md](ssh.md).

## Using it

1. Press **Show** next to **Choose a screen or window…**. Your desktop's own
   sharing dialog opens on the computer: pick a screen or a window, and allow
   input if it asks (KDE asks for both in one dialog).
2. The picture appears as a panel in the headset. Each pick is another panel;
   **Shared** lists them, each with **Stop**.

KDE can remember the answer: Frame Control keeps the portal's restore token in
its data folder (`desktop-view-restore-token`), so later picks may skip the
dialog. Delete that file to be asked again.

## Needs

- **On the computer:** a desktop with xdg-desktop-portal's ScreenCast and
  RemoteDesktop (KDE Plasma 6, GNOME 46+), the system's `python3` with
  PyGObject, and GStreamer with `pipewiresrc` and an H.264 encoder. Frame
  Control uses `nvh264enc` (NVIDIA), then `vah264enc` (VA-API: AMD, Intel),
  then `openh264enc`, whichever works first; `FRAME_DESKTOP_VIEW_ENCODER`
  picks one. Fedora-family KDE desktops (Nobara, Bazzite) have all of it.
- **On the Frame:** a Chromium for the viewer, as for the Mac: Chromium XR
  (`~/chromium-xr`) or Flathub's `org.chromium.Chromium`.

## How it works

`ui/frame_desktopview.py` is the Linux counterpart of the Mac's Swift helper,
`frame-mac-view`, and speaks the same protocol, so the viewer page
(`ui/mac-view.html`), the SSH reverse tunnel and the panel launch in
`ui/frame_macview.py` serve both. It runs as its own process on the system's
`python3`, since the app's bundled Python has no PyGObject.

| Part | Mac | Linux |
|---|---|---|
| Choosing a source | Frame Control lists windows and screens | Wayland lets no app list windows: the portal's dialog picks one (`POST /pick`) |
| Capture | ScreenCaptureKit | PipeWire, through the portal's ScreenCast |
| Encode | VideoToolbox | GStreamer: NVENC, VA-API or OpenH264 |
| Input | CGEvent (Accessibility) | The portal's RemoteDesktop session, the same one |
| Separate display per window | Yes | No |
| Adapting to a weak link | Steps down frames, then pixels | Not yet: raw frames are dropped while the network is behind, never encoded ones |

## Status

- **Verified 2026-10-03** on Nobara (Plasma 6.7, Wayland, RTX 3090 and
  Raphael iGPU): the agent starts from Frame Control, its tunnel answers from
  the Frame, and the Frame gets the viewer page through it. The test
  pattern streams to a WebSocket client as H.264 (NVENC), scaled to the
  quality setting.
- **Not yet tried:** the portal dialog and input from the headset, and the
  panel itself (the test Frame had no Chromium installed).
