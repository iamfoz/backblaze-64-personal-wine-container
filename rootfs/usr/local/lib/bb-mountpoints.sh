# Which drives show their mount points as plain folders. Sourced by startapp.sh,
# which exports the result as WINE_MOUNTPOINTS_AS_DIRS for the fork's patched
# Wine, and by bb-doctor, which reports the datasets under those drives as
# included. WineHQ's Wine (:latest) has no such patch, and startapp.sh leaves
# the value out there.
#
# The Settings tab's store wins when it exists, the container's
# MOUNTPOINTS_AS_DIRS variable decides when it does not. bbmounts.py is the
# Python side of the same rules and the two must agree.

BB_MOUNTPOINTS_STORE="${BB_MOUNTPOINTS_STORE:-/config/bb-api/mountpoints.json}"

# Drive letters in a value, lower case, sorted, once each, D: to Z: only.
_bb_mp_letters() {
    printf '%s' "$1" | tr 'A-Z' 'a-z' | tr -cd 'd-z' | fold -w1 | sort -u | tr -d '\n'
}

# The value for WINE_MOUNTPOINTS_AS_DIRS: "1" for every drive, letters for some,
# empty for none.
bb_mountpoints_value() {
    if [ -f "$BB_MOUNTPOINTS_STORE" ] && grep -q '"drives": *"[^"]*"' "$BB_MOUNTPOINTS_STORE" 2>/dev/null; then
        _bb_mp_letters "$(sed -n 's/.*"drives": *"\([^"]*\)".*/\1/p' "$BB_MOUNTPOINTS_STORE" | head -1)"
        return
    fi
    case "$(printf '%s' "${MOUNTPOINTS_AS_DIRS:-}" | tr 'A-Z' 'a-z' | tr -d ' ')" in
        all|true|yes|1) printf '1' ;;
        ""|false|no|0|none) ;;
        *) _bb_mp_letters "$MOUNTPOINTS_AS_DIRS" ;;
    esac
}

# Whether the drive with this letter (either case) has its mount points shown as
# folders under the given value.
bb_mountpoints_covers() {
    _bb_mp_l="$(printf '%s' "$2" | tr 'A-Z' 'a-z')"
    [ "$1" = 1 ] && return 0
    [ -n "$_bb_mp_l" ] && case "$1" in *"$_bb_mp_l"*) return 0 ;; esac
    return 1
}
