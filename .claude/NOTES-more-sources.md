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
