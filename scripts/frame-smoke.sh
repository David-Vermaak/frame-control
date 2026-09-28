#!/usr/bin/env zsh
# Mac or Linux: the headset smoke test. Installs, launches and removes tiny
# test titles on the Frame (the `frame` alias) and records the results with
# its BUILD_ID under tests/smoke/results/. See docs/testing.md.
#
# Usage: scripts/frame-smoke.sh [--pair]   (--pair needs you in the headset to approve)
set -euo pipefail
exec python3 "${0:A:h:h}/tests/smoke/frame_smoke.py" "$@"
