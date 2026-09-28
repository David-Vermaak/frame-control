# more-sources

Scope: only this worktree/branch; no delegation, SSH, installation, push, PR or
issue writes. Parent performs integration and independent review per brief.

Decisions: implement GitHub curated public APK releases and itch.io RSS page
links. itch robots exclude keyed download pages used by /tmp/vrapk/itchdl.py;
no bypass. Topic results are not automatically trusted. Curated Open Brush and
SuperTux prereleases must be explicitly allowed; discovered during fixture tests.
No shared files changed. Default GitHub search makes no network request.

Evidence (2026-09-28):
- Read all six prerequisite files and the full brief.
- Fetched itch terms, root/author robots and OpenXR RSS with FrameControl UA.
- Anonymous GitHub returned 403 rate-limit; authenticated gh API reads succeeded.
- GitHub fixtures record upstream Khronos, Open Brush and SuperTux releases.
- `python3 .claude/prove-more-sources.py`: GitHub search/download succeeded,
  published SHA-256 matched; `python3 ui/frame_android.py info` exited 0.
  Overall script exit 1 because subsequent itch RSS request returned HTTP 429.
  A second invocation had the same outcome; no further live itch retries.
- Download: `.claude/proof-cache/f24bbe8ba6f6339fca658628868ba8189cbc33390d6ac508f69d76fb67b5fa34.apk`.
  Package org.khronos.openxr.hello_xr.vulkan; 1.1.63/1063; API 24; arm64;
  OpenXR. No runtime compatibility claim.
- Initial whole suite: 174 tests, 1 failed (prerelease handling). After explicit
  curated prerelease support: 174 tests passed, exit 0. Final rerun below.

Unverified: live itch search completion (429), itch download/info (disallowed
flow), Open Brush/SuperTux downloads, topic live adapter search (API fixture
capture succeeded), headset runtime, UI integration, independent provider
review (explicit implementation brief prohibits delegation and assigns review
to parent). No claim that any reviewer/model participated.

Research HTML and local downloaded APKs are local evidence only, not committed.
Recorded fixtures retain public source data, not credentials. The proof script
uses gh's token in memory and overrides cache into this worktree.

Final verification:
- Python version: 3.9.6.
- `python3 -m unittest discover -s tests > .claude/tests-more-sources.log 2>&1`:
  174 tests passed in 5.569s, real exit 0.
- `git diff --check`: exit 0.
- Final artifact: docs/apk-sources.md plus two source modules, a private HTTPS
  helper, curated JSON, recorded fixtures and tests. Evidence scripts/logs stay
  in .claude; research HTML and APK cache are explicitly ignored there.

## Artwork follow-up

Added curated icon/images metadata and plain-language summaries. Icons use
publisher repository assets pinned to inspected commits. Open Brush's banner
is its README image; three screenshots are from its README-linked Steam page.
SuperTux's banner/screenshot is the upstream gameplay preview in the port's
README, not a Quest capture. hello_xr has its actual Vulkan launcher icon and
repository social banner; no real screenshot was found in its repository or
README, so screenshots stays empty rather than mislabeling branding.

Topic search uses owner.avatar_url and the requested GitHub social-preview
pattern, retaining curated artwork when a curated app is discovered by topic.
Unknown repository details use GitHub's owner.png avatar endpoint and the same
social banner without an extra API request. Existing itch cover behavior is
unchanged; tests now assert icon/banner equality and empty screenshots because
the recorded RSS provides no separate screenshots.

Validation: all nine distinct curated artwork URLs returned HTTP 200, image
Content-Type and image magic. Of six topic artwork URLs, five returned images
and LWJGL's social preview returned HTTP 429. Recorded in
`tests/fixtures/more_sources/artwork-check.json`; no repeated retry.
`python3 -m unittest discover -s tests`: 176 passed in 5.879s, real exit 0.
`git diff --check`: exit 0. No UI rendering/headset testing or independent
review; parent retains integration/review, and delegation remains prohibited.
