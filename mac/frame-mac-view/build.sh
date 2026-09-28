#!/bin/sh
# Builds the Mac streaming agent into mac/bin/frame-mac-view (arm64, macOS 14+).
# Frame Control runs it for "Mac in the headset"; the Electron app bundles it.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/../bin/frame-mac-view}
mkdir -p "$(dirname "$out")"
xcrun swiftc -O -swift-version 5 -target arm64-apple-macos14.0 \
  -o "$out" "$here"/Sources/*.swift
echo "built $out"
