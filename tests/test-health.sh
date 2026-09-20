#!/usr/bin/env bash
# Behavioural tests for bb-health's stall detection, run against a fixture tree.
#
# The property under test is fail-SAFE detection: bb-health's HANG verdict triggers
# a recovery that kills live upload threads, so a read error, a rotated log, or a
# malformed threshold must never read as "stalled". A false alarm here does not
# crash anything - it kills a healthy backup mid-pass, which is exactly the damage
# the tool exists to prevent.
#
# Run:  bash tests/test-health.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../rootfs/usr/local/bin/bb-health"
FX="$(mktemp -d)"; trap 'rm -rf "$FX"' EXIT
BZ="$FX/config/wine/dosdevices/c:/ProgramData/Backblaze/bzdata"
mkdir -p "$BZ/bzlogs/bztransmit" "$BZ/bzbackup" "$FX/proc"
LOGF="$BZ/bzlogs/bztransmit/bztransmit01.log"
LOCK="$BZ/bzbackup/lock_bzfileid_4_hour_lock.lck"

# Point the script's roots at the fixture. On macOS (dev laptops) stat -c is BSD
# stat, so shim GNU-style stat -c %Y via perl if needed.
sed -e "s#^BZ=\"/config#BZ=\"$FX/config#" \
    -e "s#/proc/\[0-9\]\*/cmdline#$FX/proc/[0-9]*/cmdline#g" \
    -e "s#/proc/uptime#$FX/proc/uptime#g" \
    "$SRC" > "$FX/bb-health"
chmod +x "$FX/bb-health"
if ! stat -c %Y "$FX" >/dev/null 2>&1; then
    mkdir -p "$FX/bin"
    cat > "$FX/bin/stat" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then
    perl -e 'my @s=stat($ARGV[0]) or exit 1; print $s[9],"\n"' "$3"
else
    exec /usr/bin/stat "$@"
fi
EOF
    chmod +x "$FX/bin/stat"
    export PATH="$FX/bin:$PATH"
fi

FAILED=0
UP=8000000   # fixture /proc/uptime, seconds since boot
ok(){ if [ "$1" = "$2" ]; then echo "PASS $3"; else echo "FAIL $3 (want '$1' got '$2')"; FAILED=$((FAILED+1)); fi; }
mkproc(){ rm -rf "$FX/proc"; mkdir -p "$FX/proc"; echo "$UP.00 $UP.00" > "$FX/proc/uptime"; local pid=100
  for cmd in "$@"; do mkdir -p "$FX/proc/$pid"; printf '%s' "$cmd" | tr ' ' '\0' > "$FX/proc/$pid/cmdline"; pid=$((pid+1)); done; }
# Give /proc/PID a stat file making the process AGE_S seconds old (starttime is
# the 20th field after the comm). mkproc assigns pids from 100 in argument
# order. Without a stat file the process age is unreadable, and bb-health must
# treat it as old enough to own the lock (fail safe).
procage(){ printf '%s (bztransmit.exe) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 %s 0\n' \
  "$1" $(( (UP - $2) * 100 )) > "$FX/proc/$1/stat"; }
run(){ env "$@" "$FX/bb-health" 2>/dev/null | awk '{print $1}'; }
old(){ touch -t 202601010000 "$1"; }
# Set a file's mtime to AGE_S seconds ago (touch -t cannot express a relative
# age portably; perl is already a dependency of the stat shim above).
agef(){ perl -e 'my $t=time-$ARGV[1]; utime $t,$t,$ARGV[0]' "$1" "$2"; }
spam(){ for i in $(seq 1 "$1"); do echo "2026-07-05 10:00:0$i Failed to grab fourHourLock"; done; }

# 1. empty container is healthy
mkproc; : > "$LOGF"
ok "OK" "$(run)" "empty container is healthy"

# 2. active transmit, fresh log
mkproc "bztransmit.exe -threadpush foo.xml" "bzserv.exe"
echo "line" > "$LOGF"
ok "OK" "$(run)" "active transmit with a fresh log is healthy"

# 3. HANG: push alive, log stale
old "$LOGF"
ok "HANG" "$(run)" "push alive + stale log = HANG"

# 4. stale log but no push = idle, not a hang
mkproc "bzserv.exe"
ok "OK" "$(run)" "stale log with no push is idle"

# 5. FAIL-SAFE: log vanishes between discovery and the age check -> no verdict.
#    (Simulated by a stat that cannot read the file: age_min returns empty.)
mkproc "bztransmit.exe -threadpush foo.xml"
old "$LOGF"
mkdir -p "$FX/bin2"; cat > "$FX/bin2/stat" <<'EOF'
#!/bin/sh
exit 1
EOF
chmod +x "$FX/bin2/stat"
got="$(env PATH="$FX/bin2:$PATH" "$FX/bb-health" 2>/dev/null | awk '{print $1}')"
ok "OK" "$got" "unreadable log mtime fails SAFE (no fabricated HANG)"

# 6. FAIL-SAFE: malformed STALL_MIN falls back to the default instead of erroring.
#    Fresh log + junk threshold must stay healthy (find -newermt would have flagged
#    HANG here on every run, permanently).
echo "line" > "$LOGF"
ok "OK" "$(run STALL_MIN=20m)" "malformed STALL_MIN on a fresh log stays healthy"
old "$LOGF"
ok "HANG" "$(run STALL_MIN=20m)" "malformed STALL_MIN still detects a real stall via the default"

# 7. WEDGE: old lock + spam + no bztransmit
mkproc "bzserv.exe"; touch "$LOCK"; old "$LOCK"; spam 8 > "$LOGF"
ok "WEDGE" "$(run)" "old lock + failure spam + no bztransmit = WEDGE"

# 8. live pass owns the lock: never flagged
mkproc "bztransmit.exe -completesync"
ok "OK" "$(run)" "lock held by a live bztransmit is not a wedge"

# 9. fresh lock is not a wedge even with spam
mkproc "bzserv.exe"; touch "$LOCK"
ok "OK" "$(run)" "fresh lock is not a wedge"

# 10. old lock without spam is not a wedge
old "$LOCK"; : > "$LOGF"
ok "OK" "$(run)" "old lock without failure spam is not a wedge"

# 11. WEDGE via the respawn loop: the lock survived a restart, bzserv relaunches
#     bztransmit every few seconds and each attempt dies on it. Every mtime is
#     fresh and the lock is younger than LOCK_AGE_MIN, so only the process age
#     can corroborate: a 30-minute lock cannot belong to a 5-second pass.
mkproc "bzserv.exe" "bztransmit.exe -doBackupPass"; procage 101 5
touch "$LOCK"; agef "$LOCK" 1800; spam 8 > "$LOGF"
ok "WEDGE" "$(run)" "respawn loop against a restart-surviving lock = WEDGE"

# 12. same signature, but the pass has been running longer than the grace
#     window: it is never second-guessed, even though the lock predates it
procage 101 600
ok "OK" "$(run)" "bztransmit older than the grace window is assumed to own the lock"

# 13. FAIL-SAFE: an unreadable process start time must read as "old enough to
#     own the lock", never as a removable wedge
rm "$FX/proc/101/stat"
ok "OK" "$(run)" "unreadable bztransmit start time fails SAFE (no wedge verdict)"

# 14. a lock younger than the grace window may have just been created by the
#     young pass that is now writing: not a wedge
procage 101 5; touch "$LOCK"
ok "OK" "$(run)" "fresh lock beside a young pass is not a wedge"

# 15. past LOCK_AGE_MIN the seconds-old respawns must not mask the wedge the
#     way "any bztransmit alive" used to
old "$LOCK"
ok "WEDGE" "$(run)" "old lock + failure spam + only seconds-old respawns = WEDGE"

# 16. HANG: the pass says it is waiting for sub_threads, has been for longer
#     than STALL_MIN, and no push child exists. The heartbeat keeps the log's
#     mtime fresh, so only the wait it reports can show the hang. (A child that
#     died before reporting: Wine 11.18 on 2026-09-19.)
rm -f "$LOCK"; mkproc "bzserv.exe" "bztransmit.exe -prepare_bzcombs"; procage 101 90000
printf '%s\n' "2026-09-20 20:34:17 696 - BzThread_WaitForAllToFinishAndProcessAllResults - Some sub_threads busy for numMillis=1800000, waiting..." > "$LOGF"
ok "HANG" "$(run)" "pass waiting 30m for sub_threads with no push child = HANG"

# 17. the same wait below the threshold is a pass finishing a batch: OK
printf '%s\n' "2026-09-20 20:34:17 696 - Some sub_threads busy for numMillis=300000, waiting..." > "$LOGF"
ok "OK" "$(run)" "pass waiting 5m for sub_threads is not a hang"

# 18. a long wait with a young child alive is a slow link, not a hang
mkproc "bzserv.exe" "bztransmit.exe -prepare_bzcombs" "bztransmit.exe -threadpush foo.xml"; procage 102 30
printf '%s\n' "2026-09-20 20:34:17 696 - Some sub_threads busy for numMillis=1800000, waiting..." > "$LOGF"
ok "OK" "$(run)" "long sub_threads wait with a 30s-old child is a slow link, not a hang"

# 19. HANG: a push child alive for longer than STALL_MIN never reached its
#     upload, whatever the log says. (Wine 11.17 on 2026-09-20: a child stuck
#     in process start-up for four hours while the parent's heartbeat ran.)
procage 102 1500; echo "fresh line" > "$LOGF"
ok "HANG" "$(run)" "push child alive 25m with a fresh log = HANG"

# 20. FAIL-SAFE: an unreadable child start time must not read as a hang
rm "$FX/proc/102/stat"
ok "OK" "$(run)" "unreadable push child start time fails SAFE (no hang verdict)"

# 21. an old wait line that is no longer the last line belongs to the past
mkproc "bzserv.exe" "bztransmit.exe -prepare_bzcombs"
printf '%s\n%s\n' "... Some sub_threads busy for numMillis=1800000, waiting..." "2026-09-20 20:40:00 696 - STARTBACKUP" > "$LOGF"
ok "OK" "$(run)" "a stale sub_threads wait above the last line is not a hang"

echo
echo "$FAILED failures"
exit $(( FAILED > 0 ? 1 : 0 ))
