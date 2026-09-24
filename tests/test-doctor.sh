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
    -e "s#^MEMINFO=.*#MEMINFO=$FX/meminfo#" \
    -e "s#^CG_EVENTS=.*#CG_EVENTS=$FX/memory.events#" \
    -e "s#^PRESSURE=/sys.*#PRESSURE=$FX/memory.pressure#" \
    -e "s#^\[ -r \"\$PRESSURE\" \] || PRESSURE=.*#:#" \
    -e "s#\"/proc/\$p/cmdline\"#\"$FX/proc/\$p/cmdline\"#g" \
    -e "s#^stop_process() { kill \"\$1\" 2>/dev/null; }#stop_process() { rm -rf \"$FX/proc/\$1\"; }#" \
    -e "s#/proc/uptime#$FX/proc/uptime#g" \
    "$SRC" > "$FX/bb-doctor"
chmod +x "$FX/bb-doctor"
printf '#!/bin/sh\necho OK\n' > "$FX/bin/bb-health"
printf '#!/bin/sh\nexit 0\n' > "$FX/bin/curl"
chmod +x "$FX/bin/bb-health" "$FX/bin/curl"
# The doctor bounds its Wine calls with coreutils timeout and util-linux setsid,
# both in the image. A dev laptop may lack either; stand-ins that just run the
# command keep the test about the doctor rather than the machine it runs on.
if ! command -v setsid >/dev/null 2>&1; then
    printf '#!/bin/sh\nexec "$@"\n' > "$FX/bin/setsid"; chmod +x "$FX/bin/setsid"
fi
if ! command -v timeout >/dev/null 2>&1; then
    cat > "$FX/bin/timeout" <<'EOF'
#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -k) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift   # the duration
exec "$@"
EOF
    chmod +x "$FX/bin/timeout"
fi
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

# ---- host memory: info on a large host, warning on a small one, warning on pressure --
meminfo(){ printf 'MemTotal: %d kB\nMemAvailable: %d kB\nSwapTotal: %d kB\nSwapFree: %d kB\n' $(($1*1048576)) $(($2*1048576)) $(($3*1048576)) $(($4*1048576)) > "$FX/meminfo"; }
printf 'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n' > "$FX/memory.events"
printf 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n' > "$FX/memory.pressure"
mkproc "bzserv.exe"; rm -f "$LOCK"; : > "$LOGCUR"
meminfo 62 40 0 0
R="$(run)"; has "$R" "\[info\] no swap; the client's passes peak at 4 to 6 GB" "memory: a large host without swap gets an info line"
has "$R" "ok, 1 info, " "memory: and the info is counted in the summary rather than as a warning"
if grep -q "\[warn\] no swap" <<<"$R"; then echo "FAIL memory: the large-host swap line must not be a warning"; FAILED=$((FAILED+1)); else echo "PASS memory: the large-host swap line is not a warning"; fi
meminfo 62 40 31 31
has "$(run)" "\[ ok \] 31 GB swap available" "memory: a large host with swap is plain ok"
meminfo 9 4 0 0
has "$(run)" "\[warn\] 9 GB host RAM and no swap - a memory peak becomes an out-of-memory kill" "memory: a small host without swap is still a warning"
meminfo 9 4 31 31
has "$(run)" "\[ ok \] 9 GB host RAM, with 31 GB swap to absorb the peaks" "memory: a small host with swap is ok"
meminfo 62 40 0 0
printf 'low 0\nhigh 0\nmax 0\noom 2\noom_kill 2\n' > "$FX/memory.events"
has "$(run)" "\[warn\] 2 processes in this container killed by the out-of-memory killer" "memory: out-of-memory kills in the container's accounting warn on any host"
printf 'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n' > "$FX/memory.events"
printf 'some avg10=5.00 avg60=4.00 avg300=3.50 total=1\nfull avg10=2.00 avg60=1.50 avg300=1.25 total=1\n' > "$FX/memory.pressure"
has "$(run)" "\[warn\] memory pressure: every task stalled on memory for 1.2% of the last five minutes" "memory: the kernel's pressure figure warns on any host"
printf 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n' > "$FX/memory.pressure"
meminfo 9 0 8 1
has "$(run)" "\[warn\] swap is three quarters used with under 1 GB of RAM available" "memory: nearly full swap with no RAM left warns"
rm -f "$FX/meminfo"

# ---- the three on-demand repairs: service, stuck pass, manifest ---------------------
# The fixture's wine stub "starts" the service by putting a bzserv.exe entry in
# the fake process table; stop_process is stood in for by removing an entry.
cat > "$FX/bin/wine" <<EOF
#!/bin/sh
case "\$1 \$2 \$3" in "net start bzserv") mkdir -p "$FX/proc/900"; printf 'C:\\\\Program Files\\\\Backblaze\\\\bzserv.exe' > "$FX/proc/900/cmdline";; esac
EOF
chmod +x "$FX/bin/wine"
mkdir -p "$PFX/drive_c/Program Files/Backblaze"; : > "$PFX/drive_c/Program Files/Backblaze/bzserv.exe"
rm -f "$LOCK"; : > "$LOGCUR"
mkproc "bzbui.exe -noquiet"
has "$(run)" "\[FAIL\] the Backblaze service (bzserv) is not running" "service: GUI up with no bzserv is reported"
has "$(run --fix)" "fixed: started the Backblaze service" "service: --fix starts it and sees it running"
mkproc "bzbui.exe -noquiet" "bzserv.exe"
has "$(run)" "\[ ok \] the Backblaze service (bzserv) is running" "service: both up is fine"
printf '#!/bin/sh\necho "HANG a push child has been alive 25m (threshold 20m) without finishing its chunk"\nexit 1\n' > "$FX/bin/bb-health"
mkproc "bzserv.exe" "bztransmit.exe -prepare_bzcombs" "bztransmit.exe -threadpush foo.xml"
has "$(run)" "re-run with --fix to stop the stuck pass" "hang: without --fix the pass is left alone and the repair named"
[ -d "$FX/proc/101" ] && [ -d "$FX/proc/102" ] && echo "PASS hang: and nothing was stopped" || { echo "FAIL hang: processes stopped without --fix"; FAILED=$((FAILED+1)); }
has "$(run --fix)" "fixed: stopped the stuck pass and 1 upload child" "hang: --fix stops the child and the pass"
[ ! -d "$FX/proc/101" ] && [ ! -d "$FX/proc/102" ] && [ -d "$FX/proc/100" ] && echo "PASS hang: both bztransmit gone, bzserv untouched" || { echo "FAIL hang: wrong processes stopped"; FAILED=$((FAILED+1)); }
printf '#!/bin/sh\necho OK\n' > "$FX/bin/bb-health"
mkdir -p "$PFX/drive_c/windows/system32" "$PFX/drive_c/windows/syswow64"
rm -f "$PFX/drive_c/windows/system32/rundll32.exe.manifest" "$PFX/drive_c/windows/syswow64/rundll32.exe.manifest"
has "$(run)" "\[warn\] supportedOS manifest missing in: system32 syswow64" "manifest: a missing manifest is reported"
has "$(run --fix)" "fixed: wrote the supportedOS manifest into: system32 syswow64" "manifest: --fix writes it"
grep -q '8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a' "$PFX/drive_c/windows/syswow64/rundll32.exe.manifest" && echo "PASS manifest: with the Windows 10 supportedOS id" || { echo "FAIL manifest: file content wrong"; FAILED=$((FAILED+1)); }
has "$(run)" "\[ ok \] rundll32 supportedOS manifest present" "manifest: and it is present afterwards"

# ---- the beta's source-drive drop-in: identity by the client's own format --------
# bzvol_id.xml carries bzVolumeGuid="v00" + 25 hex; bzvolumes.xml maps each id to
# a mount point as hex (443a5c is D:\). Captured from a live container on
# 2026-09-21; the earlier check looked for a GUID shape that never occurs and
# raised a false warning on one user's drive while missing a real one.
DROP="$HERE/../rootfs-beta/usr/local/lib/bb-doctor-drives.sh"
VOLS="$BZ/bzvolumes.xml"
KNOWN_D=v00121e7007550b3825692e70910; KNOWN_E=v000d1c7004550b3825692e70910
printf '<bzvolumes>\n<bzvolume bzVolumeGuid="%s" mountPointPathHex="443a5c" typeOfVolumeTwoCharCode="gm" />\n<bzvolume bzVolumeGuid="%s" mountPointPathHex="453a5c" typeOfVolumeTwoCharCode="gm" />\n</bzvolumes>\n' "$KNOWN_D" "$KNOWN_E" > "$VOLS"
# The stamp as captured: <bzvolume vguid="..." associated_hguid="..." />.
mkdrive(){ mkdir -p "$FX/drive_$1/.bzvol"; ln -sfn "$FX/drive_$1" "${PFX}dosdevices/$1:"; [ -n "$2" ] && printf '<?xml version="1.0" encoding="UTF-8" ?>\n<contents>\n<bzvolume vguid="%s" associated_hguid="%s" />\n</contents>\n' "$2" "${3:-a279b04955a1845499e60219}" > "$FX/drive_$1/.bzvol/bzvol_id.xml"; :; }
# This install's own identity, as bzinstall.xml carries it (attribute name captured 2026-09-22).
mkdir -p "${PFX}drive_c/Program Files/Backblaze"
printf '<?xml version="1.0"?>\n<bzinstall hguid="a279b04955a1845499e60219" version="10.0.3.1075" />\n' > "${PFX}drive_c/Program Files/Backblaze/bzinstall.xml"
mkdrive d "$KNOWN_D"                         # stamped with the id the client has for D:
mkdrive e "v00aaaaaaaaaaaaaaaaaaaaaaaaa"      # stamped by another install; the client has a record for E:
mkdrive f ""; : > "$FX/drive_f/.bzvol/bzvol_id.xml"   # stamp present but empty, and no record for F:
mkdrive g "$KNOWN_E"                         # the id the client knows as E:, now mapped as G:
mkdrive h ""                                 # .bzvol with no stamp yet
KNOWN_I=v00ffffffffffffffffffffffff1
printf '<bzvolume bzVolumeGuid="%s" mountPointPathHex="493a5c" typeOfVolumeTwoCharCode="gm" />\n' "$KNOWN_I" >> "$VOLS"
mkdrive i "$KNOWN_I" "0000000000000000deadbeef"  # right volume id, stamped for another computer identity
# Two records for one letter, an old drive and the current one (a user's list on
# 2026-09-22): the stamp matching either is recognised, and the value to restore
# is the record most recently seen attached, not the first in the file.
OLD_K=v000000000000000000000000001; CUR_K=v000000000000000000000000002
printf '<bzvolume bzVolumeGuid="%s" mountPointPathHex="4b3a5c" typeOfVolumeTwoCharCode="gm" lastTimeVolumeWasSeenAttachedGmtMillis="1700000000000" />\n<bzvolume bzVolumeGuid="%s" mountPointPathHex="4b3a5c" typeOfVolumeTwoCharCode="gm" lastTimeVolumeWasSeenAttachedGmtMillis="1790000000000" />\n' "$OLD_K" "$CUR_K" >> "$VOLS"
mkdrive k "$CUR_K"
printf '<bzvolume bzVolumeGuid="%s" mountPointPathHex="4c3a5c" typeOfVolumeTwoCharCode="gm" lastTimeVolumeWasSeenAttachedGmtMillis="1700000000000" />\n<bzvolume bzVolumeGuid="%s" mountPointPathHex="4c3a5c" typeOfVolumeTwoCharCode="gm" lastTimeVolumeWasSeenAttachedGmtMillis="1790000000000" />\n' "v000000000000000000000000003" "v000000000000000000000000004" >> "$VOLS"
mkdrive l "v00fffffffffffffffffffffffff"
# One drive with a large sparse file for the read-speed sample; the others have none.
mkdir -p "$FX/drive_d/media"; dd if=/dev/zero of="$FX/drive_d/media/film.mkv" bs=1M count=0 seek=300 2>/dev/null
DR="$(cd "$FX" && sh -c 'PREFIX="$1"; BZ="$2"; OK(){ echo "[ok] $*"; }; WARN(){ echo "[warn] $*"; }; BAD(){ echo "[FAIL] $*"; }; NOTE(){ echo "  $*"; }; . "$3"' _ "$PFX" "$BZ" "$DROP" 2>&1)"
if date +%s%N 2>/dev/null | grep -qE '^[0-9]{16,}$'; then
  has "$DR" "D: reads at about [0-9]* MB/s (64 MB sample)" "drives: read speed is measured on a drive with a large file"
else
  has "$DR" "D: read speed not measured (could not read .*, or no high-resolution clock)" "drives: without a high-resolution clock the read speed is not invented"
fi
has "$DR" "E: read speed not measured: no file over 64 MB within 3 levels" "drives: no large file means no measurement, said plainly, three levels by default"
# The depth setting: a file four levels down is missed at the default and found
# with "until a file is found"; the conf file is what the Settings tab writes.
mkdir -p "$FX/drive_e/a/b/c"; dd if=/dev/zero of="$FX/drive_e/a/b/c/deep.mkv" bs=1M count=0 seek=200 2>/dev/null
DRS="$(cd "$FX" && sh -c 'PREFIX="$1"; BZ="$2"; OK(){ echo "[ok] $*"; }; WARN(){ echo "[warn] $*"; }; BAD(){ echo "[FAIL] $*"; }; NOTE(){ echo "  $*"; }; sed "s#/config/bb-api/doctor.json#$4#" "$3" > "$4.sh"; . "$4.sh"' _ "$PFX" "$BZ" "$DROP" "$FX/doctor.json" 2>&1)"
has "$DRS" "E: read speed not measured: no file over 64 MB within 3 levels" "drives: a file four levels down is not found at the default depth"
printf '{"read_depth": 3, "read_until_found": true}\n' > "$FX/doctor.json"
DRS="$(cd "$FX" && sh -c 'PREFIX="$1"; BZ="$2"; OK(){ echo "[ok] $*"; }; WARN(){ echo "[warn] $*"; }; BAD(){ echo "[FAIL] $*"; }; NOTE(){ echo "  $*"; }; . "$4.sh"' _ "$PFX" "$BZ" "$DROP" "$FX/doctor.json" 2>&1)"
if date +%s%N 2>/dev/null | grep -qE '^[0-9]{16,}$'; then has "$DRS" "E: reads at about [0-9]* MB/s" "drives: with until-found on, the deep file is measured"; fi
printf '{"read_depth": 4, "read_until_found": false}\n' > "$FX/doctor.json"
DRS="$(cd "$FX" && sh -c 'PREFIX="$1"; BZ="$2"; OK(){ echo "[ok] $*"; }; WARN(){ echo "[warn] $*"; }; BAD(){ echo "[FAIL] $*"; }; NOTE(){ echo "  $*"; }; . "$4.sh"' _ "$PFX" "$BZ" "$DROP" "$FX/doctor.json" 2>&1)"
if date +%s%N 2>/dev/null | grep -qE '^[0-9]{16,}$'; then has "$DRS" "E: reads at about [0-9]* MB/s" "drives: a depth of four from the setting finds it too"; fi
rm -f "$FX/doctor.json"; rm -rf "$FX/drive_e/a"
has "$DR" "\[ok\] D: the client recognises this drive (id $KNOWN_D)" "drives: a stamp matching the client's record for that letter is recognised"
has "$DR" "\[warn\] E: the client does not recognise this drive's identity" "drives: a stamp from another install is a warning"
has "$DR" "record for E: is $KNOWN_E" "drives: and the note names the id the client has for that letter"
has "$DR" "set vguid in .*drive_e/.bzvol/bzvol_id.xml to that id" "drives: the repair names the field and the file"
has "$DR" "Never delete .bzvol" "drives: and warns against deleting the stamp"
if grep -q "a279b04955a1845499e60219" <<<"$DR"; then echo "FAIL drives: the hguid must never be printed"; FAILED=$((FAILED+1)); else echo "PASS drives: the hguid is never printed"; fi
has "$DR" "\[warn\] F: .bzvol/bzvol_id.xml carries no volume id" "drives: an empty stamp is a warning"
has "$DR" "no record of a drive at F:" "drives: with no record the advice is to re-tick the drive"
has "$DR" "\[warn\] G: the client knows this drive as E:, not G:" "drives: a known id under another letter names the letter it moved from"
has "$DR" "H: .bzvol present but no bzvol_id.xml yet" "drives: a .bzvol without a stamp is not a warning"
has "$DR" "\[warn\] I: the drive is stamped for a different computer identity" "drives: a stamp for another computer identity is a warning"
has "$DR" "\[ok\] K: the client recognises this drive (id $CUR_K)" "drives: with two records for one letter, a stamp matching the current one is recognised"
if grep -q "K: the client knows this drive as K:, not K:" <<<"$DR"; then echo "FAIL drives: the same-letter contradiction is back"; FAILED=$((FAILED+1)); else echo "PASS drives: no 'known as K:, not K:' contradiction"; fi
has "$DR" "record for L: is v000000000000000000000000004" "drives: the value to restore is the record most recently seen attached"
has "$DR" "set associated_hguid in .*drive_i/.bzvol/bzvol_id.xml" "drives: with the field and file to change"
if grep -q "deadbeef\|a279b04955a1845499e60219" <<<"$DR"; then echo "FAIL drives: neither hguid may be printed"; FAILED=$((FAILED+1)); else echo "PASS drives: neither hguid is printed"; fi
# --fix: the two repairs with a known value, and the refusals. A stamp in any
# other shape than the client's is left alone, and so is an empty one.
mkdir -p "$FX/drive_j/.bzvol"; ln -sfn "$FX/drive_j" "${PFX}dosdevices/j:"
printf '<bzvolume vguid="v00aaaaaaaaaaaaaaaaaaaaaaaaa" associated_hguid="x" />\n<bzvolume vguid="v00bbbbbbbbbbbbbbbbbbbbbbbbb" associated_hguid="x" />\n' > "$FX/drive_j/.bzvol/bzvol_id.xml"
printf '<bzvolume bzVolumeGuid="%s" mountPointPathHex="4a3a5c" typeOfVolumeTwoCharCode="gm" />\n' "v00ccccccccccccccccccccccccc" >> "$VOLS"
DF="$(cd "$FX" && sh -c 'FIX=1; PREFIX="$1"; BZ="$2"; OK(){ echo "[ok] $*"; }; WARN(){ echo "[warn] $*"; }; BAD(){ echo "[FAIL] $*"; }; NOTE(){ echo "  $*"; }; . "$3"' _ "$PFX" "$BZ" "$DROP" 2>&1)"
has "$DF" "\[ok\] E: repaired: vguid set to $KNOWN_E" "fix: a stamp from another install gets the client's own id"
grep -q "vguid=\"$KNOWN_E\" associated_hguid=\"a279b04955a1845499e60219\"" "$FX/drive_e/.bzvol/bzvol_id.xml" && echo "PASS fix: the file carries the new vguid and the untouched hguid" || { echo "FAIL fix: drive_e stamp not rewritten as expected"; FAILED=$((FAILED+1)); }
ls "$FX/drive_e/.bzvol/"bzvol_id.xml.bak-* >/dev/null 2>&1 && echo "PASS fix: the previous stamp is kept beside it" || { echo "FAIL fix: no backup of the stamp"; FAILED=$((FAILED+1)); }
has "$DF" "\[ok\] I: repaired: the stamp now carries this install's computer identity" "fix: a stamp for another identity gets this install's"
grep -q "vguid=\"$KNOWN_I\" associated_hguid=\"a279b04955a1845499e60219\"" "$FX/drive_i/.bzvol/bzvol_id.xml" && echo "PASS fix: the file carries the new hguid and the untouched vguid" || { echo "FAIL fix: drive_i stamp not rewritten as expected"; FAILED=$((FAILED+1)); }
has "$DF" "\[warn\] F: .bzvol/bzvol_id.xml carries no volume id" "fix: an empty stamp is still only a warning"
[ ! -s "$FX/drive_f/.bzvol/bzvol_id.xml" ] && ! ls "$FX/drive_f/.bzvol/"bzvol_id.xml.bak-* >/dev/null 2>&1 && echo "PASS fix: and is not written" || { echo "FAIL fix: the empty stamp was touched"; FAILED=$((FAILED+1)); }
has "$DF" "\[FAIL\] J: could not repair" "fix: a stamp with two volume lines is refused"
[ "$(grep -c '<bzvolume ' "$FX/drive_j/.bzvol/bzvol_id.xml")" = 2 ] && echo "PASS fix: and left as it was" || { echo "FAIL fix: the malformed stamp was changed"; FAILED=$((FAILED+1)); }
if grep -q "deadbeef\|a279b04955a1845499e60219" <<<"$DF"; then echo "FAIL fix: neither hguid may be printed"; FAILED=$((FAILED+1)); else echo "PASS fix: neither hguid is printed"; fi
for L in d e f g h i j k l; do rm -f "${PFX}dosdevices/$L:"; done

echo
echo "$FAILED failures"
exit $(( FAILED > 0 ? 1 : 0 ))
