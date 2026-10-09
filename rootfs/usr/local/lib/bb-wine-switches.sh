# The runtime switches in the fork's Wine. Sourced by startapp.sh, which
# exports the result before the first Wine process starts, and by bb-doctor.
#
# Each switch is a Wine environment variable (WINE_SOCK_SEND_READY and so on)
# that the patched Wine reads once per process; wineserver reads its own when
# it starts. Without the variable Wine behaves as upstream Wine does. The list
# is /usr/local/share/bb64/wine-switches.tsv. Which ones this image's Wine has
# is in the manifest the build writes, /opt/wine/share/bb64-patches. An image
# with WineHQ's Wine has no manifest, so nothing here applies to it.
#
# The value comes from the Settings tab's store when it names the switch, and
# from the container's own variable of the same name when it does not. The
# image sets that variable to its default: on in the beta for the fixes and
# the performance patches, and on in :latest-patched for the fixes only.
# bbwine.py is the Python side of the same rules and the two must agree.
#
# WINE_MOUNTPOINTS_AS_DIRS is listed for the page but set per drive by
# bb-mountpoints.sh, not here.

BB_WS_REGISTRY="${BB_WS_REGISTRY:-/usr/local/share/bb64/wine-switches.tsv}"
BB_WS_MANIFEST="${BB_WS_MANIFEST:-/opt/wine/share/bb64-patches}"
BB_WS_STORE="${BB_WS_STORE:-/config/bb-api/wine-switches.json}"
BB_WS_APPLIED="${BB_WS_APPLIED:-/tmp/.bb-wine-switches-applied}"
# The client release before the one that needs the service token.
BB_WS_SAFE_CLIENT="10.0.1.1069"

# The registry rows, without the header, as "var patches class requires".
bb_ws_rows() {
    [ -r "$BB_WS_REGISTRY" ] || return 0
    grep -v '^#' "$BB_WS_REGISTRY" | cut -f1-4 | tr '\t' ' '
}

# available, upstream or absent: whether this Wine carries every patch behind
# the switch, already has them from upstream, or lacks them.
bb_ws_state() {
    _ws_pats="$(grep "^$1	" "$BB_WS_REGISTRY" 2>/dev/null | cut -f2 | tr ',' ' ')"
    [ -n "$_ws_pats" ] && [ -r "$BB_WS_MANIFEST" ] || { echo absent; return; }
    _ws_state=available
    for _ws_p in $_ws_pats; do
        if grep -q "^upstream $_ws_p\$" "$BB_WS_MANIFEST"; then
            _ws_state=upstream
        elif ! grep -q "^applied $_ws_p " "$BB_WS_MANIFEST"; then
            echo absent; return
        fi
    done
    echo "$_ws_state"
}

# 1 or 0 as chosen, before interlocks: the store when it names the switch,
# otherwise the container variable.
bb_ws_chosen() {
    _ws_v=""
    if [ -f "$BB_WS_STORE" ]; then
        _ws_v="$(sed -n "s/.*\"$1\": *\"\\([01]\\)\".*/\\1/p" "$BB_WS_STORE" | head -1)"
    fi
    if [ -z "$_ws_v" ]; then
        eval "_ws_v=\"\${$1:-}\""
    fi
    case "$_ws_v" in 1|on|true|yes) echo 1 ;; *) echo 0 ;; esac
}

# Where the choice for this switch comes from: setting or variable.
bb_ws_source() {
    if [ -f "$BB_WS_STORE" ] && grep -q "\"$1\": *\"[01]\"" "$BB_WS_STORE" 2>/dev/null; then
        echo setting
    else
        echo variable
    fi
}

# One line per switch this Wine carries: "VAR VALUE NOTE...". VALUE is what to
# export after the interlocks. A switch that needs another one that is off is
# turned off too, with a note saying so.
bb_ws_resolve() {
    bb_ws_rows | while read -r _ws_var _ws_pats _ws_class _ws_req; do
        [ "$_ws_var" = WINE_MOUNTPOINTS_AS_DIRS ] && continue
        [ "$(bb_ws_state "$_ws_var")" = available ] || continue
        _ws_val="$(bb_ws_chosen "$_ws_var")"
        _ws_note=""
        if [ "$_ws_val" = 1 ] && [ "$_ws_req" != "-" ]; then
            if [ "$(bb_ws_state "$_ws_req")" = available ] && [ "$(bb_ws_chosen "$_ws_req")" != 1 ]; then
                _ws_val=0
                _ws_note="off because it needs $_ws_req, which is off"
            fi
        fi
        echo "$_ws_var $_ws_val $_ws_note"
    done
}

# Whether the service token is off in a Wine that has the switch. Then a
# client from 10.0.3.1075 on completes no pass, so the client must be pinned.
bb_ws_token_off() {
    [ "$(bb_ws_state WINE_SERVICE_TOKEN)" = available ] && [ "$(bb_ws_chosen WINE_SERVICE_TOKEN)" != 1 ]
}

# Whether a client version needs the service token: 10.0.3.1075 or later.
bb_ws_client_needs_token() {
    _ws_ver="$1"
    [ -n "$_ws_ver" ] || return 1
    printf '%s\n10.0.3.1075\n' "$_ws_ver" | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | head -1 | grep -qx "10.0.3.1075"
}
