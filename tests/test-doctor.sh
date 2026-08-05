#!/usr/bin/env bash
# Behavioural tests for bb-doctor's stale-lock respawn-loop check, run against a
# fixture tree.
#
# The property under test is the repair gate: "bb-doctor --fix" removes the
# four-hour lock, and a wrongly removed lock lets a second pass run against
# backup state a live one still owns. So the lock must go ONLY on the full
# respawn signature - lock present, grab failures still being written to
# today's log, and no bztransmit old enough to have created the lock - and any
# unreadable input must read as "keep the lock".
#
# Run:  bash tests/test-doctor.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../rootfs/usr/local/bin/bb-doctor"
FX="$(mktemp -d)"; trap 'rm -rf "$FX"' EXIT
PFX="$FX/config/wine/"
BZ="${PFX}dosdevices/c:/ProgramData/Backblaze/bzdata"
mkdir -p "$BZ/bzlogs/bztransmit" "$BZ/bzbackup" "${PFX}drive_c" "$FX/proc" "$FX/bin"
LOGCUR="$BZ/bzlogs/bztransmit/bztransmit$(date +%d).log"
LOCK="$BZ/bzbackup/lock_bzfileid_4_hour_lock.lck"

# Point the script's /proc scans at the fixture; the data paths follow from
# WINEPREFIX. Stub the two external commands: bb-health answers OK so the
# doctor's own check is what decides, and curl answers reachable instantly.
sed -e "s#/proc/\[0-9\]\*/cmdline#$FX/proc/[0-9]*/cmdline#g" \
    -e "s#/proc/uptime#$FX/proc/uptime#g" \
    "$SRC" > "$FX/bb-doctor"
chmod +x "$FX/bb-doctor"
printf '#!/bin/sh\necho OK\n' > "$FX/bin/bb-health"
printf '#!/bin/sh\nexit 0\n' > "$FX/bin/curl"
chmod +x "$FX/bin/bb-health" "$FX/bin/curl"
# On macOS (dev laptops) stat -c is BSD stat, so shim GNU-style stat -c %Y.
if ! stat -c %Y "$FX" >/dev/null 2>&1; then
    cat > "$FX/bin/stat" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then
    perl -e 'my @s=stat($ARGV[0]) or exit 1; print $s[9],"\n"' "$3"
else
    exec /usr/bin/stat "$@"
fi
EOF
    chmod +x "$FX/bin/stat"
fi

FAILED=0
UP=8000000   # fixture /proc/uptime, seconds since boot
has(){ if grep -q "$2" <<<"$1"; then echo "PASS $3"; else echo "FAIL $3 (missing '$2')"; FAILED=$((FAILED+1)); fi; }
locked(){ if [ -f "$LOCK" ]; then echo "PASS $1"; else echo "FAIL $1 (lock was removed)"; FAILED=$((FAILED+1)); fi; }
unlocked(){ if [ -f "$LOCK" ]; then echo "FAIL $1 (lock still present)"; FAILED=$((FAILED+1)); else echo "PASS $1"; fi; }
mkproc(){ rm -rf "$FX/proc"; mkdir -p "$FX/proc"; echo "$UP.00 $UP.00" > "$FX/proc/uptime"; local pid=100
  for cmd in "$@"; do mkdir -p "$FX/proc/$pid"; printf '%s' "$cmd" | tr ' ' '\0' > "$FX/proc/$pid/cmdline"; pid=$((pid+1)); done; }
# Give /proc/PID a stat file making the process AGE_S seconds old (starttime is
# the 20th field after the comm). mkproc assigns pids from 100 in argument
# order. Without a stat file the process age is unreadable, and the doctor must
# treat it as old enough to own the lock (fail safe).
procage(){ printf '%s (bztransmit.exe) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 %s 0\n' \
  "$1" $(( (UP - $2) * 100 )) > "$FX/proc/$1/stat"; }
agef(){ perl -e 'my $t=time-$ARGV[1]; utime $t,$t,$ARGV[0]' "$1" "$2"; }
spam(){ for i in $(seq 1 "$1"); do echo "10:00:0$i - Failed to grab fourHourLock lock (DoBackupPass.cpp:111)"; done; }
run(){ env WINEPREFIX="$PFX" PATH="$FX/bin:$PATH" "$FX/bb-doctor" "$@" 2>/dev/null; }

# The full wedge signature: a 30-minute lock nothing live created, grab
# failures being written to today's log right now, and only a seconds-old
# bztransmit alive (the respawn loop).
wedge(){ mkproc "bzserv.exe" "bztransmit.exe -doBackupPass"; procage 101 5
  touch "$LOCK"; agef "$LOCK" 1800; spam 8 > "$LOGCUR"; }

# 1. the full signature is reported as a problem, and without --fix nothing moves
wedge
out="$(run)"
has "$out" "bztransmit respawning against it" "respawn wedge is reported as a problem"
locked "without --fix the lock is untouched"

# 2. --fix removes the lock on the full signature
wedge
out="$(run --fix)"
has "$out" "removed the stale four-hour lock" "--fix reports the removal"
unlocked "--fix removes the stale lock"

# 3. a bztransmit older than the grace window may own the lock: keep it
wedge; procage 101 600
out="$(run --fix)"
has "$out" "no stall detected" "long-running bztransmit reads as healthy here"
locked "--fix keeps a lock a long-running bztransmit may own"

# 4. FAIL-SAFE: an unreadable start time counts as long-running
wedge; rm "$FX/proc/101/stat"
run --fix >/dev/null
locked "--fix keeps the lock when the process age is unreadable"

# 5. no grab failures in today's log: not a wedge, whatever the lock's age
wedge; echo "10:00:01 - normal transmit line" > "$LOGCUR"
run --fix >/dev/null
locked "--fix keeps the lock without fresh grab failures"

# 6. failures that stopped minutes ago are not a live respawn loop
wedge; agef "$LOGCUR" 900
run --fix >/dev/null
locked "--fix keeps the lock when the failures are no longer being written"

# 7. a lock younger than the grace window may belong to the pass that just
#    started: keep it
wedge; touch "$LOCK"
run --fix >/dev/null
locked "--fix keeps a lock younger than the grace window"

echo
echo "$FAILED failures"
exit $(( FAILED > 0 ? 1 : 0 ))
