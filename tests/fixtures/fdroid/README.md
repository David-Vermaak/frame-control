# F-Droid verification fixtures

`entry.jar` and `index-v1.jar` are synthetic RSA-2048/SHA-256 signed JARs,
including CMS signed attributes. `fingerprint.txt` identifies their throwaway
certificate. Their JSON describes org.example.app; `example.apk` is deliberately
plain test data, not an installable app. The v2 index includes incompatible
Android-31 and x86-only versions to exercise the shared reducer.

`izzy-entry.jar` was recorded from
https://apt.izzysoft.de/fdroid/repo/entry.jar on 2026-09-28. Its certificate
fingerprint matches the operator's published fingerprint:
3BF0D6ABFEAE2F401707B6D966BE743BF0EEE49C2561B9BA39073711F628937A.
It exercises an independent production JAR/CMS encoder without network access.
The index it references is not needed by this signature-only fixture test.

`artwork-v1.json` and `artwork-v2.json` are unsigned metadata/reducer fixtures
based on the synthetic indexes above. They exercise en-US preference, per-field
locale fallback, v1 artwork paths, phone/tablet ordering, the six-image cap,
author names and HTML/multiline summaries. The signed integrity fixtures remain
unchanged; artwork tests feed these JSON files directly through the reducer and
then round-trip the resulting entries through the source cache.
