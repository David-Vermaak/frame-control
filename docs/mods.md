# Flat-to-VR mods and Beat Saber songs

**Status: feasibility work, not an installer.** Frame Control does not yet
manage mods or Beat Saber songs. [Issue #26](https://github.com/saphid/frame-control/issues/26)
stays open: neither UEVR injection nor Beat Saber custom-song playback has
been verified on this Frame. Alex has an owned copy on a Quest 2; that copy
has not been inspected. There is no Mods button until the underlying
install, playback and removal have been checked.

The mod manager must be Frame Control's own implementation. Mods and songs
are permitted third-party content; BSManager, ModsBeforeFriday, MO2 and other
managers must not be dependencies. Steam, Proton and SteamVR remain platform
dependencies. No game purchases, entitlement bypasses, withdrawn builds or
unofficial mod mirrors are part of this work.

## Per-game support

Checked 2026-09-28 on SteamOS **0.4.1**, BUILD_ID **20260925.6191901**, aarch64,
with **Proton 11.0-2c ARM64** and SteamVR **2.18.1**. “Verified” describes only
the observation stated, not a promise that the game is playable. “Documented”
means an upstream source describes it; “inferred” means it still needs a test.

| Game / build | Mod or content | Evidence and support status | Next check |
|---|---|---|---|
| Half-Life 2: VR Mod – Episode One, Steam 2177750, build 25413453 | Official Steam community mod, shared base depot 658920 build 25413418 | **Verified: startup only.** Already installed; launched through Proton ARM64. The stereo headset capture showed its first-time setup, and SteamVR loaded `bindings_frame.json`. Gameplay, controller interaction, fresh installation and removal are unverified. | Complete first-time setup and play a level before offering a tested install shortcut. |
| Gravitas, Steam 1067310, Windows | UEVR 1.05 | **Verified: prerequisites and windows only.** Free Steam install completed. The game produced a `SkyArk (64-bit, PCD3D_SM5)` window. UEVR needed .NET; with official .NET 6.0.36 libraries it produced a `UEVR` window. A combined run exited 1 with X11 errors before injection was verified. **Inferred: compatibility remains unknown**, not proven broken. | Retry during a stable headset session; verify injection, stereo scene output, controls and removal. |
| Beat Saber, Steam 620980, Windows / Proton | Basic custom songs; later SongCore and version-matched mods | **Verified: absent from the 868-game library returned by this Frame.** Store metadata lists Windows, not Linux. **Documented:** the PC game reads basic maps from `Beat Saber_Data/CustomLevels` without a mod manager. Playback on Frame is unverified. | An already-owned, legitimately installed copy is required. Do not buy it as part of this task. |
| Beat Saber, claimed native ARM64 build | Custom songs / native mods | **Inferred: unverified.** The research mentions this build but supplies no verified official distributable or tested layout. CPU architecture alone does not identify Android versus Linux, the game version or the mod ABI. | Establish official provenance, ownership, binary type and version before touching files. Do not apply Quest patches to an unidentified build. |
| Beat Saber, Alex's Quest 2 copy | Custom songs / Android mods | **Documented: owner-reported copy on Quest 2**, currently charging. No APK, version or installed mods inspected; no Frame playback verified. This does not establish ownership of the Steam build. | When the Quest is available, inspect the owned copy's version and supported transfer path, then test Lepton/OpenXR compatibility without bypassing entitlement checks. |
| Hogwarts Legacy, Steam 990080 | R.E.A.L. | **Verified: listed in this Frame's owned library, not installed.** Official release access and redistribution permission were not established; the referenced author Patreon page returned HTTP 403. No archive downloaded or game tested. | Obtain a current free release from the author and confirm its terms before any test. A news report saying “free” is not a redistribution grant. |
| Horizon Zero Dawn, Steam 1151640; Horizon Forbidden West, Steam 2420110 | R.E.A.L. | **Verified: both listed as owned, neither installed.** Same source/permission blocker as above; runtime support is unverified. | Check each game's supported version against an accessible official release. |
| Half-Life 2 VR / other OpenVR games | OpenComposite, per-game replacement | **Documented:** forwards OpenVR calls to OpenXR. **Inferred: Frame compatibility unknown.** Not installed or tested. HL2 VR reached setup with the shipped OpenVR path already. | Test a specific game and replacement DLL only if needed; preserve its original DLL. Never switch the shared headset's runtime globally. |
| Doom / Quake / Half-Life Team Beef ports | Author's VR ports plus separately owned or free game data | **Inferred: untested.** Android ARM64 support does not establish OpenXR extension or controller compatibility on Lepton. | Choose an official release and legally usable data set, then test that exact port. |
| Skyrim VR, Steam 611670 | SKSEVR / HIGGS / PLANCK stack | **Verified: Skyrim VR is absent from this library.** Owning flat Skyrim or Special Edition is not the VR game's entitlement. Runtime and mod support are unverified. | An already-owned VR copy and version-matched official mod releases are required. |

The [test record](evidence/mods-2026-09-28.md) distinguishes process startup,
visible output and failures. It also records a SteamVR restart during the
shared session, which prevents attributing the failed UEVR attempt to FEX.

## Beat Saber: songs first

**Documented:** the [BSMG PC guide](https://bsmg.wiki/pc-modding.html)
describes extracting each map into its own directory below
`Beat Saber/Beat Saber_Data/CustomLevels`. Basic custom songs do not require
SongCore; maps that require mod features need their matching dependencies.
This is a candidate for our own file manager, not a verified Frame feature.

Alex's Quest 2 copy is a separate Android candidate. Its ownership does not
make the PC `CustomLevels` layout applicable. Until the actual build is
inspected, neither a direct song-copy recipe nor APK patching is justified.

Only maps whose music and chart are permitted for distribution may be used
as test fixtures or bundled content. A public download alone does not establish
those rights. Start with an original or explicitly licensed basic map.

**Documented:** [ModsBeforeFriday](https://github.com/Lauriethefish/ModsBeforeFriday)
targets Quest Beat Saber over WebUSB/ADB. It is not a generic native ARM64
modding protocol. [BSManager](https://github.com/Zagrios/bs-manager/releases/tag/v1.6.0)
publishes an aarch64 Flatpak, but its architecture says nothing about Beat
Saber or its plugins running on Frame. Neither app is an installation step
or dependency for Frame Control.

## Requirements for our manager

These are **planned**, not implemented or verified:

1. Resolve the selected Steam game's real library, installed build, executable
   architecture and Proton prefix. Confirm ownership through Steam; a directory
   or app manifest alone is not proof. Keep downloading, installed and playable
   as separate states.
2. Download a pinned mod version from the author's official release. Record
   the URL, version, license and digest. Verify the published digest when
   available; an upstream SHA-256 detects corruption but is not a signature.
   Do not treat “free to download” as permission to redistribute.
3. Stage and validate archives before writing into the game or prefix. Reject
   path traversal, links escaping the destination, archive bombs and unexpected
   executable content in song packs. Check song metadata and its referenced
   files, not just the `.zip` suffix.
4. Refuse changes while the game is running. Back up originals and journal
   every managed file and digest. Apply changes atomically where possible and
   roll back partial failures. Keep runtime prerequisites scoped to this game.
5. Uninstall only files still matching our receipt; restore originals without
   overwriting later user edits. Preserve saves, unrelated mods and songs.
   Song removal must target one managed map, never the whole CustomLevels tree.
6. Expose one-click actions beside the game only after real-Frame install,
   playback and uninstall pass. Test filesystem and download failure handling
   with fake-Frame fixtures; those cannot prove FEX injection or VR rendering.

## Sources

- [UEVR 1.05 official release](https://github.com/praydog/UEVR/releases/tag/1.05)
  and [author's usage instructions](https://github.com/praydog/UEVR#getting-started).
- [Microsoft .NET 6 release metadata](https://builds.dotnet.microsoft.com/dotnet/release-metadata/6.0/releases.json),
  including the SHA-512 hashes used for the test runtimes.
- [OpenComposite's OpenXR branch](https://gitlab.com/znixian/OpenOVR/-/tree/openxr),
  including per-game installation and the need to preserve original DLLs.
- [R.E.A.L. author post referenced by the research](https://www.patreon.com/realvr/posts/but-wheres-link-165840151)
  (HTTP 403 from this environment; contents not verified).
- [Half-Life 2 VR official site](https://halflife2vr.com/) and
  [Episode One on Steam](https://store.steampowered.com/app/2177750/).
- [Beat Saber store metadata](https://store.steampowered.com/api/appdetails?appids=620980).
