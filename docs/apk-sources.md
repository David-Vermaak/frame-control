# Developer-consented APK sources

Surveyed 2026-09-28. Free access is not proof of redistribution permission or
Frame compatibility. These adapters fetch only public publisher releases or
link to publisher pages. They do not acquire store entitlements, defeat access
checks, install anything, or rehost APKs. See [VR compatibility](vr-apks.md).

| Source | Developer consent and automated-access position | API/feed; VR coverage | Decision |
|---|---|---|---|
| [itch.io](https://itch.io/docs/legal/terms) | Publishers warrant distribution rights (§4). Users may access content through the service; this is not blanket scraping permission. Main robots excludes `/game/download/`; author subdomains exclude `/*/download/`. No challenge bypass. | Public free Android RSS for `openxr` and `oculus-quest`; substantial indie VR. Server API is mostly authenticated publisher/account functionality, not a general anonymous store-download API. | Implement RSS search, artwork and page links; `downloadable: False`. The supplied free-download script follows keyed download pages excluded by robots, so it is not shipped. |
| [GitHub releases](https://docs.github.com/en/rest/releases/releases) | Maintainers publish assets; curated repositories below establish provenance. Public hosting or an open-source topic alone does not establish rights to every uploaded binary. Use supported REST API under [API terms](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#h-api-terms), not HTML crawling. | Releases API includes APK assets and sometimes SHA-256. Topic search finds OpenXR/Quest projects. 60 unauthenticated requests/hour; authenticated user limits are generally 5,000/hour, with separate search/secondary limits. | Implement curated downloads and explicit topic discovery. Unreviewed topic results are page-only. |
| [Uptodown](https://www.uptodown.com/aboutus) | Developer distribution program exists, but that does not prove publisher authorization for every catalog item. [Privacy policy](https://www.uptodown.com/aboutus/privacy) explicitly describes protection against automated access. General automation permission was not established. | Broad Android catalog, limited VR focus; no supported public consumer-download API established in this survey. | Page links only; no downloader. Do not infer consent from an unchanged APK signature. |
| [APKPure](https://apkpure.com/terms) | Third-party APK catalog; individual publisher consent and automation rights were not established. Terms request returned HTTP 403; no bypass attempted. | Broad Android coverage, incidental VR; internal endpoints are not permission to automate. | Exclude automatic indexing/downloading; user may open site. |
| [APKMirror](https://www.apkmirror.com/faq/) | Publisher-signed files and a free-app policy are not a blanket developer-consent or automation grant. FAQ request returned HTTP 403, so current terms could not be confirmed. | General Android/version archive; APK bundles often need another installer; little VR focus. No supported consumer-download API established. | Page links only, no scraping or bundle conversion. |
| [Aptoide](https://en.aptoide.com/company/legal) | Terms define an app supplier as developer, owner or authorized distributor; user stores still require per-item provenance. API availability alone does not settle third-party access rights. | API ecosystem and general Android catalog; weak VR focus. | Defer until a publisher-owned store and its API terms can be approved. No blanket community-store downloader. |
| [Amazon Appstore](https://developer.amazon.com/docs/app-submission/understanding-submission.html) | Official developer submissions; store account, device and license rules apply. Publisher submission APIs do not authorize public binary extraction. | Fire-device distribution; Android-device Appstore support ended in 2025; little Quest relevance. | Official product links only; no account or entitlement extraction. |
| [PICO / ByteDance store](https://developer.picoxr.com/document/distribute) | Official publisher channel with store/device entitlements. No public unauthenticated binary-download grant established; documentation request encountered a redirect error. | Strong standalone VR; PICO builds may depend on PICO services/extensions. | Store links only. A developer's independently published GitHub/itch build can qualify separately. |
| [Meta Horizon Store / former App Lab](https://www.meta.com/experiences/) | Official developer submissions. A free store entitlement is still an entitlement; no license bypass or authenticated store extraction. App Lab was folded into the main store in 2024. | Strongest Quest coverage; no supported anonymous APK-download API established. | Store links only; independently distributed free builds use their publisher source. |
| [Khronos samples](https://github.com/KhronosGroup/OpenXR-SDK-Source) | Official upstream, Apache-2.0 sample; developer-published release APKs. GitHub API terms apply. | `hello_xr` Vulkan/OpenGL ES APKs; excellent OpenXR diagnostics. | Included in GitHub curated list, Vulkan variant selected. |
| [Meta OpenXR samples](https://github.com/meta-quest/Meta-OpenXR-SDK) | Official upstream; check each sample's license. Source availability does not imply a published APK, and some samples require Meta extensions/services. | Source/build examples, inconsistent ready-made APK releases. | Link to upstream; add specific free APKs only after release/provenance review. |
| [Godot XR demos](https://github.com/GodotVR/godot-xr-tools) | Official project source and publisher demo pages; licenses and dependencies vary by demo. | OpenXR examples on GitHub/itch. Older Godot builds can fail on Lepton's missing clipboard service. | Covered by source discovery; no compatibility promise from an OpenXR tag. |

The table distinguishes observed restrictions from unknown permission. An
unverified policy is a reason to defer automation, not a claim that a site is
unlawful. Only the two implemented source kinds are registered by their own
`sources()` functions; the other rows are recommendations, not new UI entries.

## Adapters

`ui/apk_sources/github.py` uses `github_curated.json`: Khronos `hello_xr`,
[Open Brush](https://github.com/icosa-foundation/open-brush), and
[SuperTux 3D](https://github.com/SgtBilko76/SuperTux-3D). These have official
OpenXR project/release evidence, not a blanket claim of headset compatibility.
Open Brush's compatibility evidence is recorded in [vr-apks.md](vr-apks.md).
Open Brush and SuperTux publish the selected builds as prereleases; curated
opt-ins preserve that label in version records. Exact APK filename patterns
avoid downloading desktop archives or alternate non-Quest builds.
[OpenSaberPlus](https://github.com/arpruss/OpenSaberPlus) was examined but not
curated: GitHub reports its license as `NOASSERTION`, and current OpenXR APK
provenance was not established in this pass.

Default GitHub search is offline against this small list. Queries
`topic:openxr`, `topic:oculus-quest`, and `topic:quest` explicitly call repository
search. Results outside the curated list stay page-only, even if a repository
claims an open-source license. This prevents an arbitrary tagged mirror from
becoming a trusted downloader. Extend the curated JSON after provenance review.

Set optional `FRAME_GITHUB_TOKEN` in the process environment for a higher API
quota. Tokens are sent only to `api.github.com`, never written to the cache,
never sent to asset hosts, and removed on redirects. The adapter does not
read `gh` credentials automatically. Metadata is cached for one hour under
`frame_host.cache_dir('apk-sources', 'publisher')`. A cold details request
fetches at most ten releases. Rate-limit errors are surfaced without retry
loops. Asset IDs and release tags are not Android version codes: metadata
leaves the latter unknown and rejects a requested `version_code` rather than
silently fetching a different build.

`itch.py` exposes separate OpenXR and Quest feed sources, so one feed's failure
does not suppress the other at the aggregator level. Queries filter the current
feed window locally: this is not an exhaustive historical itch search. Only
explicit zero-price Android entries are returned. Covers are exposed in
`images`; absent screenshots, APK version, ABI and minimum SDK stay unknown.
GitHub's release metadata has no standard app artwork field, so artwork stays
empty rather than presenting a repository-owner avatar as an app icon. VR is
based on curated evidence or a VR-specific feed/topic, not a compatibility claim.

Downloads stream to unique temporary files, require an APK manifest entry,
restrict HTTPS origins and redirects, and enforce a 2 GiB ceiling. `verified`
means the downloaded SHA-256 matches GitHub's published digest. Without such a
digest, the computed SHA-256 is returned with `verified: False`; neither value
claims publisher-signature validation. Installation must inspect the APK as
usual. OBBs, split APKs, paid assets and external release-body download links
are unsupported.

## Evidence and limits

On this Mac, Python 3.9 downloaded the real Khronos Vulkan 1.1.63 APK through
the GitHub adapter, matched its published SHA-256
`f24bbe8ba6f6339fca658628868ba8189cbc33390d6ac508f69d76fb67b5fa34`, and
`python3 ui/frame_android.py info <apk>` exited 0: package
`org.khronos.openxr.hello_xr.vulkan`, version code 1063, minimum API 24,
arm64-v8a present, OpenXR detected. No Frame connection or installation occurred.

The itch OpenXR RSS was fetched successfully and recorded as a fixture.
Subsequent live adapter search encountered HTTP 429; it is not claimed as a
successful live end-to-end search. Fixture search finds Off Nominal and parses
nine Android entries from the ten-item feed (one has only an HTML platform).
A real itch download and APK inspection were deliberately not performed:
robots restrictions take precedence over that requested proof. No current
policy text is claimed verified where the table records failed access.

Tests use recorded, reduced API/RSS fixtures with network access blocked in
the new test class. They cover selection, prereleases, unknown topic results,
paid/non-Android exclusion, URL restrictions, redirect credential removal,
caching, rate limits, checksum mismatch, non-APK rejection and partial-file
cleanup. See `.claude/NOTES-more-sources.md` for commands and local evidence.
