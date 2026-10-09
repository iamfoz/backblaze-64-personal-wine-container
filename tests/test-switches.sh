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

echo "$FAILED failures"
exit $(( FAILED > 0 ? 1 : 0 ))
