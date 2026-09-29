#!/bin/sh
# Wraps the agent in "Frame Mac View Lab.app" for testing from a terminal or
# an agent session: macOS then asks for Screen Recording and Accessibility
# for this app rather than for the terminal. Signed with an Apple Development
# identity if there is one, so the permissions survive rebuilds.
#   mac/frame-mac-view/lab.sh            build the app into mac/bin/
#   mac/frame-mac-view/lab.sh serve      also start it (token, port and log in ~/Library/Caches/frame-mac-view-lab,
#                                        readable only by you: the token opens every window and injects input)
#   mac/frame-mac-view/lab.sh serve-only restart it without rebuilding
set -eu
here=$(cd "$(dirname "$0")" && pwd)
app="$here/../bin/Frame Mac View Lab.app"
if [ "${1:-}" != serve-only ]; then
  mkdir -p "$app/Contents/MacOS"
  sh "$here/build.sh" "$app/Contents/MacOS/frame-mac-view" >/dev/null
  cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.saphid.frame-mac-view-lab</string>
<key>CFBundleExecutable</key><string>frame-mac-view</string>
<key>CFBundleName</key><string>Frame Mac View Lab</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1</string>
<key>LSUIElement</key><true/>
<key>NSScreenCaptureUsageDescription</key><string>Tests streaming Mac windows into the Steam Frame.</string>
</dict></plist>
PLIST
  id=$(security find-identity -v -p codesigning 2>/dev/null | awk '/Apple Development/ {print $2; exit}')
  codesign -f -s "${id:--}" "$app" >/dev/null 2>&1
  echo "built $app (signed ${id:-ad hoc})"
fi
if [ "${1:-}" = serve ] || [ "${1:-}" = serve-only ]; then
  pkill -f "Frame Mac View Lab.app/Contents/MacOS/frame-mac-view" 2>/dev/null || true
  dir="$HOME/Library/Caches/frame-mac-view-lab"
  mkdir -p "$dir" && chmod 700 "$dir"
  umask 077
  token=$(openssl rand -hex 16)
  rm -f "$dir/token" "$dir/port" "$dir/log"
  echo "$token" > "$dir/token"
  : > "$dir/log"
  # Experiment switches pass through: FRAME_MAC_VIEW_ENCODER (Encoder.swift),
  # _VD_HZ (Separate.swift), _ADAPT (Controller.swift), _WARM (main.swift).
  open -n "$app" --env "FRAME_MAC_VIEW_TOKEN=$token" --env "FRAME_MAC_VIEW_ENCODER=${FRAME_MAC_VIEW_ENCODER:-}" --env "FRAME_MAC_VIEW_VD_HZ=${FRAME_MAC_VIEW_VD_HZ:-}" \
    --env "FRAME_MAC_VIEW_ADAPT=${FRAME_MAC_VIEW_ADAPT:-}" --env "FRAME_MAC_VIEW_WARM=${FRAME_MAC_VIEW_WARM:-}" --stdout "$dir/log" --stderr "$dir/log" \
    --args serve --port 0 --page "$here/../../ui/mac-view.html"
  for _ in 1 2 3 4 5 6 7 8 9 10; do grep -q listening "$dir/log" && break; sleep 0.3; done
  sed -n 's/.*127\.0\.0\.1:\([0-9]*\).*/\1/p' "$dir/log" | head -n 1 > "$dir/port"
  echo "lab on 127.0.0.1:$(cat "$dir/port")"
fi
