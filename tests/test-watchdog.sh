#!/usr/bin/env bash
# Behavioural test for bb-watchdog's DOWN branch, run against stubs.
#
# The property under test: a DOWN state is left to startapp's service watch
# the first time it is seen, and started by the watchdog itself when it is
# still DOWN one cooldown later. A build of 2026-09-28 broke the watch, and
# two hosts then sat DOWN for hours while the watchdog only reported it.
#
# Run:  bash tests/test-watchdog.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../rootfs/usr/local/bin/bb-watchdog"
FX="$(mktemp -d)"; trap 'rm -rf "$FX"' EXIT
mkdir -p "$FX/bin" "$FX/config/bb-api"
export BB_RECOVERY_LOG="$FX/recovery.log"

# The cooldown counts seconds rather than minutes here, bzserv_up reads a
# marker the fake wine leaves, and the recovery helper is the real one.
sed -e 's#COOLDOWN_MIN \* 60#COOLDOWN_MIN * 1#' \
    -e "s#^bzserv_up() {.*#bzserv_up() { [ -f \"$FX/bzserv-up\" ]; }#" \
    -e "s#^SWITCH=.*#SWITCH=$FX/config/bb-api/watchdog.json#" \
    -e "s#^\. /usr/local/lib/bb-record.sh.*#. $HERE/../rootfs/usr/local/lib/bb-record.sh#" \
    "$SRC" > "$FX/bb-watchdog"
chmod +x "$FX/bb-watchdog"
cat > "$FX/bin/bb-health" <<EOS
#!/bin/sh
cat "$FX/health"
EOS
cat > "$FX/bin/wine" <<EOS
#!/bin/sh
echo "wine \$*" >> "$FX/wine.log"
[ "\$1 \$2" = "net start" ] && [ -f "$FX/start-works" ] && touch "$FX/bzserv-up"
exit 0
EOS
printf '#!/bin/sh\nexec "$@"\n' > "$FX/bin/setsid"
cat > "$FX/bin/timeout" <<'EOS'
#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -k) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift
exec "$@"
EOS
chmod +x "$FX/bin/"*
export PATH="$FX/bin:$PATH"

pass=0; fail=0
ok() { if eval "$1"; then pass=$((pass+1)); echo "PASS $2"; else fail=$((fail+1)); echo "FAIL $2"; fi; }
run_watchdog() {   # seconds to let it run
    WATCHDOG_INTERVAL=1 COOLDOWN_MIN=2 STALL_MIN=1 "$FX/bb-watchdog" > "$FX/out" 2>&1 &
    wd=$!
    sleep "$1"
    kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
}

# 1. DOWN seen once: reported, nothing started.
echo "DOWN the Backblaze service (bzserv) is not running" > "$FX/health"
run_watchdog 2
ok '[ ! -f "$FX/wine.log" ]' "the first DOWN is left to the service watch: no Wine call"
ok 'grep -q "detected: DOWN.*the service watch restarts it" "$BB_RECOVERY_LOG"' "and is recorded as such"

# 2. Still DOWN one cooldown later: the watchdog starts the service and sees it up.
rm -f "$BB_RECOVERY_LOG"
touch "$FX/start-works"
run_watchdog 8
ok 'grep -q "wine net stop bzserv" "$FX/wine.log" && grep -q "wine net start bzserv" "$FX/wine.log"' \
   "the second DOWN runs net stop then net start"
ok 'grep -q "recovered: started bzserv" "$BB_RECOVERY_LOG"' "and records the recovery"
ok '[ "$(grep -c "wine net start" "$FX/wine.log")" -eq 1 ]' "one start, not one per interval"

# 3. The start does not bring it up: say so, and no false recovery line.
rm -f "$FX/wine.log" "$BB_RECOVERY_LOG" "$FX/start-works" "$FX/bzserv-up"
sed -i.bak 's/sleep 5$/sleep 0/' "$FX/bb-watchdog"      # the up-poll, 12 x 5 s, is not what is under test
run_watchdog 8
ok 'grep -q "could NOT start bzserv" "$BB_RECOVERY_LOG"' "a start that does not take is reported as such"
ok '! grep -q "recovered" "$BB_RECOVERY_LOG"' "with no recovery claimed"

# 4. OK in between resets the count: the next DOWN is a first sighting again.
rm -f "$FX/wine.log" "$BB_RECOVERY_LOG"
echo "OK" > "$FX/health"
( sleep 3; echo "DOWN the Backblaze service (bzserv) is not running" > "$FX/health" ) &
run_watchdog 5
ok '[ ! -f "$FX/wine.log" ]' "a DOWN after an OK is a first sighting: no Wine call"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
