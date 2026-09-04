# Run bb-doctor as the container user, not as root. Sourced by a beta-only patch
# near the top of bb-doctor; fold it into that script at the next stable release.
#
# `docker exec` enters the container as root, and root passes every permission
# test. The checks that matter most in this container are permission tests:
# `[ -w /config ]` decides whether the data directory is writable, and the
# skipped-file check decides with `[ -r "$path" ]` whether the client can read a
# file it gave up on. Under root both always say yes. A user with the most common
# fault this container has, a share owned by the wrong uid, ran the tool the way
# the README said to and was told the files could be read.
#
# So when this runs as root it re-runs itself as the user the client runs as,
# which is the user whose view of the files is the one that counts. The same
# arguments are passed through. Nothing here changes what bb-doctor checks; it
# changes who is asking.
#
# The target is USER_ID:GROUP_ID, which the image sets for the client and which
# `docker exec` inherits. When those are absent, the owner of /config is used,
# because the client must own /config to run at all. A target of root means the
# container really is configured to run as root, and then root is the right
# answer and nothing is done.

if [ "$(id -u 2>/dev/null)" = 0 ] && [ -z "${BB_DOCTOR_DROPPED:-}" ]; then
    _tu="${USER_ID:-}"; _tg="${GROUP_ID:-}"
    [ -n "$_tu" ] || _tu="$(stat -c %u /config 2>/dev/null)"
    [ -n "$_tg" ] || _tg="$(stat -c %g /config 2>/dev/null)"
    case "$_tu" in ''|*[!0-9]*) _tu="" ;; esac
    case "$_tg" in ''|*[!0-9]*) _tg="$_tu" ;; esac
    if [ -n "$_tu" ] && [ "$_tu" != 0 ]; then
        # The guard is set before the exec so a failure inside the re-run cannot
        # loop back here. HOME follows the client's, in case a check ever needs it.
        export BB_DOCTOR_DROPPED=1 HOME=/config
        if command -v setpriv >/dev/null 2>&1; then
            exec setpriv --reuid="$_tu" --regid="$_tg" --clear-groups -- "$0" "$@"
        elif command -v su-exec >/dev/null 2>&1; then
            exec su-exec "${_tu}:${_tg}" "$0" "$@"
        fi
        # No way to drop privileges in this image. Say so at the top, where it
        # will be read, and let the checks run; the ones below that depend on
        # permissions now say when they cannot be trusted.
        WARN "running as root: permission checks cannot fail, so an unreadable file reads as readable"
        NOTE "run this as the container user instead: docker exec -u ${_tu}:${_tg} <container> bb-doctor"
        echo
    fi
fi
