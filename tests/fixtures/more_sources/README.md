Recorded 2026-09-28 from public publisher endpoints using FrameControl/0.1 or
`gh api`. JSON fixtures are reduced to fields consumed by the adapters; API
values are unchanged. No token, cookies or signed download URL is included.

- `*-releases.json`: `/repos/{repo}/releases?per_page=10`, first two releases,
  for KhronosGroup/OpenXR-SDK-Source, icosa-foundation/open-brush and
  SgtBilko76/SuperTux-3D (one release).
- `topic.json`: `/search/repositories?q=topic:openxr+archived:false&sort=stars&per_page=3`.
- `itch-feed.txt`: `https://itch.io/games/free/platform-android/tag-openxr.xml`.
- `itch-robots.txt`: `https://itch.io/robots.txt`.
- `itch-author-robots.txt`: `https://godotvr.itch.io/robots.txt`.

The synthetic ZIP in tests is only a transport/integrity fixture, not an
installable APK. Actual APK parsing was verified separately on the downloaded
Khronos Vulkan sample; see docs/apk-sources.md.

Artwork follow-up: `topic.json` now retains `owner.avatar_url` from authenticated
repository API reads. `artwork-check.json` records HTTPS response status,
Content-Type and image magic checks for all curated URLs and three topic
results. All curated URLs returned real images; LWJGL's social preview returned
HTTP 429. This fixture is evidence of a point-in-time check, not an uptime test.
Open Brush screenshots came from the Steam appdetails response for app 1634870,
linked by its README. SuperTux's README links the recorded upstream screenshot.
