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
ok(){ if [ "$1" = "$2" ]; then echo "PASS $3"; else echo "FAIL $3 (want '$1' got '$2')"; FAILED=$((FAILED+1)); fi; }
mkproc(){ rm -rf "$FX/proc"; mkdir -p "$FX/proc"; local pid=100
  for cmd in "$@"; do mkdir -p "$FX/proc/$pid"; printf '%s' "$cmd" | tr ' ' '\0' > "$FX/proc/$pid/cmdline"; pid=$((pid+1)); done; }
run(){ env "$@" "$FX/bb-health" 2>/dev/null | awk '{print $1}'; }
old(){ touch -t 202601010000 "$1"; }
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

echo
echo "$FAILED failures"
exit $(( FAILED > 0 ? 1 : 0 ))
