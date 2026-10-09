#!/usr/bin/env bash
# Tests for the Wine patch switches: how bb-wine-switches.sh decides each
# switch from the build manifest, the Settings store and the container
# variable, the interlocks between them, the client pin when the service token
# is off, and the startapp.sh block that exports them.
#
# The property that matters most: on an image whose Wine has no manifest
# (WineHQ's Wine), nothing is exported, and on a patched Wine a switch the
# user turned off is exported as off.
#
# Run:  bash tests/test-switches.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/.."
LIB="$ROOT/rootfs/usr/local/lib/bb-wine-switches.sh"
REG="$ROOT/rootfs/usr/local/share/bb64/wine-switches.tsv"
FX="$(mktemp -d)"; trap 'rm -rf "$FX"' EXIT
MAN="$FX/bb64-patches"; STORE="$FX/wine-switches.json"; APPLIED="$FX/applied"
FAILED=0
is(){ if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1 (got '$2', want '$3')"; FAILED=$((FAILED+1)); fi; }

ALL_APPLIED='wine_ref wine-11.19
applied wine-writability-fix switch
applied wine-fdwrite-rearm switch
applied wine-token-localsystem switch
applied wine-debug-privilege-bypass switch
applied wine-ofd-locks switch
applied wine-case-cache switch
applied wine-mountpoint-dirs builtin'

# Run the helper with a clean environment: only the variables given here.
ws(){ env -i PATH="$PATH" BB_WS_REGISTRY="$REG" BB_WS_MANIFEST="$MAN" BB_WS_STORE="$STORE" BB_WS_APPLIED="$APPLIED" "$@"; }
resolve(){ ws "$@" sh -c '. "$0"; bb_ws_resolve' "$LIB" | cut -d' ' -f1,2 | tr '\n' ' ' | sed 's/ $//'; }

# Every row has seven tab-separated fields and a known class.
bad="$(grep -v '^#' "$REG" | awk -F'\t' 'NF != 7 || ($3 != "fix" && $3 != "performance" && $3 != "experimental")')"
is "every registry row has seven fields and a known class" "$bad" ""
# Every patch the registry names is in the series, and every series patch
# has a switch: a companion .switch.patch or one built in.
for p in $(grep -v '^#' "$REG" | cut -f2 | tr ',' ' '); do
    grep -qx "$p" "$ROOT/patches/series" || { echo "FAIL $p is in the registry but not the series"; FAILED=$((FAILED+1)); }
done
for p in $(grep -v '^#' "$ROOT/patches/series" | grep -v '^$'); do
    grep -v '^#' "$REG" | cut -f2 | tr ',' '\n' | grep -qx "$p" || { echo "FAIL $p has no switch in the registry"; FAILED=$((FAILED+1)); }
done
echo "PASS registry and series agree"

# patches/promoted pins :latest-patched. CI reads it as data and checks each
# value, so a malformed one fails the release; check it here first.
PIN="$ROOT/patches/promoted"
pref="$(sed -n 's/^WINE_REF=//p' "$PIN" | head -1)"; pfrom="$(sed -n 's/^PATCHES_FROM=//p' "$PIN" | head -1)"
printf '%s' "$pref" | grep -Eq '^[A-Za-z0-9._/-]+$' && echo "PASS promoted: WINE_REF is set ($pref)" || { echo "FAIL promoted: bad WINE_REF '$pref'"; FAILED=$((FAILED+1)); }
printf '%s' "$pfrom" | grep -Eq '^[A-Za-z0-9._/-]+$' && echo "PASS promoted: PATCHES_FROM is set ($pfrom)" || { echo "FAIL promoted: bad PATCHES_FROM '$pfrom'"; FAILED=$((FAILED+1)); }
is "promoted: one WINE_REF and one PATCHES_FROM" "$(grep -c '^WINE_REF=' "$PIN") $(grep -c '^PATCHES_FROM=' "$PIN")" "1 1"
if [ "$pfrom" != this ] && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$ROOT" rev-parse --verify --quiet "${pfrom}^{commit}" >/dev/null \
        && echo "PASS promoted: PATCHES_FROM names a commit here" \
        || { echo "FAIL promoted: PATCHES_FROM=$pfrom is not a tag or commit in this repository"; FAILED=$((FAILED+1)); }
fi

rm -f "$MAN" "$STORE"
is "WineHQ's Wine (no manifest): nothing resolved" "$(resolve WINE_SOCK_SEND_READY=1)" ""

printf '%s\n' "$ALL_APPLIED" > "$MAN"
BETA="WINE_SOCK_SEND_READY=1 WINE_SOCK_FDWRITE_REARM=1 WINE_SERVICE_TOKEN=1 WINE_OFD_LOCKS=1 WINE_CASE_CACHE=1"
PATCHED="WINE_SOCK_SEND_READY=1 WINE_SOCK_FDWRITE_REARM=0 WINE_SERVICE_TOKEN=1 WINE_OFD_LOCKS=1 WINE_CASE_CACHE=0"
is "beta defaults: everything on" "$(resolve $BETA)" \
   "WINE_SOCK_SEND_READY 1 WINE_SOCK_FDWRITE_REARM 1 WINE_SERVICE_TOKEN 1 WINE_OFD_LOCKS 1 WINE_CASE_CACHE 1"
is ":latest-patched defaults: fixes on, performance off" "$(resolve $PATCHED)" \
   "WINE_SOCK_SEND_READY 1 WINE_SOCK_FDWRITE_REARM 0 WINE_SERVICE_TOKEN 1 WINE_OFD_LOCKS 1 WINE_CASE_CACHE 0"
is "no variables: everything off, as upstream Wine" "$(resolve)" \
   "WINE_SOCK_SEND_READY 0 WINE_SOCK_FDWRITE_REARM 0 WINE_SERVICE_TOKEN 0 WINE_OFD_LOCKS 0 WINE_CASE_CACHE 0"

echo '{"WINE_CASE_CACHE": "0", "WINE_OFD_LOCKS": "0"}' > "$STORE"
is "the Settings store wins over the variable" "$(resolve $BETA)" \
   "WINE_SOCK_SEND_READY 1 WINE_SOCK_FDWRITE_REARM 1 WINE_SERVICE_TOKEN 1 WINE_OFD_LOCKS 0 WINE_CASE_CACHE 0"
is "source: setting for a stored switch" "$(ws sh -c '. "$0"; bb_ws_source WINE_CASE_CACHE' "$LIB")" "setting"
is "source: variable for the rest" "$(ws sh -c '. "$0"; bb_ws_source WINE_SOCK_SEND_READY' "$LIB")" "variable"
rm -f "$STORE"

out="$(ws WINE_SOCK_SEND_READY=0 WINE_SOCK_FDWRITE_REARM=1 sh -c '. "$0"; bb_ws_resolve' "$LIB" | grep FDWRITE)"
is "the re-arm needs the writability fix: off with it" "$(echo "$out" | cut -d' ' -f2)" "0"
case "$out" in *"needs WINE_SOCK_SEND_READY"*) echo "PASS and says why";; *) echo "FAIL no reason given: $out"; FAILED=$((FAILED+1));; esac

# A patch that went upstream: its switch is not offered, and a switch that
# needs it is not held off by it.
printf '%s\n' "$ALL_APPLIED" | sed 's/^applied wine-writability-fix switch$/upstream wine-writability-fix/' > "$MAN"
is "state of a switch whose patch is upstream" "$(ws sh -c '. "$0"; bb_ws_state WINE_SOCK_SEND_READY' "$LIB")" "upstream"
is "an upstream patch's switch is not exported, and the re-arm still runs" \
   "$(resolve WINE_SOCK_FDWRITE_REARM=1 | cut -d' ' -f1-2)" "WINE_SOCK_FDWRITE_REARM 1"
# Half of a two-patch switch missing: the switch is absent.
printf '%s\n' "$ALL_APPLIED" | grep -v debug-privilege > "$MAN"
is "a switch with one of its patches missing is absent" "$(ws sh -c '. "$0"; bb_ws_state WINE_SERVICE_TOKEN' "$LIB")" "absent"
printf '%s\n' "$ALL_APPLIED" > "$MAN"

needs(){ ws sh -c '. "$0"; bb_ws_client_needs_token "$1" && echo yes || echo no' "$LIB" "$1"; }
is "10.0.3.1075 needs the token" "$(needs 10.0.3.1075)" yes
is "10.0.4.1 needs the token" "$(needs 10.0.4.1)" yes
is "10.1.0.900 needs the token" "$(needs 10.1.0.900)" yes
is "10.0.1.1069 does not" "$(needs 10.0.1.1069)" no
is "10.0.3.1074 does not" "$(needs 10.0.3.1074)" no

older(){ ws sh -c '. "$0"; bb_ws_wine_older "$1" "$2" && echo yes || echo no' "$LIB" "$1" "$2"; }
is "wine-11.0 is older than wine-11.19" "$(older wine-11.0 wine-11.19)" yes
is "wine-11.0.2 is older than wine-11.19" "$(older wine-11.0.2 wine-11.19)" yes
is "wine-11.19 is not older than wine-11.0" "$(older wine-11.19 wine-11.0)" no
is "the same version is not older" "$(older wine-11.19 wine-11.19)" no
is "a suffix such as (Staging) is ignored" "$(older "wine-11.9 (Staging)" wine-11.19)" yes
is "nothing recorded is not older" "$(older wine-11.0 "")" no

# The startapp.sh block itself, cut out by its opening comment and run with
# a stub log, then the environment it leaves.
BLOCK="$FX/block.sh"
awk '/^# The fork.s Wine \(the beta and :latest-patched\)/{on=1} on{print} on && /^fi$/{exit}' "$ROOT/rootfs/startapp.sh" \
    | sed "s#/usr/local/lib/bb-wine-switches.sh#$LIB#g" > "$BLOCK"
grep -q bb_ws_resolve "$BLOCK" || { echo "FAIL could not find the startapp block"; FAILED=$((FAILED+1)); }
runblock(){ ws LOG="$FX/log" "$@" bash -c 'log_message(){ echo "$*" >> "$LOG"; }; . "$0"; env | grep -E "^(WINE_|BACKBLAZE_VERSION=)" | sort | tr "\n" " "' "$BLOCK"; }
rm -f "$FX/log"
is "startapp: beta defaults exported" "$(runblock $BETA BACKBLAZE_VERSION=)" \
   "BACKBLAZE_VERSION= WINE_CASE_CACHE=1 WINE_OFD_LOCKS=1 WINE_SERVICE_TOKEN=1 WINE_SOCK_FDWRITE_REARM=1 WINE_SOCK_SEND_READY=1 "
is "startapp: applied file records them for the restart notice" "$(grep -c '=' "$APPLIED")" "5"
echo '{"WINE_SERVICE_TOKEN": "0"}' > "$STORE"
is "startapp: token off with no pin pins the client" "$(runblock $BETA BACKBLAZE_VERSION= | tr ' ' '\n' | grep -E '^(BACKBLAZE_VERSION|WINE_SERVICE_TOKEN)=' | tr '\n' ' ')" \
   "BACKBLAZE_VERSION=10.0.1.1069 WINE_SERVICE_TOKEN=0 "
grep -q "client is pinned to 10.0.1.1069" "$FX/log" && echo "PASS and logs it" || { echo "FAIL pin not logged"; FAILED=$((FAILED+1)); }
rm -f "$FX/log"
runblock $BETA BACKBLAZE_VERSION=10.0.3.1075 >/dev/null
grep -q "BACKBLAZE_VERSION=10.0.3.1075 needs it" "$FX/log" && echo "PASS token off with a pin that needs it is logged, the pin is left alone" || { echo "FAIL no warning for an explicit pin that needs the token"; FAILED=$((FAILED+1)); }
rm -f "$STORE" "$MAN"
is "startapp: WineHQ's Wine exports nothing and leaves the pin" "$(runblock BACKBLAZE_VERSION=10.0.1.1069)" "BACKBLAZE_VERSION=10.0.1.1069 "

# The datasets block in startapp.sh: on WineHQ's Wine the setting is dropped
# and logged, on a Wine with the patch it is exported.
MBLOCK="$FX/mblock.sh"
awk '/^# Which drives show their mount points \(ZFS datasets\)/{on=1} on{print} on && /^fi$/{exit}' "$ROOT/rootfs/startapp.sh" \
    | sed -e "s#/usr/local/lib/bb-mountpoints.sh#$ROOT/rootfs/usr/local/lib/bb-mountpoints.sh#g" \
          -e "s#/usr/local/lib/bb-wine-switches.sh#$LIB#g" -e "s#/tmp/.bb-mountpoints-applied#$FX/mp-applied#" > "$MBLOCK"
grep -q bb_mountpoints_value "$MBLOCK" || { echo "FAIL could not find the datasets block"; FAILED=$((FAILED+1)); }
mrun(){ ws LOG="$FX/log" BB_MOUNTPOINTS_STORE="$FX/none.json" "$@" bash -c 'log_message(){ echo "$*" >> "$LOG"; }; . "$0"; echo "${WINE_MOUNTPOINTS_AS_DIRS:-unset}"' "$MBLOCK"; }
rm -f "$MAN" "$FX/log"
is "datasets on WineHQ's Wine: not exported" "$(mrun MOUNTPOINTS_AS_DIRS=all)" "unset"
grep -q "this Wine has no such patch" "$FX/log" && echo "PASS and logged" || { echo "FAIL not logged"; FAILED=$((FAILED+1)); }
printf '%s\n' "$ALL_APPLIED" > "$MAN"
is "datasets on the patched Wine: exported" "$(mrun MOUNTPOINTS_AS_DIRS=all)" "1"
is "datasets per drive on the patched Wine" "$(mrun MOUNTPOINTS_AS_DIRS=e,d)" "de"

echo "$FAILED failures"
exit $(( FAILED > 0 ? 1 : 0 ))
