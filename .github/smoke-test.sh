#!/usr/bin/env bash
# Boot the built image and check it actually comes up.
#
# A successful docker build says nothing about whether the container starts: base
# image updates, apt/keyring changes and startup script edits have all broken first
# boot while building cleanly. This starts the image the way a user would, waits for
# the parts that must work, and fails the build if they do not.
#
# It does NOT sign in to Backblaze or run a backup - there are no credentials in CI.
# It checks the container's own scaffolding: the supervisor comes up, Wine builds a
# prefix and reports Windows 10, the drive mapping reaches into the prefix, and the
# bundled tools are present and runnable.
#
# Checks are deliberately deterministic. Wine is noisy on startup, so this asserts
# specific things are true rather than grepping the log for anything error-shaped,
# which would turn normal Wine chatter into red builds.
set -euo pipefail

IMAGE="${1:?usage: smoke-test.sh <image>}"
NAME="bb-smoke-$$"
CONFIG="$(mktemp -d)"
DRIVE="$(mktemp -d)"
TIMEOUT="${SMOKE_TIMEOUT:-420}"

cleanup() {
    echo "--- container log (tail) ---"
    docker logs "$NAME" 2>&1 | tail -60 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    sudo rm -rf "$CONFIG" "$DRIVE" 2>/dev/null || rm -rf "$CONFIG" "$DRIVE" 2>/dev/null || true
}
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

echo "hello from the smoke test" > "$DRIVE/canary.txt"

echo "== starting $IMAGE =="
docker run -d --name "$NAME" \
    -v "$CONFIG:/config" \
    -v "$DRIVE:/drive_d" \
    -e DISABLE_AUTOUPDATE=true \
    "$IMAGE" >/dev/null

# Wait for the Wine prefix to be built and reporting Windows 10. This is the slowest
# part of first boot and the step most likely to break, so everything else is checked
# only once it succeeds.
echo "== waiting for the Wine prefix (up to ${TIMEOUT}s) =="
deadline=$(( SECONDS + TIMEOUT ))
until docker logs "$NAME" 2>&1 | grep -q "prefix ready and reporting Windows 10"; do
    [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] \
        || fail "container exited during startup"
    [ "$SECONDS" -lt "$deadline" ] || fail "timed out waiting for the Wine prefix"
    sleep 5
done
echo "   prefix ready"

echo "== checking the drive mapping =="
docker exec "$NAME" test -e "/config/wine/dosdevices/d:" \
    || fail "/drive_d was not mapped to a Wine drive letter"
docker exec "$NAME" sh -c 'cat "/config/wine/dosdevices/d:/canary.txt"' | grep -q "hello from the smoke test" \
    || fail "mapped drive is not readable through the Wine prefix"

echo "== checking the bundled tools =="
docker exec "$NAME" python3 -c "import curses" \
    || fail "python3/curses missing - bb-monitor cannot run"
for tool in bb-monitor bb-health bb-watchdog bb-version bb-doctor bb-report; do
    docker exec "$NAME" test -x "/usr/local/bin/$tool" || fail "$tool missing or not executable"
done
docker exec "$NAME" test -x /etc/services.d/bb-watchdog/run \
    || fail "watchdog service run script missing or not executable"

echo "== checking the diagnostic tools run =="
docker exec "$NAME" bb-report --list | grep -q "would collect" \
    || fail "bb-report --list did not describe what it collects"
docker exec "$NAME" sh -c 'bb-doctor >/dev/null 2>&1; [ $? -le 1 ]' \
    || fail "bb-doctor crashed (exit >1)"

echo "== checking the build stamp =="
# test -s catches the stamp file itself missing; the grep anchors on "built=",
# which the tools' no-stamp fallback text never contains - anchoring on "version="
# would match the fallback too and pass on an image whose stamp step vanished.
docker exec "$NAME" test -s /etc/bb-build \
    || fail "/etc/bb-build is missing or empty - the build stamp step was lost"
for tool in bb-monitor bb-health bb-version bb-watchdog bb-doctor bb-report; do
    docker exec "$NAME" "$tool" --version | grep -q "^built=" \
        || fail "$tool --version did not report the build stamp"
done

echo "== checking health reporting =="
status="$(docker exec "$NAME" bb-health)" \
    || fail "bb-health exited non-zero on a freshly started container: ${status:-}"
[ "$status" = "OK" ] || fail "bb-health should report OK on a fresh container, got: $status"

# The watchdog service must start (so it is there when someone enables it) but stay
# dormant unless asked for. The state file proves both: that the service ran at all,
# and which branch it took. Asserting on the container log instead would be testing
# the supervisor's log forwarding rather than the watchdog.
echo "== checking the watchdog started and is dormant by default =="
state="$(docker exec "$NAME" cat /tmp/.bb-watchdog-state 2>/dev/null || true)"
[ -n "$state" ] || fail "watchdog service never ran (no state file) - it would not start when enabled either"
[ "$state" = "disabled" ] || fail "watchdog should be dormant without ENABLE_WATCHDOG, state file says: $state"

# Belt and braces: no watchdog process should exist either. The bracketed character
# stops the pattern matching this very command's own /proc entry.
if docker exec "$NAME" sh -c 'grep -l "bin/b[b]-watchdog" /proc/[0-9]*/cmdline 2>/dev/null | head -1' | grep -q .; then
    fail "a bb-watchdog process is running despite ENABLE_WATCHDOG not being set"
fi

# Docker's own health state must not be failing. It may legitimately still read
# "starting" here: the first check only runs after the healthcheck interval, so this
# asserts it is not unhealthy rather than waiting minutes for the first probe.
state="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo none)"
[ "$state" != "unhealthy" ] || fail "container reported unhealthy"
echo "   docker health state: $state"

echo "SMOKE PASS: $IMAGE"
