# Two checks on the mapped drives, for bb-doctor. Sourced by a beta-only patch
# after the drive-mapping section; fold into that script at the next stable release.
#
# Both come from one support thread. A user moved to this container, inherited the
# backup, and got "No files are selected" followed by permission skips and then a
# safety freeze. Two container-shaped faults sat under that: a share the container
# user could not read, which the skipped-file check finds only after the client has
# given up on files, and a drive whose identity the client no longer recognised.
# Neither was named by anything, and both can be, cheaply, before the client has
# to fail first.
#
# Nothing here is repaired. The first is the user's files on their share; the
# second is the identity of their backup. The wrong repair to either costs more
# than the fault.

echo "Source drives"
_drives=0
for _link in "${PREFIX}dosdevices"/[d-z]:; do
    [ -L "$_link" ] || continue
    _drives=$((_drives+1))
    _letter="$(basename "$_link" | cut -c1 | tr 'a-z' 'A-Z')"
    _root="$(readlink -f "$_link" 2>/dev/null)"
    if [ -z "$_root" ] || [ ! -d "$_root" ]; then
        BAD "${_letter}: is mapped but its target is missing"
        continue
    fi

    # -- Ownership, before the client has to discover it --------------------------
    # As the user this script runs as, which after the root drop is the client's.
    # The root and a sample of what is directly under it: a wrong owner on a
    # share is usually the whole share or a whole top-level folder, so a shallow
    # sample finds it without walking a NAS.
    if [ ! -r "$_root" ] || [ ! -x "$_root" ]; then
        BAD "${_letter}: root ${_root} cannot be read by $(id -un 2>/dev/null || id -u) ($(id -u):$(id -g))"
        NOTE "owner and mode: $(stat -c '%U:%G %a' "$_root" 2>/dev/null || echo unknown). Correct USER_ID/GROUP_ID, or the owner on the host."
        continue
    fi
    _unreadable=""; _seen=0
    for _e in "$_root"/* "$_root"/.[!.]*; do
        [ -e "$_e" ] || continue
        case "$(basename "$_e")" in .bzvol) continue ;; esac
        _seen=$((_seen+1))
        [ "$_seen" -gt 40 ] && break
        if [ -d "$_e" ]; then
            { [ -r "$_e" ] && [ -x "$_e" ]; } || _unreadable="${_unreadable}${_e}
"
        else
            [ -r "$_e" ] || _unreadable="${_unreadable}${_e}
"
        fi
    done
    if [ -n "$_unreadable" ]; then
        _n="$(printf '%s' "$_unreadable" | grep -c .)"
        WARN "${_letter}: ${_n} of the first ${_seen} entries under ${_root} cannot be read by this container"
        printf '%s' "$_unreadable" | head -5 | while read -r _p; do
            [ -n "$_p" ] || continue
            NOTE "  ${_p}  ($(stat -c '%U:%G %a' "$_p" 2>/dev/null || echo unknown))"
        done
        NOTE "the client will skip everything under these. Correct the owner on the host:"
        NOTE "  chown -R $(id -u):$(id -g) '<that folder>'"
    else
        OK "${_letter}: ${_root} readable, ${_seen} top-level entr$([ "$_seen" = 1 ] && echo y || echo ies) sampled"
    fi

    # -- Identity: does the client still know this drive? ---------------------------
    # The client writes its own id for a volume under <root>/.bzvol and lists the
    # volumes it knows in bzvolumes.xml. After an inherit onto a fresh install, or
    # a .bzvol rewritten by a different install, the two can disagree, and the
    # client then shows the drive as selected but backs nothing up. The id is
    # found by shape (a GUID or a 32-hex token) rather than by a file name, since
    # the layout under .bzvol is not established from a capture; when no such
    # token is present the check says so and claims nothing.
    _vol="${_root}/.bzvol"
    _vols="${BZ}/bzvolumes.xml"
    if [ ! -d "$_vol" ]; then
        NOTE "${_letter}: no .bzvol yet; the client has not taken ownership of this drive"
    elif [ ! -r "$_vols" ]; then
        NOTE "${_letter}: .bzvol present; bzvolumes.xml not readable, identity not checked"
    else
        _id="$(grep -rhoE '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}' "$_vol" 2>/dev/null | head -1)"
        if [ -z "$_id" ]; then
            NOTE "${_letter}: .bzvol present but no volume id found in it; identity not checked"
        elif grep -qi -- "$_id" "$_vols" 2>/dev/null; then
            OK "${_letter}: the client recognises this drive (id ${_id})"
        else
            WARN "${_letter}: the client does not recognise this drive's identity (${_id} is not in bzvolumes.xml)"
            NOTE "this is the state after an inherit onto a fresh install, or when another install rewrote .bzvol."
            NOTE "the client can show the drive as selected and back up nothing. Not repaired here: the"
            NOTE "identity is your backup's, and the wrong change discards it. Check the drive in the client's"
            NOTE "settings, and if you inherited, that the right computer was chosen."
        fi
    fi
done
[ "$_drives" = 0 ] && WARN "no drives mapped: mount your data at /drive_d, /drive_e, ..."
echo
