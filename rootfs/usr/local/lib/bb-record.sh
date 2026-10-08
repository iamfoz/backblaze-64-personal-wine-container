# Shared by bb-watchdog, bb-doctor --fix and startapp's service watch: one
# line per recovery action into a file the monitor reads, so an action shows
# on the Status tab's timeline, fires a notification and counts in the metrics
# as well as in the container log. Sourced, not executed.
#
#   bb_record <source> <message>
#
# Lines are "epoch<TAB>source<TAB>message". The file is kept short by trimming
# to the newest 500 lines once it passes 1000, and is given the config
# directory's owner when the writer is root, so the container user can append
# to it afterwards.
BB_RECOVERY_LOG="${BB_RECOVERY_LOG:-/config/bb-api/recovery.log}"

bb_record() {
    _br_dir="$(dirname "$BB_RECOVERY_LOG")"
    [ -d "$_br_dir" ] || mkdir -p "$_br_dir" 2>/dev/null || return 0
    printf '%s\t%s\t%s\n' "$(date +%s)" "$1" "$(printf '%s' "$2" | tr '\t\n' '  ')" >> "$BB_RECOVERY_LOG" 2>/dev/null || return 0
    if [ "$(id -u 2>/dev/null)" = 0 ] && [ -d /config ]; then
        chown --reference=/config "$BB_RECOVERY_LOG" 2>/dev/null
    fi
    if [ "$(wc -l < "$BB_RECOVERY_LOG" 2>/dev/null || echo 0)" -gt 1000 ]; then
        tail -n 500 "$BB_RECOVERY_LOG" > "$BB_RECOVERY_LOG.tmp" 2>/dev/null && mv "$BB_RECOVERY_LOG.tmp" "$BB_RECOVERY_LOG"
    fi
    return 0
}
