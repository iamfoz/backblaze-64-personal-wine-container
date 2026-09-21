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
    _root="$(readlink -f "$_link" 2>/dev/null)"
    # Wine's own z: -> / is the container, not a drive the user mapped, and a
    # note that the client has not taken ownership of it reads as a fault.
    [ "$_root" = "/" ] && continue
    _drives=$((_drives+1))
    _letter="$(basename "$_link" | cut -c1 | tr 'a-z' 'A-Z')"
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
    # The client stamps each drive it owns with an id in <root>/.bzvol/bzvol_id.xml
    # ("v00" followed by 25 hex characters, captured 2026-09-21)
    # and lists the drives it knows in bzvolumes.xml, each with the mount point
    # as hex: 443a5c is D:\. When the stamp and the list disagree the client
    # shows the drive as ticked and backs nothing up. Three cases, told apart
    # by the list: the id is known under another letter (the mapping moved), the
    # list has a different id for this letter (another install stamped the
    # drive, or an inherit brought a different record), or the list has nothing
    # for this letter at all (the client never owned it). Not repaired here: the
    # id is the backup's identity, and the wrong change starts the drive over.
    _vol="${_root}/.bzvol"
    _idf="${_vol}/bzvol_id.xml"
    _vols="${BZ}/bzvolumes.xml"
    _hex="$(printf '%s:\\' "$_letter" | od -An -tx1 | tr -d ' \n')"
    if [ ! -d "$_vol" ]; then
        NOTE "${_letter}: no .bzvol yet; the client has not taken ownership of this drive"
    elif [ ! -r "$_vols" ]; then
        NOTE "${_letter}: .bzvol present; bzvolumes.xml not readable, identity not checked"
    else
        _id="$(grep -oE 'v[0-9a-f]{27}' "$_idf" 2>/dev/null | head -1)"
        _known="$(grep "mountPointPathHex=\"${_hex}\"" "$_vols" 2>/dev/null | grep -oE 'bzVolumeGuid="v[0-9a-f]{27}"' | grep -oE 'v[0-9a-f]{27}' | head -1)"
        if [ -z "$_id" ]; then
            if [ -f "$_idf" ]; then
                WARN "${_letter}: .bzvol/bzvol_id.xml carries no volume id, so the client cannot match this drive to its backup"
                if [ -n "$_known" ]; then
                    NOTE "the client's own record for ${_letter}: is ${_known}. With the container stopped, put that id back as"
                    NOTE "the id in ${_idf}, and the existing backup of this drive carries on."
                else
                    NOTE "the client has no record of a drive at ${_letter}: either. Untick and re-tick it in the client's"
                    NOTE "settings; the client stamps it afresh and its upload starts over."
                fi
            else
                NOTE "${_letter}: .bzvol present but no bzvol_id.xml yet; the client has not taken ownership of this drive"
            fi
        elif [ "$_known" = "$_id" ]; then
            OK "${_letter}: the client recognises this drive (id ${_id})"
        elif grep -q "bzVolumeGuid=\"${_id}\"" "$_vols" 2>/dev/null; then
            _ohex="$(grep "bzVolumeGuid=\"${_id}\"" "$_vols" | grep -oE 'mountPointPathHex="[0-9a-f]*"' | grep -oE '[0-9a-f]{2}' | head -1)"
            _other="$(printf "\\$(printf '%03o' $((0x${_ohex:-3f})))")"
            WARN "${_letter}: the client knows this drive as ${_other}:, not ${_letter}: (id ${_id})"
            NOTE "the drive letter changed. Map the folder back to drive_$(printf '%s' "$_other" | tr 'A-Z' 'a-z') so it is ${_other}: again, or the"
            NOTE "client treats it as a new drive and starts its upload over."
        else
            WARN "${_letter}: the client does not recognise this drive's identity (${_id} is not in bzvolumes.xml)"
            if [ -n "$_known" ]; then
                NOTE "the client's own record for ${_letter}: is ${_known}: another install stamped this drive, or an"
                NOTE "inherit brought a different record. To keep the existing backup of this drive, stop the container"
                NOTE "and replace the id in ${_idf} with ${_known}."
            else
                NOTE "the client has no record of a drive at ${_letter}: at all. Untick and re-tick it in the client's"
                NOTE "settings; the client stamps it afresh and its upload starts over. If you inherited, check that"
                NOTE "the right computer was chosen."
            fi
        fi
    fi
done
[ "$_drives" = 0 ] && WARN "no drives mapped: mount your data at /drive_d, /drive_e, ..."
echo
