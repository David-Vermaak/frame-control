#!/usr/bin/env zsh
# Linux host with Docker: run the end-to-end tests against the fake Frame.
# Builds the fake Frame and host images (tests/fakeframe), starts them with
# docker compose, runs tests/e2e in the host container, prints the results
# and takes everything down again. Exits with the tests' status (2 if the
# harness itself didn't come up). See docs/testing.md.
#
# Usage: scripts/e2e.sh [TEST...]     e.g. scripts/e2e.sh test_titles test_faults.Faults.test_disk_full
# Env:   FAKEFRAME_BASE   base image (default: archlinux:base, or menci/archlinuxarm:base on arm64)
#        FAKEFRAME_KEEP=1 leave the containers running afterwards
set -uo pipefail

root=${0:A:h:h}
cd "$root" || exit 2
case $(uname -m) in
  x86_64|amd64) base=archlinux:base ;;
  aarch64|arm64) base=menci/archlinuxarm:base ;;
  *) print -u2 "No Arch Linux base image known for $(uname -m); set FAKEFRAME_BASE"; exit 2 ;;
esac
base=${FAKEFRAME_BASE:-$base}
compose=(docker compose -p fakeframe-e2e -f tests/fakeframe/compose.yaml)
started=$SECONDS

print "==> Building fakeframe-frame (from $base) and fakeframe-host"
docker build -q --build-arg BASE="$base" -t fakeframe-frame -f tests/fakeframe/Containerfile tests/fakeframe || exit 2
docker build -q -t fakeframe-host -f tests/fakeframe/host.Containerfile tests/fakeframe || exit 2

logs() {
  print "\n==> Fake Frame logs"
  $compose logs --no-color --tail 80 fakeframe
  $compose exec -T fakeframe sh -c 'for f in /var/log/fakeframe/*.log; do echo "--- $f"; tail -n 40 "$f"; done' 2>/dev/null
  print "\n==> ui/server.py log"
  $compose exec -T host sh -c 'tail -n 80 /tmp/fakeframe-e2e-server.log' 2>/dev/null
}

finish() {
  if [[ ${FAKEFRAME_KEEP:-0} == 1 ]]; then
    print "==> Left running: ${(j: :)compose} exec host bash"
  else
    $compose down -v --remove-orphans >/dev/null 2>&1
  fi
}
trap finish EXIT

$compose down -v --remove-orphans >/dev/null 2>&1
print "==> Starting the fake Frame and the host"
if ! $compose up -d --wait; then
  logs
  exit 2
fi
print "==> Up after $(( SECONDS - started )) s; running tests/e2e"

if (( $# )); then
  args=(-v "$@")
else
  args=(discover -v -s .)
fi
tests_started=$SECONDS
$compose exec -T -w /repo/tests/e2e host python3 -m unittest "${args[@]}"
rc=$?
(( rc == 0 )) || logs
print "\n==> tests/e2e: $([[ $rc == 0 ]] && echo passed || echo "FAILED (exit $rc)") in $(( SECONDS - tests_started )) s" \
      "($(( SECONDS - started )) s with builds)"
exit $rc
