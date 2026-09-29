#!/bin/sh
# Publish a tested draft release so running copies of Frame Control offer it
# (docs/releasing.md). Checks every installer is attached with a SHA-256
# digest first, since the app's updater refuses assets without one, then
# attaches update.json, the manifest the updater reads.
# Usage: scripts/publish-release.sh v0.4.0
set -eu
tag="${1:?usage: $0 vX.Y.Z}"
repo=saphid/frame-control
expected="Frame-Control-mac-arm64.dmg Frame-Control-mac-arm64.zip Frame-Control-Setup-x64.exe
Frame-Control-win-x64.zip Frame-Control-linux-x86_64.AppImage Frame-Control-linux-arm64.AppImage
Frame-Control-linux-amd64.deb Frame-Control-linux-arm64.deb"

info=$(gh release view "$tag" -R "$repo" --json isDraft,isPrerelease,assets)
version=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$(dirname "$0")/../app/package.json")
[ "v$version" = "$tag" ] || echo "note: app/package.json here says $version (the release was built from the tag)"

missing=""
for name in $expected; do
  digest=$(printf '%s' "$info" | python3 -c 'import json,sys
d=json.load(sys.stdin); n=sys.argv[1]
print(next((a.get("digest") or "" for a in d["assets"] if a["name"]==n), "absent"))' "$name")
  case "$digest" in
    sha256:*) echo "ok       $name" ;;
    absent) echo "MISSING  $name"; missing=1 ;;
    *) echo "NO HASH  $name"; missing=1 ;;
  esac
done
[ -z "$missing" ] || { echo "not publishing: fix the assets above" >&2; exit 1; }

# update.json: what running copies read (app/updater.js), from github.com's
# latest/download link rather than the rate-limited REST API.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
gh release view "$tag" -R "$repo" --json tagName,url,body,assets | python3 -c 'import json,sys
d=json.load(sys.stdin)
names=set(sys.argv[1].split())
print(json.dumps({"version": d["tagName"].lstrip("v"), "page": d["url"], "notes": d["body"][:4000],
                  "assets": [{"name": a["name"], "size": a["size"], "digest": a["digest"]}
                             for a in d["assets"] if a["name"] in names]}, indent=1))' "$expected" > "$tmp/update.json"
gh release upload "$tag" -R "$repo" "$tmp/update.json" --clobber
echo "ok       update.json"

gh release edit "$tag" -R "$repo" --draft=false --prerelease=false --latest
echo "published $tag; running copies will offer it at their next check"
