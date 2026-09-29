# Store preview artwork

Recorded public artwork for offline UI verification, fetched 2026-09-28.
`urls.json` records each original URL. No unit test downloads these files.

- Open Brush banner, icon and screenshots: Icosa Foundation's public
  `icosa-foundation/openbrush.app` website assets. Artwork remains credited to
  its creators; used here to preview the Open Brush listing.
- Mindustry, AntennaPod and NewPipe icons/screenshots: their public F-Droid
  listings. Corresponding projects use GPL licences; these images represent
  those same apps in the store preview.
- Luanti, SuperTuxKart and other social previews: public GitHub-generated
  repository preview images. Project names/logos belong to their owners.

`../store.json` contains illustrative listing metadata, including mock package
names, popularity, dates, version/size and compatibility fields. It is not a
catalogue or evidence that a particular release works on the Frame. `_demo.py`
is opt-in and cannot download APKs. `tests/search_preview.py` preloads these
recordings into the image cache and simulates installation without a headset.
