# User repositories

Scope: ui/apk_sources/fdroid.py, fixture tests and docs/apk-repos.md. No headset access.
No delegation or independent reviewer launched: task explicitly forbids delegation;
parent integrates and reviews. Existing catalogue API stays unchanged.

Decisions:
- Support F-Droid signed v2 and v1, HTTPS only, RSA PKCS#1 CMS/JAR verification.
- Pin operator certificate fingerprints. Without a supplied fingerprint, verify
  the complete signature chain of hashes on first use, then persist that signer.
- Reuse frame_catalog._IndexReader and _reduce_index; keep authenticated source
  cache separate from legacy unauthenticated catalogue cache to avoid laundering trust.
- Sources list compatible builds (Android <=30, arm64 or no native code), as
  existing catalogue reducer does. This is compatibility filtering, not a runtime guarantee.
- No Obtainium export import or new unsigned JSON format in this source.

Research: downloaded F-Droid API/setup docs, Obtainium and SideQuest READMEs,
Izzy repo page and entry.jar via HTTPS. Izzy's published fingerprint matched
pure-Python CMS validation: 3BF0D6ABFEAE2F401707B6D966BE743BF0EEE49C2561B9BA39073711F628937A.

## Final verification

All commands below ran in /Users/saphid/projects/steam-frame-userrepo on user-repos.

- `python3 --version`: Python 3.9.6.
- `python3 -m unittest discover -s tests -p test_fdroid_sources.py`: initially
  13 tests, OK, exit 0. Added a cache-corruption test afterward.
- Final `python3 -m unittest discover -s tests`: 180 tests in 6.866s, OK,
  exit 0 (includes 14 source tests and existing catalogue/version tests).
- `python3 ui/apk_sources/fdroid.py add 'https://apt.izzysoft.de/fdroid/repo?fingerprint=3BF0D6ABFEAE2F401707B6D966BE743BF0EEE49C2561B9BA39073711F628937A' --name 'IzzyOnDroid verification'`: exit 0;
  saved fdroid-user-57e98c13877f14fdea65 with the published pin.
- `python3 ui/apk_sources/fdroid.py search fdroid-user-57e98c13877f14fdea65 'tinymusicplayer'`:
  exit 0; com.martinmimigames.tinymusicplayer, version 1.3 / code 4,
  GPL-3.0-only, 16,520 bytes.
- `python3 ui/apk_sources/fdroid.py download fdroid-user-57e98c13877f14fdea65 com.martinmimigames.tinymusicplayer`:
  exit 0, verified true. Independent hashlib readback matched
  d7bcb24d101b04beb3394b695b24be4e2c3d6ed702f1d0e06bc4dd707f64d86a.
- Direct `_fetch` + `_jar` calls on F-Droid main/archive entry.jar: exit 0;
  both matched the published 43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab pin.
- `.claude/user-repos-proof.json` retains live CLI results and APK readback.
  Live APK remains in the per-user apk-sources cache; the added source remains
  in the per-user apk-repos.json, as requested for the real add/search/download run.

Not verified: headset installation/runtime, native UI/server integration (sibling
worker), real v1-only server (offline signed fixture covers fallback), executing
the fdroidserver publishing instructions, full F-Droid main/archive index/APK
downloads (their live signed entry jars were checked). No independent reviewer
was run because this task forbids delegation and assigns review to the parent.

Known limits / follow-up questions: only one RSA-2048–8192 JAR signer supported;
no ECDSA/DSA/PSS or section-only SF signatures; no index timestamp rollback or
expiry policy, automated key rotation or cross-process settings-write locking.
The settings API serializes threads and publishes atomically. Should a later
change add explicit rollback policy and broader JAR algorithms? Parent may
choose UI wording for TOFU; this source already returns trust_on_first_use.
